"""Read the MonthClose Gmail label, file documents, link the books, draft owner mail.

The summary is counts only. Message bodies are not returned.
"""
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from app.db import ROOT, engine, init_db
from app.extract import list_categories
from app.google_auth import GoogleAuthError
from app.llm import llm_call_count, reset_llm_calls

logger = logging.getLogger("app.sync")

STATUS_FILE = ROOT / "data" / "cache" / "sync_status.json"
_KNOWN_STATUS = (
    "validated",
    "review",
    "error",
    "no_document",
    "bank_statement",
    "duplicate",
)

_LOCK = threading.Lock()
_RUNNING = False


class SyncError(Exception):
    def __init__(self, code, status=503):
        # type: (str, int) -> None
        self.code = code
        self.status = status
        super(SyncError, self).__init__(code)


def _empty_processing():
    # type: () -> Dict[str, int]
    counts = {}  # type: Dict[str, int]
    for name in _KNOWN_STATUS:
        counts[name] = 0
    counts["other"] = 0
    counts["skipped_already_stored"] = 0
    counts["llm_calls"] = 0
    return counts


def _add_results(counts, results):
    # type: (Dict[str, int], List[Dict[str, Any]]) -> None
    for item in results:
        status = item.get("status") or ""
        if status in _KNOWN_STATUS:
            counts[status] += 1
        else:
            counts["other"] += 1
        if item.get("already_existed"):
            counts["skipped_already_stored"] += 1


def _load_last():
    # type: () -> Optional[Dict[str, Any]]
    if not STATUS_FILE.is_file():
        return None
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _save_last(summary):
    # type: (Dict[str, Any]) -> None
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def sync_status():
    # type: () -> Dict[str, Any]
    with _LOCK:
        running = _RUNNING
    return {"running": running, "last": _load_last()}


def _begin():
    # type: () -> bool
    global _RUNNING
    with _LOCK:
        if _RUNNING:
            return False
        _RUNNING = True
        return True


def _end():
    # type: () -> None
    global _RUNNING
    with _LOCK:
        _RUNNING = False


def run_sync():
    # type: () -> Dict[str, Any]
    """Process Gmail, upload to Drive, link bank lines, and draft owner requests."""
    if not _begin():
        raise SyncError("already_running", 409)
    try:
        return _run_sync()
    finally:
        _end()


def _run_sync():
    # type: () -> Dict[str, Any]
    from app.drive import upload_pending
    from app.linker import link_all
    from app.mailbox import GmailMailbox
    from app.owner_drafts import create_owner_drafts
    from app.process_one import process_eml

    init_db()
    reset_llm_calls()
    summary = {
        "messages": {"read": 0, "processed": 0, "labeled": 0, "failed": 0},
        "processing": _empty_processing(),
        "documents": {
            "uploaded": 0,
            "already_linked": 0,
            "relinked": 0,
            "skipped_no_file": 0,
            "failed": 0,
        },
        "linker": {},
        "drafts": {
            "created": 0,
            "already_drafted": 0,
            "skipped": 0,
            "failed": 0,
            "owner_email_missing": 0,
        },
        "error": None,
        "finished_at": None,
    }  # type: Dict[str, Any]

    with Session(engine, expire_on_commit=False) as session:
        categories = list_categories(session)
        if not categories:
            raise SyncError("categories_empty", 409)

        try:
            mailbox = GmailMailbox()
        except GoogleAuthError:
            raise SyncError("not_authorized", 503)

        try:
            for message in mailbox.messages():
                summary["messages"]["read"] += 1
                name = Path("gmail-%s.eml" % (message.gmail_id or "message"))
                try:
                    batch = process_eml(session, message, name, categories)
                except Exception:
                    session.rollback()
                    logger.error("message processing failed")
                    summary["messages"]["failed"] += 1
                    continue
                _add_results(summary["processing"], batch)
                summary["messages"]["processed"] += 1
                try:
                    mailbox.mark_processed(message)
                    summary["messages"]["labeled"] += 1
                except Exception:
                    logger.error("label update failed")
                    summary["messages"]["failed"] += 1
        except Exception:
            logger.error("gmail read failed")
            summary["error"] = "gmail_read_failed"

        try:
            summary["documents"] = upload_pending(session)
        except Exception:
            logger.error("drive upload failed")
            summary["documents"]["failed"] += 1
            if summary["error"] is None:
                summary["error"] = "drive_upload_failed"

        try:
            summary["linker"] = _int_map(link_all(session))
        except Exception:
            logger.error("linker failed")
            session.rollback()
            if summary["error"] is None:
                summary["error"] = "linker_failed"

        try:
            summary["drafts"] = create_owner_drafts(session)
        except Exception:
            logger.error("drafts failed")
            session.rollback()
            summary["drafts"]["failed"] += 1
            if summary["error"] is None:
                summary["error"] = "drafts_failed"

    summary["processing"]["llm_calls"] = llm_call_count()
    summary["finished_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    _save_last(summary)
    return summary


def _int_map(values):
    # type: (Dict[str, Any]) -> Dict[str, int]
    cleaned = {}  # type: Dict[str, int]
    for key, value in values.items():
        try:
            cleaned[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return cleaned
