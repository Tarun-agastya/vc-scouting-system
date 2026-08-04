"""
Shared Gmail OAuth2 authentication (Phase PM, 4 Aug 2026).

Factored out of newsletter_ingestor.py so it and press_monitor/emailer.py
share ONE token/consent flow instead of two independent ones that could
drift out of sync or fight over the same credentials/token.json. SCOPES
covers both read (newsletter ingestion) and send (press-monitor digest) —
completing the interactive consent once (a browser window, one click)
grants both.

Switched to (from SMTP + an app password) after the app-password path
proved unreliable for the press-monitor's Gmail account even with valid,
freshly-generated credentials — Google's risk-scoring on programmatic
basic-auth sign-in is independent of whether anything is actually
misconfigured, and the OAuth flow this module uses is what Google itself
recommends over app passwords.
"""
import logging
import os

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_PATH = os.path.join(_PROJECT_ROOT, "credentials", "token.json")


def get_gmail_service():
    """
    Returns an authenticated Gmail API service client.

    Runs the interactive OAuth consent flow (opens a local server + browser
    window) on first use, or after a token that can no longer be silently
    refreshed — same behaviour newsletter_ingestor.py always had, now
    shared. Never call this from unattended/headless code expecting it to
    succeed without a human available to click through the browser consent
    at least once.
    """
    from config import settings
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as refresh_err:
                logger.error(
                    "[Gmail] Token refresh failed — deleting expired token. "
                    f"Details: {refresh_err}"
                )
                if os.path.exists(TOKEN_PATH):
                    os.remove(TOKEN_PATH)
                raise RuntimeError(
                    "Gmail OAuth token expired and could not be refreshed. "
                    "Delete credentials/token.json and restart to re-authenticate."
                ) from refresh_err
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.gmail_credentials_path, SCOPES
            )
            creds = flow.run_local_server(port=0)

        os.makedirs(os.path.join(_PROJECT_ROOT, "credentials"), exist_ok=True)
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)
