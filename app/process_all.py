"""Process every email in the local inbox. Safe to re-run.

--reprocess updates the stored record of an attachment instead of skipping it.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

from sqlmodel import Session

from app.db import ROOT, engine, init_db
from app.extract import list_categories
from app.llm import llm_call_count, reset_llm_calls
from app.mailbox import LocalEmlMailbox
from app.process_one import LOW_CONFIDENCE_THRESHOLD, ensure_owner_tasks, process_eml

logger = logging.getLogger("app.process")


def process_all(folder=None, reprocess=False):
    # type: (Path, bool) -> List[Dict[str, Any]]
    init_db()
    reset_llm_calls()
    inbox = Path(folder) if folder is not None else ROOT / "data" / "inbox"
    if not inbox.is_absolute():
        inbox = ROOT / inbox
    if not inbox.is_dir():
        raise SystemExit("inbox folder not found: %s" % inbox)

    mailbox = LocalEmlMailbox(inbox)
    paths = sorted(inbox.glob("*.eml"))
    results = []  # type: List[Dict[str, Any]]
    with Session(engine, expire_on_commit=False) as session:
        categories = list_categories(session)
        if not categories:
            raise SystemExit(
                "Category table is empty. Run python -m app.load_history first."
            )
        total = len(paths)
        for index, path in enumerate(paths, start=1):
            print(
                "email %d/%d %s" % (index, total, path.name),
                file=sys.stderr,
                flush=True,
            )
            try:
                message = mailbox.read(path)
            except Exception as exc:
                session.rollback()
                reason = "%s: %s" % (type(exc).__name__, exc)
                logger.exception("failed to read %s", path.name)
                results.append(
                    {
                        "email": path.name,
                        "attachment": "",
                        "status": "error",
                        "reason": reason,
                        "document_id": None,
                        "already_existed": False,
                    }
                )
                continue
            results.extend(
                process_eml(
                    session, message, path, categories, reprocess=reprocess
                )
            )
        ensure_owner_tasks(session)
    return results


def format_summary(results):
    # type: (List[Dict[str, Any]]) -> str
    counts = {
        "validated": 0,
        "review": 0,
        "error": 0,
        "no_document": 0,
        "bank_statement": 0,
        "duplicate": 0,
    }
    other = {}  # type: Dict[str, int]
    skipped = 0
    reprocessed = 0
    for item in results:
        status = item.get("status") or ""
        if status in counts:
            counts[status] += 1
        else:
            other[status] = other.get(status, 0) + 1
        if item.get("already_existed"):
            skipped += 1
        if item.get("reprocessed"):
            reprocessed += 1
    lines = [
        "validated: %d" % counts["validated"],
        "review: %d" % counts["review"],
        "error: %d" % counts["error"],
        "no_document: %d" % counts["no_document"],
        "bank_statement: %d" % counts["bank_statement"],
        "duplicate: %d" % counts["duplicate"],
        "llm_calls: %d" % llm_call_count(),
        "items: %d" % len(results),
        "skipped_already_stored: %d" % skipped,
        "reprocessed: %d" % reprocessed,
        "low_confidence_below: %.2f" % LOW_CONFIDENCE_THRESHOLD,
    ]
    for status in sorted(other):
        lines.append("%s: %d" % (status, other[status]))
    lines.append("")
    lines.append("review / error / no_document / bank_statement / duplicate")
    lines.append("status | email | attachment | reason")
    found = False
    for item in results:
        if item.get("status") not in (
            "review",
            "error",
            "no_document",
            "bank_statement",
            "duplicate",
        ):
            continue
        found = True
        lines.append(
            "%s | %s | %s | %s"
            % (
                item.get("status"),
                item.get("email") or "",
                item.get("attachment") or "-",
                item.get("reason") or "",
            )
        )
    if not found:
        lines.append("(none)")
    lines.append("")
    lines.append("changed documents (category / tipo / counterparty)")
    lines.append("id | field | old | new")
    changed = False
    seen = set()
    for item in results:
        for change in item.get("changes") or []:
            key = (
                change.get("id"),
                change.get("field"),
                change.get("old"),
                change.get("new"),
            )
            if key in seen:
                continue
            seen.add(key)
            changed = True
            lines.append(
                "%s | %s | %s | %s"
                % (
                    change.get("id"),
                    change.get("field") or "",
                    change.get("old") or "",
                    change.get("new") or "",
                )
            )
    if not changed:
        lines.append("(none)")
    new_ids = []
    for item in results:
        if item.get("created") and item.get("document_id"):
            new_ids.append(str(item["document_id"]))
    if new_ids:
        lines.append("")
        lines.append("new documents: %s" % ", ".join(new_ids))
    return "\n".join(lines)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(
        description="Process every email in the local inbox."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        help="Inbox folder (default: data/inbox)",
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Update the stored record of each attachment instead of skipping it",
    )
    args = parser.parse_args()
    results = process_all(args.folder, reprocess=args.reprocess)
    print(format_summary(results))


if __name__ == "__main__":
    main()
