"""Insert local .eml files into Gmail with the monthclose label.

Uses users.messages.insert, which stores the message and does not send it.
Original dates are kept from the Date header. A message already present with
the same Message-ID is skipped.
"""
import argparse
import hashlib
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Optional, Tuple

from app.db import ROOT
from app.gmail_api import (
    LABEL_MONTHCLOSE,
    add_label,
    ensure_label,
    find_by_message_id,
    insert_raw,
)
from app.google_auth import GoogleAuthError, gmail_service


def _message_id(raw):
    # type: (bytes) -> Optional[str]
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    value = str(parsed.get("message-id") or "").strip()
    return value or None


def _with_message_id(raw):
    # type: (bytes) -> Tuple[bytes, str]
    """Return bytes that carry a Message-ID, without rewriting the local file."""
    current = _message_id(raw)
    if current:
        return raw, current
    digest = hashlib.sha256(raw).hexdigest()
    header = "<monthclose-%s@seed.local>" % digest
    stamped = ("Message-ID: %s\r\n" % header).encode("ascii") + raw
    return stamped, header


def seed_folder(folder):
    # type: (Path) -> dict
    inbox = Path(folder)
    if not inbox.is_absolute():
        inbox = ROOT / inbox
    if not inbox.is_dir():
        raise SystemExit("folder not found")

    try:
        service = gmail_service()
    except GoogleAuthError as exc:
        print(exc.code, file=sys.stderr)
        raise SystemExit(1)

    label = ensure_label(service, LABEL_MONTHCLOSE)
    counts = {"inserted": 0, "labeled": 0, "skipped": 0, "failed": 0}
    paths = sorted(inbox.glob("*.eml"))
    for path in paths:
        try:
            raw, message_id = _with_message_id(path.read_bytes())
            existing = find_by_message_id(service, message_id)
            if existing:
                meta = (
                    service.users()
                    .messages()
                    .get(userId="me", id=existing, format="minimal")
                    .execute()
                )
                if label not in (meta.get("labelIds") or []):
                    add_label(service, existing, label)
                    counts["labeled"] += 1
                else:
                    counts["skipped"] += 1
                continue
            insert_raw(service, raw, [label])
            counts["inserted"] += 1
        except Exception:
            counts["failed"] += 1
    return counts


def format_counts(counts):
    # type: (dict) -> str
    return "\n".join(
        [
            "inserted: %d" % counts["inserted"],
            "labeled: %d" % counts["labeled"],
            "skipped: %d" % counts["skipped"],
            "failed: %d" % counts["failed"],
        ]
    )


def main():
    # type: () -> None
    parser = argparse.ArgumentParser(
        description="Insert local .eml files into Gmail under the monthclose label."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Insert data/demo/emails instead of data/inbox",
    )
    args = parser.parse_args()
    folder = ROOT / "data" / "demo" / "emails" if args.demo else ROOT / "data" / "inbox"
    print(format_counts(seed_folder(folder)))


if __name__ == "__main__":
    main()
