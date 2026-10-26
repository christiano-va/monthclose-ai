"""Link every bank line to its documents. Safe to re-run.

--relink drops the previous links and computes them again.
"""
import argparse
import json
import logging
import sys
from datetime import datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import ROOT, engine, init_db
from app.linker import link_all
from app.main import app
from app.models import BankLine, Document, Link, OpenItem, Period
from app.statements import verify_statement_text

METHODS = (
    "reference",
    "exact",
    "n_to_1",
    "1_to_n",
    "llm",
    "payout",
    "settlement",
    "reimbursement",
    "evidence",
    "late_document",
    "explanation",
)


def _statement_reports():
    # type: () -> list
    folder = ROOT / "data" / "statements"
    reports = []
    if not folder.is_dir():
        return reports
    for path in sorted(folder.glob("*.csv")):
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        report = verify_statement_text(text)
        report["file"] = str(path.relative_to(ROOT))
        reports.append(report)
    return reports


def _format_statement(reports):
    # type: (list) -> str
    lines = ["statement check: opening + credits - debits == closing"]
    if not reports:
        lines.append("(no CSV in data/statements)")
        return "\n".join(lines)
    for report in reports:
        flag = "OK" if report.get("equation_ok") and report.get("running_balance_ok") else "FAIL"
        lines.append(
            "%s | %s | opening %s + credits %s - debits %s = %s | closing %s | %s"
            % (
                flag,
                report.get("file"),
                report.get("opening_balance"),
                report.get("credits"),
                report.get("debits"),
                report.get("computed_closing"),
                report.get("closing_balance"),
                report.get("period_label") or "",
            )
        )
        for item in report.get("running_breaks") or []:
            lines.append("  break: %s" % item)
    return "\n".join(lines)


def _format_summary(links):
    # type: (list) -> str
    tally = {}
    for link in links:
        tally[link.method] = tally.get(link.method, 0) + 1
    rows = ["links by method"]
    for name in METHODS:
        rows.append("%s: %d" % (name, tally.get(name, 0)))
    for name in sorted(tally):
        if name not in METHODS:
            rows.append("%s: %d" % (name, tally[name]))
    rows.append("link rows: %d" % len(links))
    return "\n".join(rows)


def _link_sort(link):
    # type: (Link) -> tuple
    amount = link.allocated_amount
    if link.method == "evidence" or amount == 0:
        return (2, link.id or 0)
    if amount < 0:
        return (1, link.id or 0)
    return (0, -amount, link.id or 0)


def _format_table(session, links, lines):
    # type: (Session, list, list) -> str
    from app.money import ZERO, to_money

    documents = {}
    for document in session.exec(select(Document)).all():
        documents[document.id] = document
    by_line = {}
    for link in links:
        by_line.setdefault(link.bank_line_id, []).append(link)
    rows = ["links by bank line"]
    for line in lines:
        rows.append(
            "line %s | %s | %s | %s"
            % (line.id, line.movement_date.isoformat(), line.amount, line.description)
        )
        group = sorted(by_line.get(line.id, []), key=_link_sort)
        if not group:
            rows.append("  (no documents)")
            continue
        total = ZERO
        for link in group:
            total = to_money(total + to_money(link.allocated_amount))
            document = documents.get(link.document_id)
            number = document.document_number if document is not None else "?"
            entity = document.entity if document is not None else "?"
            rows.append(
                "  doc %s | %s | %s | allocated %s | %s | %.2f | %s"
                % (
                    link.document_id,
                    number,
                    entity,
                    to_money(link.allocated_amount),
                    link.method,
                    link.confidence,
                    link.explanation,
                )
            )
        rows.append("  allocated sum %s" % to_money(total))
    unsupported = [line for line in lines if line.status == "unsupported"]
    rows.append("")
    rows.append("unsupported")
    if not unsupported:
        rows.append("(none)")
    for line in unsupported:
        rows.append(
            "%s | %s | %s | %s"
            % (line.id, line.movement_date.isoformat(), line.amount, line.description)
        )
    return "\n".join(rows)


def _format_tasks(items):
    # type: (list) -> str
    rows = ["owner tasks"]
    if not items:
        rows.append("(none)")
        return "\n".join(rows)
    for item in items:
        rows.append(
            "%s | %s | %s | %s | %s | %s"
            % (
                item.id,
                item.period,
                item.kind,
                item.status,
                item.amount,
                item.description,
            )
        )
    return "\n".join(rows)


