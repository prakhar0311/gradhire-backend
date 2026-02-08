import os
import requests
import re
import random
import uuid
from dotenv import load_dotenv

load_dotenv()

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_API_KEY = os.getenv("ADZUNA_API_KEY")

# -------- CONFIG --------
REQUEST_TIMEOUT = 5
MAX_DESCRIPTION_LENGTH = 2000
MAX_JOBS_RETURNED = 20
MAX_PAGES_TO_SCAN = 3  # 🔥 scan multiple pages

# -------- STOPWORDS --------
STOPWORDS = {
    "the", "and", "to", "a", "of", "in", "for", "with",
    "on", "at", "by", "an", "be", "is", "are", "as", "from", "or"
}

# -------- VISA FILTER --------
VISA_KEYWORDS = [
    "visa",
    "sponsorship",
    "h1b",
    "opt",
    "cpt",
    "relocation"
]

def is_visa_friendly(description: str) -> bool:
    if not description:
        return False
    return any(word in description.lower() for word in VISA_KEYWORDS)

# -------- TOKENIZER --------
def tokenize(text: str):
    if not text:
        return set()

    words = re.findall(r"\b\w+\b", text.lower())

    return {
        w for w in words
        if w not in STOPWORDS and len(w) > 2
    }

# -------- MATCH SCORE --------
def compute_match_score(resume_text: str, job_text: str) -> int:

    resume_words = tokenize(resume_text)
    job_words = tokenize(job_text)

    if not resume_words or not job_words:
        return 40

    overlap = resume_words.intersection(job_words)
    count = len(overlap)

    if count >= 15:
        base = 85
    elif count >= 10:
        base = 70
    elif count >= 6:
        base = 55
    elif count >= 3:
        base = 40
    else:
        base = 25

    return min(100, base + random.randint(0, 8))

# -------- CLEAN DESCRIPTION --------
def clean_description(text: str) -> str:

    if not text:
        return ""

    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > MAX_DESCRIPTION_LENGTH:
        text = text[:MAX_DESCRIPTION_LENGTH] + "..."

    return text

# -------- ENTRY LEVEL FILTER --------

ENTRY_LEVEL_KEYWORDS = [
    "junior",
    "entry level",
    "graduate",
    "fresher",
    "intern",
    "trainee",
    "associate",
    "new grad",
    "0-2",
    "0 – 2",
]

SENIOR_KEYWORDS = [
    "senior",
    "lead",
    "principal",
    "staff",
    "manager",
    "architect",
    "5+",
    "6+",
    "7+",
]

def is_entry_level(title: str, description: str) -> bool:

    text = f"{title} {description}".lower()

    # reject obvious senior roles
    if any(word in text for word in SENIOR_KEYWORDS):
        return False

    # prefer strong entry signals
    if any(word in text for word in ENTRY_LEVEL_KEYWORDS):
        return True

    # allow neutral roles but not management
    return True

# -------- FETCH JOBS --------
def fetch_jobs(
    query: str,
    country: str = "in",
    limit: int = MAX_JOBS_RETURNED,
    resume_text: str = ""
):

    if not ADZUNA_APP_ID or not ADZUNA_API_KEY:
        print("❌ Missing Adzuna API keys")
        return []

    collected_jobs = []

    for page in range(1, MAX_PAGES_TO_SCAN + 1):

        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_API_KEY,
            "what": query,
            "results_per_page": limit,
        }

        try:
            response = requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            response.raise_for_status()
            data = response.json()

        except requests.exceptions.Timeout:
            print("⏱ Adzuna timeout")
            break

        except requests.exceptions.RequestException as e:
            print("❌ Adzuna request failed:", str(e))
            break

        for job in data.get("results", []):

            try:
                title = job.get("title", "")
                description_raw = job.get("description", "")

                if not is_entry_level(title, description_raw):
                    continue

                description = clean_description(description_raw)

                if country == "us" and not is_visa_friendly(description):
                    continue

                score = compute_match_score(
                    resume_text,
                    description
                )

                collected_jobs.append({
                    "id": str(uuid.uuid4()),
                    "title": title or "Unknown",
                    "company": job.get("company", {}).get("display_name") or "Unknown",
                    "location": job.get("location", {}).get("display_name") or "Remote",
                    "description": description,
                    "matchScore": int(score),
                    "applyURL": job.get("redirect_url") or ""
                })

                # stop early if enough jobs collected
                if len(collected_jobs) >= MAX_JOBS_RETURNED:
                    break

            except Exception as e:
                print("⚠️ Skipping bad job entry:", str(e))
                continue

        if len(collected_jobs) >= MAX_JOBS_RETURNED:
            break

    collected_jobs.sort(key=lambda x: x["matchScore"], reverse=True)

    return collected_jobs[:MAX_JOBS_RETURNED]
