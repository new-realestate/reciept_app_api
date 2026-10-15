"""One-off helper: run the Google OAuth consent flow and print a Gmail refresh token.

Usage (from reciept_app_api/):
    uv run --with google-auth-oauthlib python scripts/gmail_authorize.py

Reads GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET from the environment or .env, opens a
browser for you to sign in as the sending Gmail account, then prints the refresh
token to store as GMAIL_REFRESH_TOKEN. The OAuth client must be a "Desktop app" client.
"""

import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    _load_dotenv()
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET (env or .env) first.", file=sys.stderr)
        return 1

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        print("No refresh token returned; revoke the app's access in your Google account and retry.", file=sys.stderr)
        return 1
    print("\nGMAIL_REFRESH_TOKEN=" + creds.refresh_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