def _format_balances(balances):
    # type: (list) -> str
    rows = ["partial receipts"]
    if not balances:
        rows.append("(none)")
        return "\n".join(rows)
    for invoice in balances:
        rows.append(
            "%s | %s | invoice %s | remaining open %s"
            % (
                invoice.get("document_number"),
                invoice.get("entity"),
                invoice.get("invoice_total"),
                invoice.get("remaining_open"),
            )
        )
        for step in invoice.get("receipts") or []:
            rows.append(
                "  %s | %s | %s | remaining open %s"
                % (
                    step.get("date"),
                    step.get("document_number"),
                    step.get("amount"),
                    step.get("remaining_open"),
                )
            )
    return "\n".join(rows)


def _checklist(month):
    # type: (str) -> dict
    client = TestClient(app)
    response = client.get("/periods/%s/checklist" % month)
    response.raise_for_status()
    return response.json()


def _print_response(response):
    # type: (object) -> None
    print("HTTP %s" % response.status_code)
    try:
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(response.text)


def _approval_reason(row):
    # type: (dict) -> str
    kind = row.get("kind")
    text = (row.get("item") or "").lower()
    if kind == "missing_sales_invoice":
        return (
            "The guest sales invoice was not issued. Accepted so this month can close. "
            "Issuing the invoice remains a legal obligation."
        )
    if "ptdomain" in text or "renovacao" in text or "renovação" in text:
        return "Lost invoice: the domain renewal document cannot be obtained."
    if "santos" in text:
        return "Unidentified transfer: the supporting document cannot be obtained."
    return (
        "The document is lost or cannot be obtained. "
        "Owner accepts this item so the month can close."
    )


def _reopen_for_demo(month):
    # type: (str) -> bool
    """A previous demonstration may have closed the month. Show the flow again."""
    with Session(engine) as session:
        period = session.exec(select(Period).where(Period.month == month)).first()
        if period is None or period.status != "closed":
            return False
        period.status = "open"
        period.closed_at = None
        session.add(period)
        session.commit()
    return True


def _demonstrate_close(month):
    # type: (str) -> None
    if _reopen_for_demo(month):
        print(
            "period %s was closed; reopened so the close flow can be demonstrated"
            % month
        )
        print("")
    client = TestClient(app)
    print("POST /periods/%s/close" % month)
    refused = client.post("/periods/%s/close" % month)
    _print_response(refused)
    print("")
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
        print("close was not refused for pending items; approval step skipped")
        return
    stamp = datetime.utcnow().replace(microsecond=0)
    request = {
        "approvals": approvals,
        "approved_by": "owner",
        "timestamp": stamp.isoformat(),
    }
    print("POST /periods/%s/close" % month)
    print(json.dumps(request, indent=2, ensure_ascii=False))
    approved = client.post("/periods/%s/close" % month, json=request)
    _print_response(approved)
    print("")
    print("GET /reports/%s accepted_pending_items" % month)
    report = client.get("/reports/%s" % month)
    report.raise_for_status()
    payload = report.json()
    print(
        json.dumps(
            {"accepted_pending_items": payload.get("accepted_pending_items")},
            indent=2,
            ensure_ascii=False,
        )
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    parser = argparse.ArgumentParser(description="Link bank lines to documents.")
    parser.add_argument(
        "--relink",
        action="store_true",
        help="Drop existing links and compute them again",
    )
    parser.add_argument(
        "--month",
        default="2026-09",
        help="Checklist month (default 2026-09)",
    )
    args = parser.parse_args()
    init_db()
    print(_format_statement(_statement_reports()))
    print("")
    with Session(engine, expire_on_commit=False) as session:
        link_all(session, relink=args.relink)
        links = session.exec(select(Link).order_by(Link.bank_line_id, Link.id)).all()
        lines = session.exec(
            select(BankLine).order_by(BankLine.movement_date, BankLine.id)
        ).all()
        tasks = session.exec(select(OpenItem).order_by(OpenItem.id)).all()
        owner_tasks = [
            item
            for item in tasks
            if item.kind in ("missing_document", "missing_sales_invoice", "invoice_to_request")
            and item.status in ("open", "accepted_by_owner")
            and (item.period or "") >= "2026-09"
        ]
        print(_format_summary(links))
        print("")
        print(_format_table(session, links, lines))
        print("")
        print(_format_tasks(owner_tasks))
    print("")
    checklist = _checklist(args.month)
    print(_format_balances(checklist.get("invoice_open_balances") or []))
    print("")
    print("GET /periods/%s/checklist" % args.month)
    print(json.dumps(checklist, indent=2, ensure_ascii=False))
    print("")
    _demonstrate_close(args.month)


if __name__ == "__main__":
    main()
