import os
import re
import time
import uuid
import asyncio
import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

load_dotenv()

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_API_KEY = os.getenv("ADZUNA_API_KEY")

ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs"

# Per-request: 3s to connect, 6s to read. Whole endpoint is capped separately.
REQUEST_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
OVERALL_TIMEOUT = 10.0
MAX_CONCURRENT_REQUESTS = 7

MAX_DESCRIPTION_LENGTH = 1800
MAX_JOBS_RETURNED = 20
MAX_DAYS_OLD = 30
MATCH_MAX_DAYS_OLD = 180

# Adzuna's free tier is rate limited, so results are cached in-process.
MATCH_CACHE_TTL = 30 * 60
NEW_GRAD_CACHE_TTL = 30 * 60
NEW_GRAD_PAGE_SIZE = 20

# Stable UUIDs derived from Adzuna's job id, so bookmarks survive refetches.
JOB_ID_NAMESPACE = uuid.UUID("6f1c2a52-8d3e-4b7a-9c41-2f5e0d8b7a10")


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
    "avp",
    "vice",
    "ii",
    "iii",
    "iv"
}

# Whole words only, so "leadership" or "ahead" don't trip it. Letter
# boundaries (not \b) so "IN_Sr Associate" or "Lead/Manager" still match.
SENIOR_TITLE_PATTERN = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(SENIOR_BLOCKLIST)) + r")(?![a-z])",
    re.IGNORECASE
)

# Searched in job titles (Adzuna's title_only takes one term per request).
NEW_GRAD_TITLE_KEYWORDS = [
    "graduate",
    "fresher",
    "entry level",
    "junior",
    "trainee",
    "new grad",
    "associate"
]

# Note: Adzuna's what_exclude also matches descriptions ("lead the team",
# "report to manager") and drops ~80% of valid roles, so seniority is
# filtered on titles only via SENIOR_TITLE_PATTERN.


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


def is_senior_title(title: str) -> bool:

    return bool(SENIOR_TITLE_PATTERN.search(title or ""))


def is_entry_level(title: str, description: str):

    text = f"{title} {description}".lower()

    # Always block senior roles (judged on the title only)
    if is_senior_title(title):
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




class AdzunaUnavailable(Exception):
    """Every Adzuna request failed or timed out."""


# =====================================================
# TTL CACHE (IN-PROCESS)
# =====================================================

_cache: Dict[str, Tuple[float, object]] = {}


def cache_get(key: str, allow_stale: bool = False):

    entry = _cache.get(key)

    if not entry:
        return None

    expires_at, value = entry

    if allow_stale or time.time() < expires_at:
        return value

    return None


def cache_set(key: str, value, ttl: int):

    _cache[key] = (time.time() + ttl, value)


# =====================================================
# ADZUNA CLIENT
# =====================================================

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:

    global _client

    if _client is None:
        _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    return _client


async def close_client():

    global _client

    if _client is not None:
        await _client.aclose()
        _client = None


async def adzuna_search(
    country: str,
    params: dict,
    page: int = 1,
    semaphore: Optional[asyncio.Semaphore] = None
) -> Optional[List[dict]]:

    # Returns None on failure so callers can tell "no jobs" from "Adzuna down".
    url = f"{ADZUNA_BASE_URL}/{country}/search/{page}"

    full_params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_API_KEY,
        "content-type": "application/json",
        **params
    }

    try:

        if semaphore:
            async with semaphore:
                response = await get_client().get(url, params=full_params)
        else:
            response = await get_client().get(url, params=full_params)

        response.raise_for_status()

        return response.json().get("results", [])

    except Exception as e:

        logging.warning(f"Adzuna fetch failed ({params.get('what') or params.get('title_only')}): {e}")

        return None


async def gather_searches(country: str, param_sets: List[dict]) -> List[List[dict]]:

    # Runs searches in parallel; anything slower than OVERALL_TIMEOUT is dropped
    # so one slow request never blocks the whole response.
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    tasks = [
        asyncio.ensure_future(adzuna_search(country, p, semaphore=semaphore))
        for p in param_sets
    ]

    done, pending = await asyncio.wait(tasks, timeout=OVERALL_TIMEOUT)

    for task in pending:
        task.cancel()

    if pending:
        logging.warning(f"{len(pending)} Adzuna requests timed out")

    batches = [
        task.result() for task in tasks
        if task in done and task.result() is not None
    ]

    if not batches:
        raise AdzunaUnavailable()

    return batches


# =====================================================
# JOB SERIALIZATION
# =====================================================

def stable_job_id(job: dict) -> str:

    source = str(job.get("id") or job.get("redirect_url", ""))

    return str(uuid.uuid5(JOB_ID_NAMESPACE, source))


