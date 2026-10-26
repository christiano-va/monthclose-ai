"""Gmail calls used by the mailbox, the seeder and owner drafts.

Message listing always receives an explicit query. Drafts are created and
never sent. users.messages.insert stores a copy; it does not send mail.
"""
import base64
import logging
from email.message import EmailMessage
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger("app.gmail")

LABEL_MONTHCLOSE = "monthclose"
LABEL_PROCESSED = "monthclose-processed"


def _decode_raw(value):
    # type: (str) -> bytes
    text = (value or "").encode("ascii")
    padding = b"=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _encode_raw(raw):
    # type: (bytes) -> str
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _http_status(exc):
    # type: (Exception) -> str
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    return str(status or "")


def ensure_label(service, name):
    # type: (Any, str) -> str
    """Return the label id, creating the label when it is missing."""
    found = label_id(service, name)
    if found:
        return found
    try:
        created = (
            service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            .execute()
        )
    except Exception as exc:
        logger.error("label create failed http %s", _http_status(exc))
        found = label_id(service, name)
        if found:
            return found
        raise
    label = created.get("id")
    if not label:
        raise RuntimeError("label_missing")
    return label


def label_id(service, name):
    # type: (Any, str) -> Optional[str]
    response = service.users().labels().list(userId="me").execute()
    for label in response.get("labels") or []:
        if label.get("name") == name:
            return label.get("id")
    return None


def iter_message_ids(service, query):
    # type: (Any, str) -> Iterator[str]
    """List ids that match `query`. An empty query is refused."""
    text = (query or "").strip()
    if not text:
        raise RuntimeError("empty_gmail_query")
    page = None  # type: Optional[str]
    while True:
        kwargs = {"userId": "me", "q": text, "maxResults": 50}  # type: Dict[str, Any]
        if page:
            kwargs["pageToken"] = page
        response = service.users().messages().list(**kwargs).execute()
        for item in response.get("messages") or []:
            msg_id = item.get("id")
            if msg_id:
                yield msg_id
        page = response.get("nextPageToken")
        if not page:
            break


def fetch_raw(service, gmail_id):
    # type: (Any, str) -> bytes
    response = (
        service.users()
        .messages()
        .get(userId="me", id=gmail_id, format="raw")
        .execute()
    )
    return _decode_raw(response.get("raw") or "")


def add_label(service, gmail_id, label_id_value):
    # type: (Any, str, str) -> None
    (
        service.users()
        .messages()
        .modify(
            userId="me",
            id=gmail_id,
            body={"addLabelIds": [label_id_value]},
        )
        .execute()
    )


def find_by_message_id(service, message_id):
    # type: (Any, str) -> Optional[str]
    """Look up one RFC Message-ID. Does not list the rest of the mailbox."""
    bare = (message_id or "").strip()
    if bare.startswith("<") and bare.endswith(">"):
        bare = bare[1:-1].strip()
    bare = bare.replace('"', "").strip()
    if not bare:
        return None
    query = 'rfc822msgid:"%s"' % bare
    response = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=1)
        .execute()
    )
    messages = response.get("messages") or []
    if not messages:
        return None
    return messages[0].get("id")


def insert_raw(service, raw, label_ids):
    # type: (Any, bytes, List[str]) -> str
    """Store a raw message. internalDate comes from the Date header. No send."""
    created = (
        service.users()
        .messages()
        .insert(
            userId="me",
            body={"raw": _encode_raw(raw), "labelIds": list(label_ids)},
            internalDateSource="dateHeader",
        )
        .execute()
    )
    return created.get("id") or ""


def create_draft(service, recipient, subject, body):
    # type: (Any, str, str, str) -> str
    """Create one draft addressed to `recipient`. Nothing is sent."""
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    created = (
        service.users()
        .drafts()
        .create(
            userId="me",
            body={"message": {"raw": _encode_raw(message.as_bytes())}},
        )
        .execute()
    )
    return created.get("id") or ""
