import logging
import uuid
from typing import Optional

from app.services.supabase_client import get_supabase


# =====================================================
# STORED ANALYSES (signed-in users)
# =====================================================
# The Optimize screen's result for one (resume, job), so reopening it is
# instant, shows the same numbers and costs no AI call. Each row records
# the hashes of the resume and job text it was made from; a changed resume
# or posting is analysed again. Only the backend writes these rows; users
# can read and delete their own (RLS).

def _valid_uuid(value: Optional[str]) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError):
        return False


def owns_resume(user_id: str, resume_id: str) -> bool:
    client = get_supabase()
    if client is None or not _valid_uuid(resume_id):
        return False
    try:
        rows = (
            client.table("resumes").select("id")
            .eq("id", resume_id).eq("user_id", str(user_id))
            .limit(1).execute().data
        )
        return bool(rows)
    except Exception as e:
        logging.warning(f"Resume ownership check failed: {type(e).__name__}")
        return False


def load(user_id: str, resume_id: str, job_id: str, resume_hash: str, job_hash: str, version: str) -> Optional[dict]:
    client = get_supabase()
    if client is None or not (_valid_uuid(resume_id) and _valid_uuid(job_id)):
        return None
    try:
        rows = (
            client.table("resume_analyses")
            .select("payload,resume_hash,job_hash,version")
            .eq("user_id", str(user_id)).eq("resume_id", resume_id).eq("job_id", job_id)
            .limit(1).execute().data
        )
    except Exception as e:
        logging.warning(f"Loading stored analysis failed: {type(e).__name__}")
        return None
    if not rows:
        return None
    row = rows[0]
    if row.get("resume_hash") != resume_hash or row.get("job_hash") != job_hash or row.get("version") != version:
        return None
    return row.get("payload")


def save(user_id: str, resume_id: str, job_id: str, resume_hash: str, job_hash: str, version: str, payload: dict) -> None:
    """Background task; never raises."""
    client = get_supabase()
    if client is None or not (_valid_uuid(resume_id) and _valid_uuid(job_id)):
        return
    try:
        client.table("resume_analyses").upsert({
            "user_id": str(user_id),
            "resume_id": resume_id,
            "job_id": job_id,
            "resume_hash": resume_hash,
            "job_hash": job_hash,
            "version": version,
            "payload": payload,
        }, on_conflict="resume_id,job_id").execute()
    except Exception as e:
        # e.g. the job isn't in the shared table yet; the in-memory cache still has it
        logging.warning(f"Saving analysis failed: {type(e).__name__}")


# =====================================================
# STORED RESUME STRUCTURE (resumes.parsed_data.structure)
# =====================================================
# The sections read from a saved resume, kept with it so every analysis and
# PDF uses the same structure (no re-reading, no drift). structure_hash ties
# it to the exact text and analysis version.

def load_structure(user_id: str, resume_id: str, structure_hash: str) -> Optional[dict]:
    client = get_supabase()
    if client is None or not _valid_uuid(resume_id):
        return None
    try:
        rows = (
            client.table("resumes").select("parsed_data")
            .eq("id", resume_id).eq("user_id", str(user_id))
            .limit(1).execute().data
        )
    except Exception as e:
        logging.warning(f"Loading stored structure failed: {type(e).__name__}")
        return None
    parsed = (rows[0].get("parsed_data") if rows else None) or {}
    if isinstance(parsed, dict) and parsed.get("structure_hash") == structure_hash and isinstance(parsed.get("structure"), dict):
        return parsed["structure"]
    return None


def save_structure(user_id: str, resume_id: str, structure_hash: str, structure: dict) -> None:
    """Background task; never raises. Keeps the existing parsed_data fields."""
    client = get_supabase()
    if client is None or not _valid_uuid(resume_id):
        return
    try:
        rows = (
            client.table("resumes").select("parsed_data")
            .eq("id", resume_id).eq("user_id", str(user_id))
            .limit(1).execute().data
        )
        if not rows:
            return
        parsed = rows[0].get("parsed_data") or {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed.update({"structure": structure, "structure_hash": structure_hash})
        client.table("resumes").update({"parsed_data": parsed}).eq("id", resume_id).eq("user_id", str(user_id)).execute()
    except Exception as e:
        logging.warning(f"Saving structure failed: {type(e).__name__}")
