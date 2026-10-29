import re
import time
from urllib.parse import urlparse
import uuid
import logging
import threading
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from app.services.jobs import JOB_ID_NAMESPACE
from app.services.supabase_client import get_supabase


# =====================================================
# SHARED JOBS TABLE
# =====================================================
# The app tracks jobs by the stable id the backend gives them, and
# applications.job_id must point at a row in public.jobs. Only the backend
# writes that table:
#   - every job the backend serves is upserted in the background
#     (save_served_jobs), so the row usually exists before anyone tracks it
#   - POST /jobs/ensure inserts one job the app is about to track, but only
#     when its id provably came from our feed, and never overwrites a row

SOURCE = "adzuna"

# Adzuna ad id inside the apply URL, e.g. https://www.adzuna.in/details/5945565010
ADZUNA_AD_ID = re.compile(r"/(?:details|land/ad)/(\d+)")
ADZUNA_HOST = re.compile(r"^(?:www\.)?adzuna\.[a-z]{2,3}(?:\.[a-z]{2})?$")

DESCRIPTION_MAX = 4000
TEXT_MAX = 300
URL_MAX = 2048

# Feeds are cached and re-served for 30 minutes; don't rewrite the same rows
# on every request.
RESAVE_AFTER_SECONDS = 6 * 60 * 60
UPSERT_BATCH = 100

_last_saved: Dict[str, float] = {}
_last_saved_lock = threading.Lock()


def external_id_for(job_id: str, apply_url: str) -> Optional[str]:
    """Returns the Adzuna id behind a job id, or None if the id doesn't
    match the URL (i.e. it wasn't produced by our feed)."""

    candidates = []

    match = ADZUNA_AD_ID.search(apply_url or "")
    if match:
        candidates.append(match.group(1))

    # stable_job_id falls back to the redirect URL when Adzuna has no id
    if apply_url:
        candidates.append(apply_url)

    for candidate in candidates:
        if str(uuid.uuid5(JOB_ID_NAMESPACE, candidate)) == str(job_id).lower():
            return candidate

    return None


def _clip(value, limit: int) -> Optional[str]:

    if not isinstance(value, str):
        return None

    value = value.strip()
    return value[:limit] if value else None


def _posted_at(value) -> Optional[str]:

    if not isinstance(value, str) or not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.isoformat()


def job_row(job: dict) -> Optional[dict]:
    """Maps an API job (as served to the app) to a public.jobs row."""

    job_id = str(job.get("id") or "")
    apply_url = _clip(job.get("applyURL"), URL_MAX)
    title = _clip(job.get("title"), TEXT_MAX)

    if not job_id or not apply_url or not title:
        return None

    parsed = urlparse(apply_url)
    if parsed.scheme != "https" or not ADZUNA_HOST.match(parsed.hostname or ""):
        return None

    external_id = external_id_for(job_id, apply_url)
    if external_id is None:
        return None

    return {
        "id": job_id.lower(),
        "source": SOURCE,
        "external_id": external_id,
        "title": title,
        "company": _clip(job.get("company"), TEXT_MAX),
        "location": _clip(job.get("location"), TEXT_MAX),
        "description": _clip(job.get("description"), DESCRIPTION_MAX),
        "apply_url": apply_url,
        "posted_at": _posted_at(job.get("postedAt")),
    }


# =====================================================
# BACKGROUND SAVE OF SERVED JOBS
# =====================================================

def save_served_jobs(jobs: Iterable[dict], is_new_grad: bool = False) -> int:
    """Upserts served jobs into public.jobs. Runs as a background task, so it
    never raises. Returns how many rows were sent."""

    client = get_supabase()
    if client is None:
        return 0

    now = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows: List[dict] = []

    with _last_saved_lock:
        for job in jobs:
            row = job_row(job)
            if row is None:
                continue
            if now - _last_saved.get(row["id"], 0) < RESAVE_AFTER_SECONDS:
                continue

            row["last_seen_at"] = now_iso
            row["is_active"] = True
            # Only the new-grad feed knows a job is new-grad; leave the flag
            # alone for jobs that came from resume matching.
            if is_new_grad:
                row["is_new_grad"] = True

            rows.append(row)
            _last_saved[row["id"]] = now

    if not rows:
        return 0

    # Every row in one request must have the same columns
    sent = 0
    try:
        for start in range(0, len(rows), UPSERT_BATCH):
            batch = rows[start:start + UPSERT_BATCH]
            client.table("jobs").upsert(
                batch,
                on_conflict="source,external_id"
            ).execute()
            sent += len(batch)
    except Exception as e:
        # Forget the failed rows so the next request retries them
        with _last_saved_lock:
            for row in rows[sent:]:
                _last_saved.pop(row["id"], None)
        logging.warning(f"Saving served jobs failed: {type(e).__name__}")

    return sent


# =====================================================
# ENSURE ONE JOB EXISTS (before the app tracks it)
# =====================================================

class JobStoreError(Exception):

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def ensure_job(job: dict) -> dict:
    """Inserts the job if it isn't in public.jobs yet. Existing rows are
    never changed, so a client can't rewrite shared job data."""

    client = get_supabase()
    if client is None:
        raise JobStoreError(503, "Job tracking isn't available right now.")

    row = job_row(job)
    if row is None:
        raise JobStoreError(400, "This job can't be tracked.")

    try:
        client.table("jobs").upsert(
            row,
            on_conflict="source,external_id",
            ignore_duplicates=True
        ).execute()
    except Exception as e:
        logging.warning(f"Ensuring job failed: {type(e).__name__}")
        raise JobStoreError(503, "Couldn't save this job. Please try again.")

    return {"job_id": row["id"]}
