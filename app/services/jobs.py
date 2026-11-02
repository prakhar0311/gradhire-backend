import os
import re
import time
import uuid
import asyncio
import hashlib
import logging
from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

from app.services import fresher_ai
from app.services.fresher import assess, dedupe, is_broken, is_senior_title  # noqa: F401 (used by tests)
from app.services.skills import extract_skills, skill_gap

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

# Search: Adzuna pages are fetched lazily as the client scrolls
SEARCH_CACHE_TTL = 5 * 60
SEARCH_CACHE_MAX_ENTRIES = 200
SEARCH_ADZUNA_PAGE_SIZE = 50
SEARCH_MAX_ADZUNA_PAGES = 5
# Pages of "query + any fresher word" read before the plain query
SEARCH_FRESHER_PAGES = 2
SEARCH_QUERY_MIN_LENGTH = 2
SEARCH_QUERY_MAX_LENGTH = 100
SEARCH_RETRY_IF_FASTER_THAN = 3.0

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
# FRESHER FILTER (CORE OF GRADHIRE)
# =====================================================
# The rules live in fresher.py; jobs they can't decide go to the AI check
# in fresher_ai.py. Every list (For You, All Jobs, search) goes through
# keep_fresher_jobs.

# Searched in job titles (Adzuna's title_only takes one term per request).
NEW_GRAD_TITLE_KEYWORDS = [
    "graduate",
    "fresher",
    "entry level",
    "junior",
    "trainee",
    "new grad",
    "associate",
    "intern",
    "campus"
]

# Adzuna's what_or: any of these words. Paired with a role in `what`, it
# finds that role's fresher postings.
FRESHER_WORDS = "fresher freshers junior trainee graduate graduates intern entry"

# Note: Adzuna's what_exclude also matches descriptions ("lead the team",
# "report to manager") and drops ~80% of valid roles, so seniority is
# filtered on titles and parsed requirements instead.


def is_entry_level(title: str, description: str, company: str = "") -> bool:
    """False for jobs the rules exclude (unclear ones count as entry level)."""
    return assess(title, description, company).status != "exclude"


async def keep_fresher_jobs(jobs: List[dict], wait: float = fresher_ai.WAIT_SECONDS,
                            seen: Optional[List[dict]] = None) -> List[dict]:
    """The jobs (build_job shape, in order) that suit a fresher, each with
    minYears and batches for the app's label. Broken listings and
    duplicates (also of `seen`, e.g. earlier pages) are dropped too."""

    kept, unclear = [], []

    for job in jobs:
        if is_broken(job):
            continue
        assessment = assess(job["title"], job["description"], job["company"])
        if assessment.status == "exclude":
            continue
        job["minYears"] = assessment.min_years
        job["batches"] = assessment.batches
        kept.append(job)
        if assessment.status == "unclear":
            unclear.append(job)

    if unclear:
        results = await fresher_ai.classify(unclear, wait=wait)
        for job in unclear:
            result = results.get(job["id"])
            if result is None:
                continue
            if fresher_ai.excludes(result):
                job["_drop"] = True
            else:
                job["minYears"] = result["min_years"]
                job["batches"] = result["batches"]
        kept = [job for job in kept if not job.pop("_drop", False)]

    return dedupe(kept, list(seen) if seen is not None else None)


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


# One entry per distinct resume; without a cap these would pile up forever.
# Feed entries are kept even when stale (they're the fallback when Adzuna
# fails), so only match entries are evicted.
MATCH_CACHE_MAX_ENTRIES = 300


def cache_set(key: str, value, ttl: int):

    _cache[key] = (time.time() + ttl, value)

    match_keys = [k for k in _cache if k.startswith("match:")]
    if len(match_keys) > MATCH_CACHE_MAX_ENTRIES:
        now = time.time()
        expired = [k for k in match_keys if _cache[k][0] <= now]
        for k in expired:
            del _cache[k]
        # Still too many: drop the oldest (dicts keep insertion order)
        live = [k for k in match_keys if k in _cache]
        for k in live[:len(live) - MATCH_CACHE_MAX_ENTRIES]:
            del _cache[k]


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

        # Never log the exception text: httpx errors include the request URL,
        # which carries app_id and app_key
        # Only our own feed keywords are logged; search text comes from users
        query = params.get('title_only') or 'search'
        if isinstance(e, httpx.HTTPStatusError):
            reason = f"HTTP {e.response.status_code}"
        else:
            reason = type(e).__name__
        logging.warning(f"Adzuna fetch failed ({query}): {reason}")

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

    source = str(job.get("id") or job.get("redirect_url") or "")

    return str(uuid.uuid5(JOB_ID_NAMESPACE, source))


