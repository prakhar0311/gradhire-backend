import uuid
import logging
from typing import List, Optional

from app.services.account import verify_user, AccountError
from app.services.job_store import save_served_jobs
from app.services.supabase_client import get_supabase


# =====================================================
# SAVED MATCHES (signed-in users with a saved resume)
# =====================================================
# After /jobs/from-resume, the matches for a resume the user saved to their
# account are stored in job_matches (replacing the previous set), and the
# resume's readiness score is updated. The dashboard reads both.
#
# Readiness = the average match score of the resume's top 10 matches, using
# the same scoring as the For You list.

READINESS_TOP_N = 10


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

        # Replace the previous set, so the dashboard reflects this run
        client.table("job_matches").delete().eq("resume_id", resume_id).execute()
        if rows:
            client.table("job_matches").insert(rows).execute()

        score = readiness_score([row["match_score"] for row in rows])
        if score is not None:
            client.table("resumes").update({"readiness_score": score}).eq("id", resume_id).execute()

    except Exception as e:
        logging.warning(f"Saving matches failed: {type(e).__name__}")
