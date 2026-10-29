import logging
from typing import Optional

from app.services.supabase_client import get_supabase
from app.services.apple_auth import revoke_with_authorization_code


# =====================================================
# ACCOUNT DELETION
# =====================================================
# Deletes everything a user owns:
#   1. revokes their Apple tokens (Sign in with Apple users)
#   2. deletes their files under resumes/{user_id}/ in Storage
#   3. deletes the auth user; every user-owned table row cascades from it
#      (profiles, resumes, job_matches, applications, application_events,
#      device_tokens). Shared jobs rows are kept.

RESUMES_BUCKET = "resumes"
STORAGE_PAGE_SIZE = 100


class AccountError(Exception):

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def verify_user(access_token: str):
    """Returns the Supabase user for a valid access token, or raises 401."""

    client = get_supabase()

    if client is None:
        raise AccountError(503, "Account service is not configured.")

    try:
        response = client.auth.get_user(access_token)
    except Exception:
        raise AccountError(401, "Your session has expired. Please sign in again.")

    if response is None or response.user is None:
        raise AccountError(401, "Your session has expired. Please sign in again.")

    return response.user


def delete_user_files(user_id: str) -> int:
    """Deletes every file in the user's folder. Returns how many were removed."""

    bucket = get_supabase().storage.from_(RESUMES_BUCKET)
    removed = 0

    while True:
        items = bucket.list(user_id, {"limit": STORAGE_PAGE_SIZE, "offset": 0})
        paths = [f"{user_id}/{item['name']}" for item in items if item.get("name")]

        if not paths:
            return removed

        bucket.remove(paths)
        removed += len(paths)


def delete_account(access_token: str, apple_authorization_code: Optional[str]) -> dict:

    user = verify_user(access_token)
    user_id = user.id

    providers = (user.app_metadata or {}).get("providers") or []
    apple_revoked = None

    if "apple" in providers:
        if apple_authorization_code:
            apple_revoked = revoke_with_authorization_code(apple_authorization_code)
        else:
            apple_revoked = False
            logging.warning("Apple user deleted without an authorization code; token not revoked")

    # Files first: if this fails the account still exists and the user can retry
    try:
        files_removed = delete_user_files(user_id)
    except Exception as e:
        logging.error(f"Account deletion: removing files failed: {type(e).__name__}")
        raise AccountError(500, "Could not delete your account. Please try again.")

    try:
        get_supabase().auth.admin.delete_user(user_id)
    except Exception as e:
        logging.error(f"Account deletion: deleting auth user failed: {type(e).__name__}")
        raise AccountError(500, "Could not delete your account. Please try again.")

    return {
        "deleted": True,
        "files_removed": files_removed,
        "apple_token_revoked": apple_revoked
    }
