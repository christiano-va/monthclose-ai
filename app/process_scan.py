"""Process one scanned PDF with no email sender and no email body.

Pass --note when the owner typed a remark with the scan. That note is context.
The file name and the sender are not used to choose a pending item.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from sqlmodel import Session

from app.db import ROOT, engine, init_db
from app.extract import list_categories
from app.pending import format_late_match
from app.process_one import process_scan_bytes


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
    )
    parser = argparse.ArgumentParser(description="Process one scanned PDF.")
    parser.add_argument("pdf", help="Path to the PDF")
    parser.add_argument(
        "--note",
        default="",
        help="Optional note typed with the scan",
    )
    args = parser.parse_args()
    path = Path(args.pdf)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise SystemExit("file not found: %s" % path)
    init_db()
    with Session(engine, expire_on_commit=False) as session:
        categories = list_categories(session)
        if not categories:
            raise SystemExit(
                "Category table is empty. Run python -m app.load_history first."
            )
        results = process_scan_bytes(
            session, path.read_bytes(), path.name, args.note or "", categories
        )
    for item in results:
        print(format_late_match(item))
        print("")
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
