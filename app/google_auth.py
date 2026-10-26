"""OAuth for Gmail and Drive.

The desktop client lives in credentials.json. The refresh token lives in
token.json. Neither file is logged or printed.
"""
import logging
import os
import warnings
from typing import Any

from app.db import ROOT

logger = logging.getLogger("app.google")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.file",
]

CREDENTIALS_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "token.json"


class GoogleAuthError(RuntimeError):
    def __init__(self, code):
        # type: (str) -> None
        self.code = code
        super(GoogleAuthError, self).__init__(code)


def token_ready():
    # type: () -> bool
    return TOKEN_FILE.is_file()


def _save(creds):
    # type: (Any) -> None
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(str(TOKEN_FILE), 0o600)
    except OSError:
        pass


def _quiet_loggers():
    # type: () -> None
    warnings.filterwarnings("ignore", category=FutureWarning)
    warnings.filterwarnings("ignore", module="urllib3")
    for name in (
        "googleapiclient",
        "google.auth",
        "google_auth_oauthlib",
        "oauthlib",
        "urllib3",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)


def load_credentials():
    # type: () -> Any
    """Stored credentials, with a silent refresh when the access token expired."""
    _quiet_loggers()
    if not TOKEN_FILE.is_file():
        raise GoogleAuthError("not_authorized")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        raise GoogleAuthError("google_libraries_missing")

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    except (ValueError, OSError):
        raise GoogleAuthError("not_authorized")
    if creds is None:
        raise GoogleAuthError("not_authorized")
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            logger.error("token refresh failed")
            raise GoogleAuthError("not_authorized")
        _save(creds)
        return creds
    raise GoogleAuthError("not_authorized")


def authorize_interactive():
    # type: () -> None
    """Open the local browser once and save token.json."""
    _quiet_loggers()
    if not CREDENTIALS_FILE.is_file():
        raise GoogleAuthError("credentials_missing")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise GoogleAuthError("google_libraries_missing")

    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            browser="open -a 'Google Chrome' %s",
            authorization_prompt_message="",
            access_type="offline",
            prompt="consent",
        )
    except GoogleAuthError:
        raise
    except Exception:
        logger.error("oauth flow failed")
        raise GoogleAuthError("authorization_failed")
    if creds is None or not creds.valid:
        raise GoogleAuthError("authorization_failed")
    _save(creds)


def gmail_service():
    # type: () -> Any
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=load_credentials())


def drive_service():
    # type: () -> Any
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=load_credentials())
