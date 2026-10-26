"""Load synthetic bookkeeping history from data/history into SQLite."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from sqlmodel import Session, select

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import engine, init_db  # noqa: E402
from app.models import Category, Document, OpenItem, Period  # noqa: E402
from app.money import to_money  # noqa: E402

HISTORY_DIR = ROOT / "data" / "history"
JSON_PATH = HISTORY_DIR / "lancamentos_2025-07_a_2026-08.json"
CSV_PATH = HISTORY_DIR / "lancamentos_2025-07_a_2026-08.csv"
PDF_DIR = HISTORY_DIR / "documentos_exemplo_agosto"


def parse_iso_date(value):
    # type: (Optional[str]) -> Optional[date]
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def add_months(year_month, extra):
    # type: (str, int) -> str
    year, month = [int(part) for part in year_month.split("-")]
    month += extra
    while month > 12:
        month -= 12
        year += 1
    return "%04d-%02d" % (year, month)


def match_pdfs(documents):
    # type: (List[dict]) -> Dict[str, List[str]]
    """Map history source ids to example PDF paths found on disk."""
    mapping = {}  # type: Dict[str, List[str]]
    if not PDF_DIR.exists():
        return mapping

    for pdf in sorted(PDF_DIR.glob("*.pdf")):
        stem = pdf.stem.lower()
        rel = str(pdf.relative_to(ROOT))
        matched_id = None
        if "ft_2026a-0008" in stem or "ft_2026a_0008" in stem:
            for row in documents:
                if row["documento"] == "FT 2026A/0008":
                    matched_id = row["id"]
                    break
        elif "iva_2t2026" in stem or "iva_2t" in stem:
            for row in documents:
                if (
                    row["categoria"] == "ESTADO_IVA"
                    and str(row["data_documento"]).startswith("2026-08")
                ):
                    matched_id = row["id"]
                    break
        if matched_id:
            mapping.setdefault(matched_id, []).append(rel)
    return mapping


def load_history():
    init_db()
    if not JSON_PATH.exists():
        raise SystemExit("history JSON not found: %s" % JSON_PATH)

    rows = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    pdfs = match_pdfs(rows)

    created_docs = 0
    skipped_docs = 0
    created_open = 0
    categories = 0
    periods = 0

    with Session(engine) as session:
        for row in rows:
            source_id = row["id"]
            existing = session.exec(
                select(Document).where(Document.source_id == source_id)
            ).first()
            if existing is not None:
                skipped_docs += 1
                continue

            month = row["data_documento"][:7]
            period = session.exec(
                select(Period).where(Period.month == month)
            ).first()
            if period is None:
                session.add(Period(month=month, status="open"))
                session.commit()
                periods += 1

            category = session.exec(
                select(Category).where(Category.code == row["categoria"])
            ).first()
            if category is None:
                category = Category(
                    code=row["categoria"],
                    snc_account=str(row["conta_snc"]),
                    description=row["categoria"],
                )
                session.add(category)
                session.commit()
                session.refresh(category)
                categories += 1

            paths = pdfs.get(source_id, [])
            file_path = paths[0] if paths else None
            notes = row.get("notas") or ""
            extra_files = paths[1:]
            if extra_files:
                extra = "additional_files: " + "; ".join(extra_files)
                notes = (notes + " | " + extra).strip(" |")

            document = Document(
                source_id=source_id,
                period=month,
                document_date=parse_iso_date(row["data_documento"]),
                payment_date=parse_iso_date(row.get("data_pagamento") or None),
                tipo=row["tipo"],
                category_id=category.id,
                entity=row["entidade"],
                nif=None if row.get("nif") in (None, "-", "") else str(row["nif"]),
                document_number=row["documento"],
                base=to_money(row["base"]),
                vat_rate=to_money(row["taxa_iva"]),
                vat=to_money(row["iva"]),
                total=to_money(row["total"]),
                deductible_vat=to_money(row["iva_dedutivel"]),
                payment_method=row.get("meio_pagamento"),
                status=row.get("estado") or "pendente",
                notes=notes or None,
                file_path=file_path,
                confidence=1.0,
                justification="loaded from data/history JSON",
            )
            session.add(document)
            session.commit()
            session.refresh(document)
            created_docs += 1

            is_open = row.get("estado") == "em_aberto"
            is_next_month_guide = (
                row.get("meio_pagamento") == "guia_mes_seguinte"
                and not row.get("data_pagamento")
            )
            if is_open or is_next_month_guide:
                kind = "receivable" if row["tipo"] == "receita" else "payable"
                due = parse_iso_date(row.get("data_pagamento") or None)
                if due is None and is_next_month_guide:
                    due_month = add_months(month, 1)
                    due = datetime.strptime(due_month + "-15", "%Y-%m-%d").date()
                open_item = OpenItem(
                    document_id=document.id,
                    period=month,
                    kind=kind,
                    description="%s — %s" % (row["documento"], row["entidade"]),
                    amount=to_money(row["total"]),
                    due_date=due,
                    status="open",
                    notes=row.get("notas") or None,
                    confidence=1.0,
                    justification="history status %s / %s"
                    % (row.get("estado"), row.get("meio_pagamento")),
                )
                session.add(open_item)
                session.commit()
                created_open += 1

    return {
        "json": str(JSON_PATH.relative_to(ROOT)),
        "csv_duplicate_of_json": str(CSV_PATH.relative_to(ROOT)),
        "pdf_dir": str(PDF_DIR.relative_to(ROOT)),
        "records_in_file": len(rows),
        "documents_created": created_docs,
        "documents_skipped": skipped_docs,
        "categories_created": categories,
        "periods_created": periods,
        "open_items_created": created_open,
        "pdfs_attached": sum(1 for paths in pdfs.values() if paths),
    }


def main():
    summary = load_history()
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
