import os
import requests
import re
import uuid
import logging
from dotenv import load_dotenv

load_dotenv()

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_API_KEY = os.getenv("ADZUNA_API_KEY")

REQUEST_TIMEOUT = 6
MAX_DESCRIPTION_LENGTH = 1800
MAX_JOBS_RETURNED = 20
MAX_PAGES_TO_SCAN = 4
MAX_RETRIES = 2


# =====================================================
# STOPWORDS
# =====================================================

STOPWORDS = {
    "the","and","to","a","of","in","for","with",
    "on","at","by","an","be","is","are","as","from","or"
}


# =====================================================
# TECH KEYWORDS (MODERN STACK)
# =====================================================

TECH_KEYWORDS = {

    "python","java","javascript","typescript","go","c++","c#","swift",

    "react","node","express","fastapi","flask","django",

    "sql","mysql","postgresql","mongodb","redis",

    "aws","gcp","azure","docker","kubernetes",

    "git","api","rest",

    "machine","learning","tensorflow","pytorch",

    "html","css","frontend","backend","fullstack"
}


# =====================================================
# ENTRY LEVEL KEYWORDS
# =====================================================

ENTRY_LEVEL_KEYWORDS = {

    "junior",
    "entry",
    "graduate",
    "new grad",
    "fresher",
    "intern",
    "internship",
    "trainee",
    "associate",
    "campus"
}


# =====================================================
# SENIOR BLOCKLIST (STRICT)
# =====================================================

SENIOR_BLOCKLIST = {

    "senior",
    "sr",
    "lead",
    "principal",
    "staff",
    "manager",
    "architect",
    "director",
    "head",
    "vp",
    " ii",
    " iii",
    " iv"
}


# =====================================================
# TOKENIZER
# =====================================================

def tokenize(text: str):

    if not text:
        return set()

    words = re.findall(r"\b[a-zA-Z]+\b", text.lower())

    return {
        w for w in words
        if w not in STOPWORDS and len(w) > 2
    }


# =====================================================
# EXPERIENCE DETECTOR
# =====================================================

def extract_required_experience(text: str):

    matches = re.findall(r"(\d+)\+?\s*(?:years|yrs)", text.lower())

    if not matches:
        return 0

    try:
        return min(int(x) for x in matches)
    except:
        return 0


# =====================================================
# ENTRY LEVEL FILTER (CORE OF GRADHIRE)
# =====================================================

def extract_years(text: str):

    matches = re.findall(r"(\d+)\+?\s*(?:years|yrs)", text.lower())

    if not matches:
        return None   # Important change

    try:
        return min(int(x) for x in matches)
    except:
        return None


def is_entry_level(title: str, description: str):

    text = f"{title} {description}".lower()

    # Always block senior roles
    if any(word in text for word in SENIOR_BLOCKLIST):
        return False

    years = extract_years(text)

    # If experience explicitly mentioned and >2 → reject
    if years is not None and years > 2:
        return False

    # If explicitly entry-level → accept
    if any(word in text for word in ENTRY_LEVEL_KEYWORDS):
        return True

    # If no experience mentioned → assume entry-level (REAL-WORLD FIX)
    if years is None:
        return True

    # If <=2 years → accept
    return years <= 2


# =====================================================
# MATCH SCORE (0–100 NORMALIZED)
# =====================================================

def compute_match_score(resume_text: str, job_text: str):

    resume_words = tokenize(resume_text)
    job_words = tokenize(job_text)

    if not resume_words or not job_words:
        return 50

    overlap = resume_words.intersection(job_words)

    tech_overlap = overlap.intersection(TECH_KEYWORDS)

    # weighted overlap score
    base_score = len(overlap)
    tech_bonus = len(tech_overlap) * 2

    raw_score = base_score + tech_bonus

    # normalize relative to job size
    max_possible = max(len(job_words), 20)

    normalized = (raw_score / max_possible) * 100

    # clamp properly
    score = int(max(25, min(normalized, 95)))

    return score


# =====================================================
# CLEAN DESCRIPTION
# =====================================================

def clean_description(text: str):

    if not text:
        return ""

    text = re.sub(r"\s+", " ", text).strip()

    return text[:MAX_DESCRIPTION_LENGTH]


# =====================================================
# FETCH JOBS
# =====================================================

def fetch_jobs(
    query: str,
    country: str = "in",
    limit: int = MAX_JOBS_RETURNED,
    resume_text: str = ""
):

    if not ADZUNA_APP_ID or not ADZUNA_API_KEY:
        logging.error("Missing Adzuna API keys")
        return []

    # Multiple fallback queries (CRITICAL FIX)
    queries = [

        query,

        "software engineer",

        "software developer",

        "junior software engineer",

        "fresher software engineer",

        "graduate software engineer",

        "entry level software engineer"
    ]

    collected_jobs = []
    seen_urls = set()

    for q in queries:

        logging.info(f"Trying query: {q}")

        for page in range(1, MAX_PAGES_TO_SCAN + 1):

            url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

            params = {
                "app_id": ADZUNA_APP_ID,
                "app_key": ADZUNA_API_KEY,
                "what": q,
                "results_per_page": 20,
                "sort_by": "date"
            }

            try:

                response = requests.get(
                    url,
                    params=params,
                    timeout=REQUEST_TIMEOUT
                )

                response.raise_for_status()

                data = response.json()

            except Exception as e:

                logging.warning(f"Adzuna fetch failed: {str(e)}")

                continue


            for job in data.get("results", []):

                title = job.get("title", "")
                description_raw = job.get("description", "")
                redirect_url = job.get("redirect_url", "")

                if not redirect_url:
                    continue

                if redirect_url in seen_urls:
                    continue

                if not is_entry_level(title, description_raw):
                    continue

                description = clean_description(description_raw)

                score = compute_match_score(
                    resume_text,
                    description
                )

                job_obj = {

                    "id": str(uuid.uuid4()),

                    "title": title,

                    "company":
                    job.get("company", {})
                    .get("display_name", "Unknown"),

                    "location":
                    job.get("location", {})
                    .get("display_name", "Remote"),

                    "description": description,

                    "matchScore": score,

                    "applyURL": redirect_url
                }

                collected_jobs.append(job_obj)

                seen_urls.add(redirect_url)

                if len(collected_jobs) >= limit:
                    break

            if len(collected_jobs) >= limit:
                break

        if len(collected_jobs) >= limit:
            break


    collected_jobs.sort(
        key=lambda x: x["matchScore"],
        reverse=True
    )

    return collected_jobs[:limit]
