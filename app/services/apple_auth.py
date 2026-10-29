import os
import time
import logging

import httpx
import jwt


# =====================================================
# SIGN IN WITH APPLE: TOKEN REVOCATION
# =====================================================
# Apple requires apps that offer Sign in with Apple to revoke the user's
# Apple tokens when they delete their account. The app sends a fresh
# authorization code; we exchange it for a token and revoke that token.
#
# Needs (backend environment only):
#   APPLE_TEAM_ID              e.g. 9U3N995J6M
#   APPLE_CLIENT_ID            the app's bundle ID
#   APPLE_SIGNIN_KEY_ID        Key ID of the Sign in with Apple key
#   APPLE_SIGNIN_PRIVATE_KEY   contents of the .p8 file (newlines or \n)

APPLE_BASE_URL = "https://appleid.apple.com"
REQUEST_TIMEOUT_SECONDS = 10
CLIENT_SECRET_LIFETIME_SECONDS = 300


def is_configured() -> bool:

    return all(os.getenv(name) for name in (
        "APPLE_TEAM_ID",
        "APPLE_CLIENT_ID",
        "APPLE_SIGNIN_KEY_ID",
        "APPLE_SIGNIN_PRIVATE_KEY"
    ))


def make_client_secret(now: int = None) -> str:
    """Short-lived ES256 JWT that authenticates us to Apple's token endpoints."""

    now = now or int(time.time())

    # Render and .env files may store the .p8 with literal "\n"
    private_key = os.environ["APPLE_SIGNIN_PRIVATE_KEY"].replace("\\n", "\n")

    return jwt.encode(
        {
            "iss": os.environ["APPLE_TEAM_ID"],
            "iat": now,
            "exp": now + CLIENT_SECRET_LIFETIME_SECONDS,
            "aud": APPLE_BASE_URL,
            "sub": os.environ["APPLE_CLIENT_ID"]
        },
        private_key,
        algorithm="ES256",
        headers={"kid": os.environ["APPLE_SIGNIN_KEY_ID"]}
    )


def revoke_with_authorization_code(authorization_code: str) -> bool:
    """Exchanges a fresh authorization code for a token and revokes it.

    Returns True when Apple confirms the revocation. Never raises; failures
    are logged without the code or tokens.
    """

    if not is_configured():
        logging.warning("Apple revocation skipped: Sign in with Apple key not configured")
        return False

    try:
        client_id = os.environ["APPLE_CLIENT_ID"]
        client_secret = make_client_secret()

        response = httpx.post(
            f"{APPLE_BASE_URL}/auth/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": authorization_code,
                "grant_type": "authorization_code"
            },
            timeout=REQUEST_TIMEOUT_SECONDS
        )

        if response.status_code != 200:
            logging.warning(f"Apple token exchange failed: HTTP {response.status_code}")
            return False

        tokens = response.json()

        if tokens.get("refresh_token"):
            token, hint = tokens["refresh_token"], "refresh_token"
        elif tokens.get("access_token"):
            token, hint = tokens["access_token"], "access_token"
        else:
            logging.warning("Apple token exchange returned no token")
            return False

        response = httpx.post(
            f"{APPLE_BASE_URL}/auth/revoke",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "token": token,
                "token_type_hint": hint
            },
            timeout=REQUEST_TIMEOUT_SECONDS
        )

        if response.status_code != 200:
            logging.warning(f"Apple token revocation failed: HTTP {response.status_code}")
            return False

        return True

    except Exception as e:

        logging.warning(f"Apple token revocation error: {type(e).__name__}")
        return False
