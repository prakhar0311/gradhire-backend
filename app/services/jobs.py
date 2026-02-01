import os
import requests
import re
import random
import uuid
from dotenv import load_dotenv

load_dotenv()

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_API_KEY = os.getenv("ADZUNA_API_KEY")

# -------- STOPWORDS --------
STOPWORDS = {
    "the", "and", "to", "a", "of", "in", "for", "with",
    "on", "at", "by", "an", "be", "is", "are", "as", "from", "or"
}

# -------- VISA FILTER (US ONLY) --------
VISA_KEYWORDS = [
    "visa",
    "sponsorship",
    "h1b",
    "opt",
    "cpt",
    "relocation"
]

def is_visa_friendly(description: str):
    return any(word in description.lower() for word in VISA_KEYWORDS)

# -------- TEXT HELPERS --------
def tokenize(text: str):
    words = re.findall(r"\b\w+\b", text.lower())
    return {
        w for w in words
        if w not in STOPWORDS and len(w) > 2
    }

# -------- MATCH SCORE --------
def compute_match_score(resume_text: str, job_text: str):
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

    return base + random.randint(0, 10)

# -------- FETCH JOBS --------
def fetch_jobs(
    query: str,
    country: str = "in",
    limit: int = 10,
    resume_text: str = ""
):
    if not ADZUNA_APP_ID or not ADZUNA_API_KEY:
        raise Exception("Adzuna API keys not set")

    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"

    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_API_KEY,
        "what": query,
        "results_per_page": limit,
    }

    response = requests.get(url, params=params)
    response.raise_for_status()

    data = response.json()
    jobs = []

    for job in data.get("results", []):
        description = job.get("description", "")

        # 🇺🇸 Visa filter only for US
        if country == "us" and not is_visa_friendly(description):
            continue

        score = compute_match_score(resume_text, description)

        jobs.append({
            # ✅ EXACT Swift-compatible schema
            "id": str(uuid.uuid4()),
            "title": job.get("title") or "Unknown",
            "company": job.get("company", {}).get("display_name") or "Unknown",
            "location": job.get("location", {}).get("display_name") or "Remote",
            "description": description or "",
            "matchScore": int(score),
            "applyURL": job.get("redirect_url") or ""
        })

    jobs.sort(key=lambda x: x["matchScore"], reverse=True)
    return jobs
