import os
import logging
from typing import Optional

from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions


# =====================================================
# SUPABASE CLIENT (BACKEND ONLY)
# =====================================================
# Uses the SECRET (service_role) key, which bypasses RLS. It must only ever
# live in the backend's environment (.env locally, Render in production),
# never in the iOS app.

REQUEST_TIMEOUT_SECONDS = 10

_client: Optional[Client] = None


def is_configured() -> bool:

    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SECRET_KEY"))


def get_supabase() -> Optional[Client]:
    """Returns the shared client, or None when Supabase isn't configured."""

    global _client

    if _client is None and is_configured():

        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SECRET_KEY"],
            options=SyncClientOptions(
                postgrest_client_timeout=REQUEST_TIMEOUT_SECONDS,
                storage_client_timeout=REQUEST_TIMEOUT_SECONDS
            )
        )

    return _client


# =====================================================
# HEALTH CHECK
# =====================================================

def check_connection() -> dict:
    """Reads one row from jobs with the secret key.

    Returns {"ok": bool, "reason": str}. The reason never contains secrets.
    """

    client = get_supabase()

    if client is None:
        return {"ok": False, "reason": "not configured"}

    try:
        client.table("jobs").select("id").limit(1).execute()
        return {"ok": True, "reason": "jobs table reachable"}

    except Exception as e:

        message = str(e)
        code = getattr(e, "code", "") or ""

        if code == "42501" or "permission denied" in message:
            reason = "permission denied (check the service_role grants)"
        elif code in ("42P01", "PGRST205") or "does not exist" in message or "Could not find the table" in message:
            reason = "jobs table missing (push the migrations)"
        elif "Invalid API key" in message or "401" in message:
            reason = "invalid secret key"
        else:
            reason = "unreachable"

        logging.warning(f"Supabase health check failed: {reason}")

        return {"ok": False, "reason": reason}
