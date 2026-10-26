"""Link every bank line to its documents. Safe to re-run.

--relink drops the previous links and computes them again.
"""
import argparse
import json
import logging
import sys

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import ROOT, engine, init_db
from app.linker import link_all
from app.main import app
from app.models import BankLine, Document, Link
from app.statements import verify_statement_text

METHODS = ("reference", "exact", "n_to_1", "1_to_n", "llm", "unsupported")


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


def _format_summary(counts, links, lines):
    # type: (dict, list, list) -> str
    by_line = {}
    for link in links:
        by_line.setdefault(link.bank_line_id, []).append(link.method)
    tally = {}
    for name in METHODS:
        tally[name] = 0
    for line in lines:
        methods = by_line.get(line.id) or []
        if not methods or line.status == "unsupported":
            key = "unsupported"
        elif len(set(methods)) == 1:
            key = methods[0]
        else:
            key = methods[0]
        if key not in tally:
            tally[key] = 0
        tally[key] += 1
    rows = ["links by bank line"]
    for name in METHODS:
        rows.append("%s: %d" % (name, tally.get(name, 0)))
    for name in sorted(tally):
        if name not in METHODS:
            rows.append("%s: %d" % (name, tally[name]))
    rows.append("new link rows this run: %s" % ", ".join(
        "%s %s" % (name, counts.get(name, 0)) for name in ("reference", "exact", "n_to_1", "1_to_n", "llm")
    ))
    return "\n".join(rows)


def _format_table(session, links, lines):
    # type: (Session, list, list) -> str
    documents = {}
    for document in session.exec(select(Document)).all():
        documents[document.id] = document
    lines_by_id = {}
    for line in lines:
        lines_by_id[line.id] = line
    rows = [
        "links",
        "line | date | bank_amount | method | doc | document | allocated | confidence | explanation",
    ]
    ordered = sorted(links, key=lambda link: (lines_by_id[link.bank_line_id].movement_date, link.bank_line_id, link.id or 0))
    for link in ordered:
        line = lines_by_id.get(link.bank_line_id)
        document = documents.get(link.document_id)
        number = document.document_number if document is not None else "?"
        entity = document.entity if document is not None else "?"
        rows.append(
            "%s | %s | %s | %s | %s | %s %s | %s | %.2f | %s"
            % (
                link.bank_line_id,
                line.movement_date.isoformat() if line is not None else "",
                line.amount if line is not None else "",
                link.method,
                link.document_id,
                number,
                entity,
                link.allocated_amount,
                link.confidence,
                link.explanation,
            )
        )
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


def _checklist(month):
    # type: (str) -> dict
    client = TestClient(app)
    response = client.get("/periods/%s/checklist" % month)
    response.raise_for_status()
    return response.json()


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
        counts = link_all(session, relink=args.relink)
        links = session.exec(select(Link).order_by(Link.bank_line_id, Link.id)).all()
        lines = session.exec(
            select(BankLine).order_by(BankLine.movement_date, BankLine.id)
        ).all()
        print(_format_summary(counts, links, lines))
        print("")
        print(_format_table(session, links, lines))
    print("")
    print("GET /periods/%s/checklist" % args.month)
    print(json.dumps(_checklist(args.month), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
