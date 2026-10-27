"""Rebuild the demo database from the synthetic history, inbox and statement.

Without flags, September 2026 stays open and its pending tasks stay open.
With --closed, the owner accepts those pending items and the month stays closed.
"""
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import DB_PATH, ROOT, engine, init_db
from app.link_all import _approval_reason
from app.linker import link_all
from app.load_history import load_history
from app.main import app
from app.models import BankLine, OpenItem, Period
from app.process_all import format_summary, process_all
from app.statements import import_statement_text

STATEMENT = ROOT / "data" / "statements" / "extrato_BancoExemplo_2026-09.csv"
MONTH = "2026-09"
PENDING_KINDS = ("missing_document", "missing_sales_invoice", "invoice_to_request")


def clear_uploads():
    # type: () -> None
    """Phone photos are not part of the rebuilt books."""
    folder = ROOT / "data" / "uploads"
    if not folder.is_dir():
        return
    for path in folder.iterdir():
        if path.is_file():
            path.unlink()


def drop_database():
    # type: () -> None
    """Close every connection and delete the SQLite file."""
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(DB_PATH) + suffix)
        if path.exists():
            path.unlink()
    init_db()


def _import_statement():
    # type: () -> dict
    raw = STATEMENT.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    with Session(engine, expire_on_commit=False) as session:
        return import_statement_text(
            session,
            text,
            justification="imported from the September bank statement",
        )


def _pending_rows():
    # type: () -> list
    with Session(engine) as session:
        period = session.exec(select(Period).where(Period.month == MONTH)).first()
        status = period.status if period is not None else "missing"
        items = session.exec(select(OpenItem).order_by(OpenItem.id)).all()
        rows = []
        for item in items:
            if item.period != MONTH or item.kind not in PENDING_KINDS:
                continue
            if item.status not in ("open", "accepted_by_owner", "resolved_by_late_document"):
                continue
            rows.append(
                {
                    "id": item.id,
                    "kind": item.kind,
                    "status": item.status,
                    "amount": str(item.amount),
                    "description": item.description,
                }
            )
        lines = session.exec(select(BankLine).where(BankLine.period == MONTH)).all()
        return {
            "period": MONTH,
            "status": status,
            "bank_lines": len(lines),
            "pending": rows,
        }


def _close_with_owner():
    # type: () -> dict
    """Accept every open blocker and close the month. Leave it closed."""
    client = TestClient(app)
    refused = client.post("/periods/%s/close" % MONTH)
    body = refused.json()
    detail = body.get("detail") if isinstance(body, dict) else {}
    if not isinstance(detail, dict):
        detail = {}
    approvals = []
    for rows in (detail.get("blockers_by_kind") or {}).values():
        for row in rows:
            if row.get("id") is None:
                continue
            approvals.append({"id": row["id"], "reason": _approval_reason(row)})
    if refused.status_code != 409 or not approvals:
        raise SystemExit(
            "September has no owner pendings to accept (HTTP %s)." % refused.status_code
        )
    stamp = datetime.utcnow().replace(microsecond=0)
    request = {
        "approvals": approvals,
        "approved_by": "owner",
        "timestamp": stamp.isoformat(),
    }
    approved = client.post("/periods/%s/close" % MONTH, json=request)
    if approved.status_code != 200:
        raise SystemExit("close failed: %s" % approved.text)
    return {"request": request, "closed": approved.json()}


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
    )
    parser = argparse.ArgumentParser(
        description="Rebuild the demo database from scratch."
    )
    parser.add_argument(
        "--closed",
        action="store_true",
        help="Close 2026-09 after the owner accepts the pending items",
    )
    args = parser.parse_args()

    print("Dropped %s" % DB_PATH.name)
    drop_database()
    clear_uploads()
    print("Cleared data/uploads")

    print("Loaded history")
    history = load_history()
    print(json.dumps(history, indent=2, ensure_ascii=False))
    print("")

    print("Processed data/inbox")
    results = process_all(ROOT / "data" / "inbox")
    print(format_summary(results))
    print("")

    print("Imported %s" % STATEMENT.relative_to(ROOT))
    imported = _import_statement()
    print(
        "statement lines created %s, already stored %s, periods %s"
        % (imported["created"], imported["skipped"], ", ".join(imported["periods"]))
    )
    print("")

    print("Linked bank lines to documents")
    with Session(engine, expire_on_commit=False) as session:
        counts = link_all(session)
    print(json.dumps(counts, indent=2, ensure_ascii=False))
    print("")

    if args.closed:
        print("Accepted the pending items and closed %s" % MONTH)
        closed = _close_with_owner()
        print(json.dumps(closed["closed"], indent=2, ensure_ascii=False))
        print("approvals: %d" % len(closed["request"]["approvals"]))
    else:
        print("Left %s open. Pending tasks were not accepted." % MONTH)

    print("")
    state = _pending_rows()
    print(json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
