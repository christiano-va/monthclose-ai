"""Mailbox abstraction: local .eml files now, Gmail later."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class MailMessage:
    sender: str
    subject: str
    date: Optional[datetime]
    body: str
    attachments: List[Attachment] = field(default_factory=list)
    source_path: Optional[str] = None
    message_id: Optional[str] = None


class Mailbox(ABC):
    @abstractmethod
    def messages(self):
        # type: () -> Iterable[MailMessage]
        raise NotImplementedError


def _parse_eml(path):
    # type: (Path) -> MailMessage
    raw = path.read_bytes()
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    sender = str(msg.get("from") or "")
    subject = str(msg.get("subject") or "")
    message_id = str(msg.get("message-id") or "").strip() or None
    date_header = msg.get("date")
    parsed_date = None
    if date_header:
        try:
            parsed_date = parsedate_to_datetime(str(date_header))
        except (TypeError, ValueError):
            parsed_date = None

    body_parts = []
    attachments = []
    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        is_pdf = content_type == "application/pdf" or (
            filename is not None and filename.lower().endswith(".pdf")
        )
        if disposition == "attachment" or is_pdf:
            attachments.append(
                Attachment(
                    filename=filename or "attachment.bin",
                    content_type=content_type,
                    data=payload,
                )
            )
            continue
        if content_type == "text/plain":
            charset = part.get_content_charset() or "utf-8"
            try:
                body_parts.append(payload.decode(charset, errors="replace"))
            except LookupError:
                body_parts.append(payload.decode("utf-8", errors="replace"))

    return MailMessage(
        sender=sender,
        subject=subject,
        date=parsed_date,
        body="\n".join(body_parts).strip(),
        attachments=attachments,
        source_path=str(path),
        message_id=message_id,
    )


class LocalEmlMailbox(Mailbox):
    def __init__(self, folder):
        # type: (Path) -> None
        self.folder = Path(folder)

    def messages(self):
        # type: () -> Iterable[MailMessage]
        for path in sorted(self.folder.glob("*.eml")):
            yield _parse_eml(path)

    def read(self, path):
        # type: (Path) -> MailMessage
        return _parse_eml(Path(path))


class GmailMailbox(Mailbox):
    """Same interface as LocalEmlMailbox; Gmail API wiring comes later."""

    def messages(self):
        # type: () -> Iterable[MailMessage]
        raise NotImplementedError("Gmail mailbox is not implemented yet")
