#!/usr/bin/env python3
"""
One-time CLI helper to authenticate your personal Google Calendar account
and generate `token.json` so the SWE Mock Interview Platform can automatically
create 1-on-1 Google Meet links on your free Gmail account.

Usage:
1. Download your OAuth 2.0 Client ID JSON from Google Cloud Console (Desktop App)
   and save it as `credentials.json` in the project root.
2. Run:
   python3 scripts/setup_google_oauth.py
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.config import settings
from app.services.calendar_meet import SCOPES


def main() -> None:
    if not settings.GOOGLE_CLIENT_SECRETS_FILE.exists():
        print(f"ERROR: Could not find {settings.GOOGLE_CLIENT_SECRETS_FILE}")
        print(
            "Please download your OAuth 2.0 Desktop Client JSON from Google Cloud Console\n"
            "and place it at 'credentials.json' in the project root."
        )
        sys.exit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.GOOGLE_CLIENT_SECRETS_FILE), SCOPES
    )
    creds = flow.run_local_server(port=0)
    settings.GOOGLE_TOKEN_FILE.write_text(creds.to_json())
    print(f"Success! Saved Google Calendar OAuth token to: {settings.GOOGLE_TOKEN_FILE}")


if __name__ == "__main__":
    main()
