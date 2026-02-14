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
MAX_DESCRIPTION_LENGTH = 2000
MAX_JOBS_RETURNED = 20
MAX_PAGES_TO_SCAN = 4
MAX_RETRIES = 2

STOPWORDS = {
    "the","and","to","a","of","in","for","with",
    "on","at","by","an","be","is","are","as","from","or"
}

TECH_KEYWORDS = {
    "python","java","swift","react","javascript","node",
    "aws","docker","sql","api","git","ios","android"
}

VISA_KEYWORDS = {
    "visa","sponsorship","h1b","opt","cpt","relocation"
}

ENTRY_LEVEL_KEYWORDS = {
    "junior","entry level","graduate","fresher",
    "intern","trainee","associate","new grad"
}

SENIOR_KEYWORDS = {
    "senior","lead","principal","staff",
    "manager","architect","director"
}


# =====================================================
# TOKENIZER
# =====================================================

def tokenize(text: str):

    if not text:
        return set()

    words = re.findall(r"\b\w+\b", text.lower())

    return {
        w for w in words
        if w not in STOPWORDS and len(w) > 2
    }


# =====================================================
# MATCH SCORE (DETERMINISTIC, TRUSTABLE)
# =====================================================

def compute_match_score(resume_text: str, job_text: str):

    resume_words = tokenize(resume_text)
    job_words = tokenize(job_text)

    if not resume_words or not job_words:
        return 35

    overlap = resume_words.intersection(job_words)

    overlap_score = len(overlap)

    tech_overlap = overlap.intersection(TECH_KEYWORDS)

    score = (
        overlap_score * 3 +
        len(tech_overlap) * 5
    )

    score = min(score, 95)

    if score < 35:
        score = 35

    return int(score)


# =====================================================
# ENTRY LEVEL FILTER (STRICT)
# =====================================================

def is_entry_level(title: str, description: str):

    text = f"{title} {description}".lower()

    if any(word in text for word in SENIOR_KEYWORDS):
        return False

    if any(word in text for word in ENTRY_LEVEL_KEYWORDS):
        return True

    return True


# =====================================================
# VISA FILTER
# =====================================================

def is_visa_friendly(description: str):

    if not description:
        return False

    text = description.lower()

    return any(word in text for word in VISA_KEYWORDS)


# =====================================================
# CLEAN DESCRIPTION
# =====================================================

def clean_description(text: str):

    if not text:
        return ""

    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > MAX_DESCRIPTION_LENGTH:
        text = text[:MAX_DESCRIPTION_LENGTH] + "..."

    return text


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

    if not query:
        query = "software engineer"

    collected_jobs = []
    seen_urls = set()

    for page in range(1, MAX_PAGES_TO_SCAN + 1):

        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_API_KEY,
            "what": query,
            "results_per_page": limit
        }

        response = None

        for attempt in range(MAX_RETRIES):

            try:

                response = requests.get(
                    url,
                    params=params,
                    timeout=REQUEST_TIMEOUT
                )

                response.raise_for_status()
                break

            except requests.exceptions.RequestException as e:

                logging.warning(
                    f"Adzuna retry {attempt+1}: {str(e)}"
                )

        if not response:
            continue

        try:
            data = response.json()
        except:
            continue

        for job in data.get("results", []):

            try:

                title = job.get("title") or ""
                description_raw = job.get("description") or ""
                redirect_url = job.get("redirect_url") or ""

                if not redirect_url:
                    continue

                if redirect_url in seen_urls:
                    continue

                if not is_entry_level(title, description_raw):
                    continue

                description = clean_description(description_raw)

                if country == "us":
                    if not is_visa_friendly(description):
                        continue

                score = compute_match_score(
                    resume_text,
                    description
                )

                job_obj = {

                    "id": str(uuid.uuid4()),

                    "title": title or "Software Engineer",

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

            except Exception as e:

                logging.warning(
                    f"Skipping bad job entry: {str(e)}"
                )

                continue

        if len(collected_jobs) >= limit:
            break

    collected_jobs.sort(
        key=lambda x: x["matchScore"],
        reverse=True
    )

    return collected_jobs[:limit]
