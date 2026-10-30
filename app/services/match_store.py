import uuid
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from app.services.account import verify_user, AccountError
from app.services.job_store import save_served_jobs
from app.services.supabase_client import get_supabase


# =====================================================
# SAVED MATCHES (signed-in users with a saved resume)
# =====================================================
# After /jobs/from-resume or /jobs/for-resume, the matches for a resume the
# user saved to their account are merged into job_matches: a job scored once
# keeps its score, new jobs are added, and matches older than
# MATCH_MAX_AGE_DAYS are dropped. The resume's readiness score is then updated
# from what's stored. The dashboard and the For You tab read both.
#
# Readiness = the average match score of the resume's top 10 matches, using
# the same scoring as the For You list.

READINESS_TOP_N = 10
MATCH_MAX_AGE_DAYS = 30


class MatchStoreError(Exception):

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message


def readiness_score(scores: List[int]) -> Optional[int]:

    top = sorted(scores, reverse=True)[:READINESS_TOP_N]
    if not top:
        return None

    return max(0, min(100, round(sum(top) / len(top))))


def _valid_uuid(value: str) -> bool:

    try:
        uuid.UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def save_matches(access_token: str, resume_id: str, jobs: List[dict]) -> None:
    """Background task; never raises. Only stores matches for a resume the
    token's user owns."""

    client = get_supabase()
    if client is None or not _valid_uuid(resume_id):
        save_served_jobs(jobs)
        return

    try:
        user = verify_user(access_token)
    except AccountError:
        save_served_jobs(jobs)
        return

    try:
        owned = (
            client.table("resumes")
            .select("id")
            .eq("id", resume_id)
            .eq("user_id", user.id)
            .limit(1)
            .execute()
            .data
        )
        if not owned:
            save_served_jobs(jobs)
            return

        # job_matches.job_id must point at a saved job
        save_served_jobs(jobs)
        ids = [job["id"] for job in jobs if job.get("id")]
        existing = {
            row["id"]
            for row in client.table("jobs").select("id").in_("id", ids).execute().data
        } if ids else set()

        rows = [
            {
                "resume_id": resume_id,
                "job_id": job["id"],
                "match_score": int(job["matchScore"]),
                "matched_skills": job.get("matchedSkills") or [],
                "missing_skills": job.get("missingSkills") or [],
            }
            for job in jobs
            if job.get("id") in existing and job.get("matchScore") is not None
        ]

        # Jobs scored before keep their stored score
        if rows:
            client.table("job_matches").upsert(
                rows, on_conflict="resume_id,job_id", ignore_duplicates=True
            ).execute()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=MATCH_MAX_AGE_DAYS)).isoformat()
        client.table("job_matches").delete().eq("resume_id", resume_id).lt("created_at", cutoff).execute()

        stored = (
            client.table("job_matches")
            .select("match_score")
            .eq("resume_id", resume_id)
            .execute()
            .data
        )
        score = readiness_score([row["match_score"] for row in stored])
        if score is not None:
            client.table("resumes").update({"readiness_score": score}).eq("id", resume_id).execute()

    except Exception as e:
        logging.warning(f"Saving matches failed: {type(e).__name__}")


def load_resume_for_matching(user_id: str, resume_id: str):
    """Returns (resume text, {job_id: stored match}) for one of the user's
    resumes. Raises MatchStoreError if it isn't theirs or has no text."""

    client = get_supabase()
    if client is None:
        raise MatchStoreError(503, "Matching is temporarily unavailable. Please try again.")

    if not _valid_uuid(resume_id):
        raise MatchStoreError(404, "Resume not found.")

    rows = (
        client.table("resumes")
        .select("id,parsed_data")
        .eq("id", resume_id)
        .eq("user_id", str(user_id))
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        raise MatchStoreError(404, "Resume not found.")

    parsed = rows[0].get("parsed_data") or {}
    text = parsed.get("text") if isinstance(parsed, dict) else None
    if not text or not text.strip():
        raise MatchStoreError(422, "This resume has no saved text. Upload it again to get matches.")

    stored: Dict[str, dict] = {
        row["job_id"]: row
        for row in client.table("job_matches")
        .select("job_id,match_score,matched_skills,missing_skills")
        .eq("resume_id", resume_id)
        .execute()
        .data
    }

    return text, stored