def build_job(job: dict, match_score: Optional[int]) -> dict:

    return {

        "id": stable_job_id(job),

        "title": job.get("title") or "",

        # Adzuna sometimes sends null for these
        "company":
        (job.get("company") or {})
        .get("display_name") or "Unknown",

        "location":
        (job.get("location") or {})
        .get("display_name") or "Remote",

        "description": clean_description(job.get("description") or ""),

        "matchScore": match_score,

        "applyURL": job.get("redirect_url") or "",

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

def match_cache_key(query: str, country: str, limit: int, resume_text: str) -> str:
    resume_hash = hashlib.sha256(resume_text.encode("utf-8")).hexdigest()
    return f"match:{country}:{query}:{limit}:{resume_hash}"


def is_match_cached(query: str, country: str, resume_text: str) -> bool:
    """True when fetch_jobs would answer from the cache (no Adzuna calls)."""
    return cache_get(match_cache_key(query, country, MAX_JOBS_RETURNED, resume_text)) is not None


async def fetch_jobs(
    query: str,
    country: str = "in",
    limit: int = MAX_JOBS_RETURNED,
    resume_text: str = ""
):

    if not ADZUNA_APP_ID or not ADZUNA_API_KEY:
        logging.error("Missing Adzuna API keys")
        raise AdzunaUnavailable()

    cache_key = match_cache_key(query, country, limit, resume_text)

    cached = cache_get(cache_key)

    if cached is not None:
        return cached

    # The resume's role ("junior backend developer"), the same role with
    # any fresher word, then general fresher software roles; all in IT jobs
    role = re.sub(r"^junior\s+", "", query)
    searches = [
        {"what": query},
        {"what": role, "what_or": FRESHER_WORDS},
        {"what": "fresher software engineer"},
        {"what": "graduate software engineer"},
        {"what": "trainee software engineer"},
        {"what": "entry level software developer"},
        {"what": "junior software developer"},
    ]

    param_sets = [
        {
            **search,
            "category": "it-jobs",
            "results_per_page": 50,
            "max_days_old": MATCH_MAX_DAYS_OLD,
            "sort_by": "date"
        }
        for search in searches
    ]

    batches = await gather_searches(country, param_sets)

    collected_jobs = []
    seen_urls = set()
    resume_skills = extract_skills(resume_text)

    # Batches keep query order, so the resume-specific query wins ties
    for results in batches:

        for job in results:

            title = job.get("title") or ""
            description_raw = job.get("description") or ""
            redirect_url = job.get("redirect_url") or ""

            if not redirect_url:
                continue

            if redirect_url in seen_urls:
                continue

            # Cheap rules first; the AI check runs on the best matches only
            if not is_entry_level(title, description_raw, (job.get("company") or {}).get("display_name") or ""):
                continue

            score = compute_match_score(
                resume_text,
                clean_description(description_raw)
            )

            built = build_job(job, score)

            # Skills the job asks for that the resume has / doesn't have
            matched, missing = skill_gap(resume_skills, f"{title} {description_raw}")
            built["matchedSkills"] = matched
            built["missingSkills"] = missing

            collected_jobs.append(built)

            seen_urls.add(redirect_url)

    collected_jobs.sort(
        key=lambda x: x["matchScore"],
        reverse=True
    )

    # Enough candidates that excluded ones still leave a full list
    jobs = (await keep_fresher_jobs(collected_jobs[:limit * 3]))[:limit]

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

            built = build_job(job, None)

            if built["id"] in seen_ids:
                continue

            seen_ids.add(built["id"])
            feed.append(built)

    feed.sort(key=posted_timestamp, reverse=True)
    feed = await keep_fresher_jobs(feed)

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


# =====================================================
# SEARCH (ALL JOBS)
# =====================================================

_SEARCH_DISALLOWED = re.compile(r"[^\w\s+#./-]")


def clean_search_query(raw: str) -> Optional[str]:

    # Letters, digits, spaces and + # . - / only (keeps "C++", "C#", "Node.js").
    # Returns None when too little is left to search for.
    text = _SEARCH_DISALLOWED.sub(" ", raw or "").replace("_", " ")
    text = " ".join(text.split())[:SEARCH_QUERY_MAX_LENGTH].strip()

    if len(text) < SEARCH_QUERY_MIN_LENGTH:
        return None

    return text


class _SearchState:

    def __init__(self):
        self.jobs: List[dict] = []
        self.seen_ids = set()
        # Fresher postings of the query first, then the plain query
        self.phase = 0
        self.next_page = 1
        self.exhausted = False
        self.expires_at = time.time() + SEARCH_CACHE_TTL
        self.lock = asyncio.Lock()


_searches: "OrderedDict[str, _SearchState]" = OrderedDict()


def _search_state(key: str) -> _SearchState:

    state = _searches.get(key)

    if state is None or time.time() >= state.expires_at:
        state = _SearchState()
        _searches[key] = state

    _searches.move_to_end(key)

    while len(_searches) > SEARCH_CACHE_MAX_ENTRIES:
        _searches.popitem(last=False)

    return state


async def _fetch_next_search_page(country: str, query: str, max_days: int, state: _SearchState) -> bool:

    # Returns False when Adzuna failed (the state is left as it was)
    # Adzuna's default (relevance) order: sorting by date buries title matches
    params = {
        "what": query,
        "max_days_old": max_days,
        "results_per_page": SEARCH_ADZUNA_PAGE_SIZE
    }
    if state.phase == 0:
        params["what_or"] = FRESHER_WORDS

    results = None

    # Adzuna sometimes answers with a quick 503; a fast failure gets one retry
    for attempt in range(2):
        started = time.monotonic()
        try:
            results = await asyncio.wait_for(
                adzuna_search(country, params, page=state.next_page),
                timeout=OVERALL_TIMEOUT
            )
        except asyncio.TimeoutError:
            logging.warning("Adzuna search timed out")
            break
        if results is not None or time.monotonic() - started > SEARCH_RETRY_IF_FASTER_THAN:
            break
        await asyncio.sleep(0.4)

    if results is None:
        return False

    words = query.lower().split()
    batch = []

    for job in results:

        if not job.get("redirect_url"):
            continue

        built = build_job(job, None)

        if built["id"] in state.seen_ids:
            continue

        state.seen_ids.add(built["id"])
        batch.append(built)

    # Same fresher rules as matching; duplicates of earlier pages are dropped
    batch = await keep_fresher_jobs(batch, seen=state.jobs)

    # Within each batch: title matches first, then company matches, newest first
    def rank(job: dict):
        title = job["title"].lower()
        company = job["company"].lower()
        if all(word in title for word in words):
            tier = 0
        elif all(word in company for word in words):
            tier = 1
        elif any(word in title for word in words):
            tier = 2
        else:
            tier = 3
        return (tier, -posted_timestamp(job))

    state.jobs.extend(sorted(batch, key=rank))
    state.next_page += 1

    max_pages = SEARCH_FRESHER_PAGES if state.phase == 0 else SEARCH_MAX_ADZUNA_PAGES
    if len(results) < SEARCH_ADZUNA_PAGE_SIZE or state.next_page > max_pages:
        if state.phase == 0:
            state.phase, state.next_page = 1, 1
        else:
            state.exhausted = True

    return True


async def search_new_grad_jobs(
    query: str,
    country: str = "in",
    page: int = 1,
    max_days: Optional[int] = None
) -> dict:

    if not ADZUNA_APP_ID or not ADZUNA_API_KEY:
        logging.error("Missing Adzuna API keys")
        raise AdzunaUnavailable()

    days = max_days or MAX_DAYS_OLD
    state = _search_state(f"{country}:{days}:{query.lower()}")

    start = (page - 1) * NEW_GRAD_PAGE_SIZE
    end = start + NEW_GRAD_PAGE_SIZE
    failed = False

    async with state.lock:
        # One job past this page tells us whether another page exists
        while len(state.jobs) <= end and not state.exhausted:
            if not await _fetch_next_search_page(country, query, days, state):
                failed = True
                break

    page_jobs = state.jobs[start:end]

    if failed and not page_jobs:
        raise AdzunaUnavailable()

    return {
        "jobs": page_jobs,
        "page": page,
        # After a failed fetch, let the client try the next page again
        "hasMore": len(state.jobs) > end or (failed and not state.exhausted),
        "total": len(state.jobs),
        "totalIsExact": state.exhausted
    }