def build_job(job: dict, match_score: Optional[int]) -> dict:

    return {

        "id": stable_job_id(job),

        "title": job.get("title", ""),

        "company":
        job.get("company", {})
        .get("display_name", "Unknown"),

        "location":
        job.get("location", {})
        .get("display_name", "Remote"),

        "description": clean_description(job.get("description", "")),

        "matchScore": match_score,

        "applyURL": job.get("redirect_url", ""),

        "postedAt": job.get("created"),

        "contractTime": job.get("contract_time")
    }


def posted_timestamp(job: dict) -> float:

    try:
        return datetime.strptime(job.get("postedAt") or "", "%Y-%m-%dT%H:%M:%SZ").timestamp()
    except ValueError:
        return 0


# =====================================================
# FETCH JOBS (RESUME MATCHING)
# =====================================================

async def fetch_jobs(
    query: str,
    country: str = "in",
    limit: int = MAX_JOBS_RETURNED,
    resume_text: str = ""
):

    if not ADZUNA_APP_ID or not ADZUNA_API_KEY:
        logging.error("Missing Adzuna API keys")
        raise AdzunaUnavailable()

    resume_hash = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()
    cache_key = f"match:{country}:{query}:{limit}:{resume_hash}"

    cached = cache_get(cache_key)

    if cached is not None:
        return cached

    # Multiple fallback queries, fetched in parallel
    queries = [

        query,

        "software engineer",

        "software developer",

        "junior software engineer",

        "fresher software engineer",

        "graduate software engineer",

        "entry level software engineer"
    ]

    param_sets = [
        {
            "what": q,
            "results_per_page": 50,
            "max_days_old": MATCH_MAX_DAYS_OLD,
            "sort_by": "date"
        }
        for q in queries
    ]

    batches = await gather_searches(country, param_sets)

    collected_jobs = []
    seen_urls = set()

    # Batches keep query order, so the resume-specific query wins ties
    for results in batches:

        for job in results:

            title = job.get("title", "")
            description_raw = job.get("description", "")
            redirect_url = job.get("redirect_url", "")

            if not redirect_url:
                continue

            if redirect_url in seen_urls:
                continue

            if not is_entry_level(title, description_raw):
                continue

            score = compute_match_score(
                resume_text,
                clean_description(description_raw)
            )

            collected_jobs.append(build_job(job, score))

            seen_urls.add(redirect_url)

    collected_jobs.sort(
        key=lambda x: x["matchScore"],
        reverse=True
    )

    jobs = collected_jobs[:limit]

    cache_set(cache_key, jobs, MATCH_CACHE_TTL)

    return jobs


# =====================================================
# FETCH NEW GRAD JOBS (ALL JOBS FEED)
# =====================================================

async def load_new_grad_feed(country: str) -> List[dict]:

    cache_key = f"newgrad:{country}"

    cached = cache_get(cache_key)

    if cached is not None:
        return cached

    param_sets = [
        {
            "title_only": keyword,
            "category": "it-jobs",
            "max_days_old": MAX_DAYS_OLD,
            "sort_by": "date",
            "results_per_page": 50
        }
        for keyword in NEW_GRAD_TITLE_KEYWORDS
    ]

    try:
        batches = await gather_searches(country, param_sets)
    except AdzunaUnavailable:
        # Serve the last good feed if we have one
        stale = cache_get(cache_key, allow_stale=True)
        if stale is not None:
            return stale
        raise

    feed = []
    seen_ids = set()

    for results in batches:

        for job in results:

            if not job.get("redirect_url"):
                continue

            if is_senior_title(job.get("title", "")):
                continue

            built = build_job(job, None)

            if built["id"] in seen_ids:
                continue

            seen_ids.add(built["id"])
            feed.append(built)

    feed.sort(key=posted_timestamp, reverse=True)

    cache_set(cache_key, feed, NEW_GRAD_CACHE_TTL)

    return feed


async def fetch_new_grad_jobs(
    country: str = "in",
    page: int = 1,
    max_days: Optional[int] = None
) -> dict:

    if not ADZUNA_APP_ID or not ADZUNA_API_KEY:
        logging.error("Missing Adzuna API keys")
        raise AdzunaUnavailable()

    feed = await load_new_grad_feed(country)

    # Date filter runs on the cached feed, so it costs no extra Adzuna calls
    if max_days is not None:
        cutoff = time.time() - max_days * 24 * 60 * 60
        feed = [job for job in feed if posted_timestamp(job) >= cutoff]

    start = (page - 1) * NEW_GRAD_PAGE_SIZE
    end = start + NEW_GRAD_PAGE_SIZE

    return {
        "jobs": feed[start:end],
        "page": page,
        "hasMore": end < len(feed),
        "total": len(feed)
    }
