"""PDF text extraction + LLM JSON + history-based category classification."""
import io
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber
from sqlmodel import Session, select

from app.llm import llm_json
from app.mailbox import MailMessage
from app.models import Category, Document
from app.money import to_money
from app.settings import FINAL_CONSUMER_NIF, company_nif
from app.validate import parse_money_field

EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "document_kind",
        "document_type",
        "issuer_name",
        "issuer_nif",
        "nif",
        "recipient_name",
        "recipient_nif",
        "payee_name",
        "payee_nif",
        "document_number",
        "date",
        "due_date",
        "base",
        "vat_per_rate",
        "total",
        "total_payable",
        "lines",
        "amount",
        "gross",
        "deductions",
        "net",
        "iban",
        "currency",
        "description",
        "suggested_category",
        "conta_snc",
        "confidence",
        "justification",
        "risk_flags",
        "settles_invoice",
        "referenced_invoice",
    ],
    "properties": {
        "document_kind": {
            "type": "string",
            "enum": [
                "invoice",
                "receipt",
                "payslip",
                "tax_guide",
                "payment_proof",
                "payment_notice",
                "bank_statement",
                "payout_report",
                "other",
            ],
        },
        "document_type": {"type": "string"},
        "issuer_name": {"type": "string"},
        "issuer_nif": {"type": ["string", "null"]},
        "nif": {"type": ["string", "null"]},
        "recipient_name": {"type": "string"},
        "recipient_nif": {"type": ["string", "null"]},
        "payee_name": {"type": ["string", "null"]},
        "payee_nif": {"type": ["string", "null"]},
        "settles_invoice": {"type": "boolean"},
        "referenced_invoice": {"type": ["string", "null"]},
        "document_number": {"type": "string"},
        "date": {"type": "string"},
        "due_date": {"type": ["string", "null"]},
        "base": {"type": "string"},
        "amount": {"type": ["string", "null"]},
        "gross": {"type": ["string", "null"]},
        "deductions": {"type": ["string", "null"]},
        "net": {"type": ["string", "null"]},
        "iban": {"type": ["string", "null"]},
        "vat_per_rate": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["rate", "base", "vat"],
                "properties": {
                    "rate": {"type": "string"},
                    "base": {"type": "string"},
                    "vat": {"type": "string"},
                },
            },
        },
        "total": {"type": "string"},
        "total_payable": {"type": "string"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "base", "vat_rate", "vat", "total", "nature"],
                "properties": {
                    "description": {"type": "string"},
                    "base": {"type": "string"},
                    "vat_rate": {"type": "string"},
                    "vat": {"type": "string"},
                    "total": {"type": "string"},
                    "nature": {
                        "type": "string",
                        "enum": ["supply", "fee", "topup", "tax", "other"],
                    },
                },
            },
        },
        "currency": {"type": "string"},
        "description": {"type": "string"},
        "suggested_category": {"type": ["string", "null"]},
        "conta_snc": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
}


def pdf_text(data):
    # type: (bytes) -> str
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(text)
    return "\n".join(pages).strip()


def list_categories(session):
    # type: (Session) -> List[Category]
    return session.exec(select(Category).order_by(Category.code)).all()


HISTORY_JUSTIFICATION = "loaded from data/history JSON"


def _norm_nif(value):
    # type: (Optional[str]) -> str
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def lookup_issuer_category(session, nif):
    # type: (Session, Optional[str]) -> Optional[Category]
    """Latest history category for this counterparty NIF. Inbox rows are not history.

    The company's own NIF is never a counterparty key. 999999990 is a valid
    final-consumer NIF shared by lodging and 3D sales, so it is not one memory.
    """
    wanted = _norm_nif(nif)
    if not wanted or wanted == company_nif() or wanted == FINAL_CONSUMER_NIF:
        return None
    rows = session.exec(
        select(Document)
        .where(Document.justification == HISTORY_JUSTIFICATION)
        .order_by(Document.document_date.desc(), Document.id.desc())
    ).all()
    for document in rows:
        if document.category_id is None:
            continue
        if _norm_nif(document.nif) == wanted:
            return session.get(Category, document.category_id)
    return None


def _category_block(categories):
    # type: (List[Category]) -> str
    lines = []
    for category in categories:
        lines.append(
            "- %s | snc=%s | %s"
            % (category.code, category.snc_account or "", category.description or "")
        )
    return "\n".join(lines)


def extract_pdf_document(pdf_bytes, message, categories, text=None):
    # type: (bytes, MailMessage, List[Category], Optional[str]) -> Dict[str, Any]
    """Extract document fields. Category is chosen later, not in this call."""
    if text is None:
        text = pdf_text(pdf_bytes)
    if not text:
        raise RuntimeError("PDF has no extractable text")
    # `categories` is accepted so callers can pass the table; classification uses it.
    if categories is None:
        categories = []

    sender_domain = ""
    if "@" in (message.sender or ""):
        sender_domain = message.sender.split("@")[-1].rstrip(">")

    our_nif = company_nif()
    prompt_parts = [
        "Extract the accounting document from this PDF.",
        "The company closing the books is Nortavia Digital, NIF %s." % our_nif,
        "Ignore the vertical watermark (one letter per line). Read the real lines.",
        "issuer_name and issuer_nif are who ISSUED the document. nif is the same value as issuer_nif.",
        "recipient_name and recipient_nif are who the document is addressed to (customer, supplier's client, worker, taxpayer or tenant).",
        "On a purchase the issuer is the supplier and the recipient is Nortavia (NIF %s)." % our_nif,
        "On a sale, fatura-recibo or recibo issued by Nortavia, the issuer NIF is %s and the recipient is the customer." % our_nif,
        "Do not put the customer in issuer_nif. Do not put Nortavia in issuer_nif unless Nortavia issued the document.",
        "On a tax guide the header NIF is often zeros. Leave issuer_nif null. issuer_name is Segurança Social or Autoridade Tributária. recipient_nif is %s." % our_nif,
        "payee_name and payee_nif are who receives the money on a bank payment proof or a payment notice. On a payment proof the issuer is the bank and the payee is the entity paid (Segurança Social, Autoridade Tributária), not the account holder. On an ordinary invoice payee_name and payee_nif are null.",
        "NIF 999999990 is valid. Use it when the customer is a final consumer (consumidor final).",
        "document_kind is exactly one of: invoice, receipt, payslip, tax_guide, payment_proof, payment_notice, bank_statement, payout_report, other.",
        "Use invoice for faturas, faturas-recibo, notas de crédito and notas de débito (including VAT-exempt bank fee notes).",
        "Use receipt for payment receipts (recibo / RG), rent receipts and donation receipts.",
        "Use payslip for recibo de vencimento. Use tax_guide for a payment guide (guia) from Segurança Social or Autoridade Tributária.",
        "Use payment_proof for a bank comprovativo de pagamento. Use payment_notice for an aviso de pagamento.",
        "Use bank_statement for an extrato de conta. Use payout_report for a platform payout report. Use other for an expense reimbursement note.",
        "document_type is a short label such as fatura, fatura-recibo, recibo, nota_credito, guia, comprovativo, recibo_vencimento, extrato.",
        "Portuguese tax ids are 9 digits. Foreign issuers have an EU VAT id (for example IE1234567X). Do not rewrite a foreign id into 9 digits. Do not replace a zero NIF with %s." % our_nif,
        "Money fields are strings with a dot decimal and two places (example 184.50). A negative amount has a leading minus. Copy the figures. Do not invent amounts.",
        "total_payable is the amount that is payable or that was paid: the invoice TOTAL, total a pagar, importância recebida, líquido, or net payout. It is not the smallest line.",
        "lines lists every economic line. Each line total includes that line's VAT (base + vat = total). vat_rate is 0, 6, 13 or 23. Reverse charge and isento use vat_rate 0 and vat 0.00.",
        "The sum of line totals must equal total_payable. A withholding is a negative line. A host fee deducted from a payout is a negative fee line.",
        "nature is supply for the goods or service, fee for a commission or platform charge, topup for a balance load or meal-card credit, tax for stamp duty or a tax line, other otherwise.",
        "A meal-card top-up plus its commission are two natures: the load is topup and the commission is fee. Do not drop the load. Do not put the load in the fee line.",
        "On a rent receipt or any payment with IRS withholding: gross is the full amount before withholding, deductions is the IRS withheld, net is the amount actually paid. One positive supply line is the gross and one negative tax line is the withholding. total_payable, amount and the sum of the line totals equal net (gross minus withholding).",
        "On a payout report, each stay is a supply line whose total is the gross guest charge, and the host fee is a negative fee line. total_payable is the net paid to the host.",
        "settles_invoice is true only when this document settles an invoice that was already issued: a payment notice (aviso de pagamento), a recibo (RG) that lists documentos liquidados, or a payout of stays already invoiced.",
        "A fatura or fatura-recibo is the invoice itself: settles_invoice is false and referenced_invoice is null.",
        "A rent receipt or a donation receipt does not settle an earlier invoice: settles_invoice is false and referenced_invoice is null.",
        "referenced_invoice is the invoice number being settled, copied as printed (example FT 2026A/0008). Several numbers are separated by a comma. Null when nothing is settled.",
        "iban is the account the document asks someone to pay, copied as printed, or null if none is printed.",
        "For invoice and receipt: fill base, vat_per_rate, total, total_payable and lines. vat_per_rate has one object per printed VAT rate (0, 6, 13 or 23). total equals total_payable for a normal invoice.",
        "If an invoice or receipt shows no VAT, vat_per_rate has one object with rate \"0\", vat \"0.00\", and base equal to total. amount equals total_payable. gross, deductions and net are null.",
        "For a payslip: the recipient is the worker, not the company. gross is the sum of every earning, deductions is the sum of every deduction, net is the take-home pay, and gross - deductions must equal net.",
        "A meal allowance shown both as an earning and as a deduction belongs in both gross and deductions. amount, total and total_payable equal net. Lines are positive earnings and negative deductions and must sum to net. vat_per_rate is empty. base may equal gross.",
        "For tax_guide and payment_proof: amount is the liability being settled (social security, IRS withholding, IRC or VAT), not a new expense. total, base and total_payable equal amount. Lines may repeat that single sum with nature tax and vat 0. gross, deductions and net are null. Copy the payment reference digits (referência) into the description.",
        "For payment_notice: total_payable is the amount transferred. Lines are the invoices being paid. referenced_invoice is those invoice numbers. settles_invoice is true.",
        "For bank_statement: amount and total_payable are the closing balance. lines is an empty list. vat_per_rate is empty. settles_invoice is false. referenced_invoice is null.",
        "For other (a reimbursement note): payee_name is the manager or partner being reimbursed and issuer_name is the company when the company prepared the note. amount and total_payable are the total to transfer. Lines are the expenses with vat 0. vat_per_rate is empty. settles_invoice is false. A reimbursement to that person's own account is a normal flow.",
        "confidence is 0-1. Lower it when the text is ambiguous or a figure is unclear.",
        "justification is one line.",
        "risk_flags may contain only these names: iban_new, iban_changed, urgency_language, short_payment_deadline, personal_account.",
        "iban_new or iban_changed only when the document says the bank account is new, updated, or different from the usual one. A normal payment IBAN printed on an invoice is not a flag.",
        "urgency_language only for pressure or threat wording (URGENTE, suspensão, last chance). A normal due date, including one forwarded by the accountant, is not urgency.",
        "short_payment_deadline only when the document itself demands payment in a very short time.",
        "personal_account only when payment should go to a private person's account (for example a reimbursement to the manager or a partner). That flag describes a normal reimbursement. It is not, by itself, fraud. Do not flag a supplier invoice just because someone paid it with a personal card.",
        "Never flag a sender domain. This mailbox receives mail forwarded by the accountant and by the owner.",
        "Use an empty list when none of those four situations apply.",
        "Set suggested_category and conta_snc to null. Category is chosen in a separate step.",
        "",
        "Email sender: %s" % message.sender,
        "Email subject: %s" % message.subject,
        "Email date: %s" % (message.date.isoformat() if message.date else ""),
        "Sender domain: %s" % sender_domain,
        "Email body:",
        message.body or "(empty)",
        "",
        "PDF text:",
        text[:12000],
    ]
    return llm_json("\n".join(prompt_parts), EXTRACT_SCHEMA)


CLASSIFY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "confidence", "justification"],
    "properties": {
        "category": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
    },
}


def match_category(categories, raw):
    # type: (List[Category], Optional[str]) -> Optional[Category]
    """Return the Category row when `raw` is exactly a code from the table."""
    text = str(raw or "").strip().strip("\"'`")
    if not text:
        return None
    by_code = {}
    for category in categories:
        by_code[category.code.lower()] = category
    if text.lower() in by_code:
        return by_code[text.lower()]
    first = text.split()[0].strip(".,;:|")
    if first.lower() in by_code:
        return by_code[first.lower()]
    return None


def classify_unknown_issuer(extracted, categories, pdf_text_value, message):
    # type: (Dict[str, Any], List[Category], str, MailMessage) -> Dict[str, Any]
    """Ask the LLM to pick a category code. The table is the only allowed list."""
    prompt = "\n".join(
        [
            "Choose the bookkeeping category for this document.",
            "The company is Nortavia Digital, NIF %s." % company_nif(),
            "Answer with one category code copied exactly from the allowed list.",
            "direction sale means the company issued the document: choose a REC_ code from the main line.",
            "Alojamento, estadia or noites is REC_AL. Impressão 3D is REC_3D. Consultoria is REC_CONSULTORIA.",
            "direction purchase is a cost, asset, payroll or tax. Do not choose a REC_ code.",
            "Classify the main line (the largest amount). Do not classify the document by a smaller fee, commission or stamp-duty line.",
            "A meal-card top-up (subsídio de refeição, carregamento) is PESSOAL_REMUNERACOES.",
            "Office furniture and equipment used for more than a year (a chair, a computer) is EQUIPAMENTO_ADMIN. A 3D printer is EQUIPAMENTO_3D.",
            "A tax guide or a tax payment proof settles a liability, not a cost. IRS withholding (retenções na fonte) is ESTADO_RETENCOES_IRS. Social security contributions are ESTADO_SEGURANCA_SOCIAL. IRC and VAT keep ESTADO_IRC, ESTADO_IRC_PPC or ESTADO_IVA.",
            "A reimbursement of expenses to the manager or a partner is SOCIO_CONTA_CORRENTE, not an expense and not SUSPECT.",
            "Do not answer UNMAPPED for an IRS withholding guide.",
            "confidence is 0-1 for this classification. justification is one line.",
            "",
            "direction: %s" % (extracted.get("direction") or ""),
            "document_kind: %s" % (extracted.get("document_kind") or ""),
            "document_type: %s" % (extracted.get("document_type") or ""),
            "counterparty: %s" % (extracted.get("counterparty_name") or extracted.get("issuer_name") or ""),
            "counterparty_nif: %s" % (extracted.get("nif") or ""),
            "issuer_name: %s" % (extracted.get("issuer_name") or ""),
            "main_line: %s" % (extracted.get("main_line") or ""),
            "document_number: %s" % (extracted.get("document_number") or ""),
            "description: %s" % (extracted.get("description") or ""),
            "email subject: %s" % (message.subject or ""),
            "email sender: %s" % (message.sender or ""),
            "",
            "Allowed categories (code | snc | description):",
            _category_block(categories),
            "",
            "PDF text:",
            (pdf_text_value or "")[:6000],
        ]
    )
    return llm_json(prompt, CLASSIFY_SCHEMA)


def classify_category(session, extracted, categories, pdf_text_value, message):
    # type: (Session, Dict[str, Any], List[Category], str, MailMessage) -> Tuple[Optional[Category], float, str, str, str]
    """Reuse history for a known issuer NIF. Unknown issuers go through the LLM."""
    nif = extracted.get("nif")
    known = lookup_issuer_category(session, nif)
    if known is not None:
        return (
            known,
            0.95,
            "Issuer NIF %s found in history documents; reused category %s"
            % (nif, known.code),
            "history",
            known.code,
        )
    data = classify_unknown_issuer(extracted, categories, pdf_text_value, message)
    raw = str(data.get("category") or "").strip()
    category = match_category(categories, raw)
    confidence = float(data.get("confidence") or 0)
    justification = data.get("justification") or "LLM classified category"
    if category is None:
        justification = (
            "LLM category %r is not in the Category table. %s" % (raw, justification)
        )
    return category, confidence, justification, "llm", raw


def primary_vat(extracted):
    # type: (Dict[str, Any]) -> Tuple[Decimal, Decimal]
    """Return (rate, vat_amount) for the VAT line with the largest base."""
    lines = extracted.get("vat_per_rate") or []
    if not lines:
        return to_money(0), to_money(0)
    parsed = []
    for item in lines:
        parsed.append(
            (
                parse_money_field(item.get("base")),
                parse_money_field(item.get("rate")),
                parse_money_field(item.get("vat")),
            )
        )
    parsed.sort(key=lambda row: row[0], reverse=True)
    return parsed[0][1], sum((row[2] for row in parsed), to_money(0))


def guess_tipo(document_type, kind=None):
    # type: (Optional[str], Optional[str]) -> str
    if kind == "tax_guide":
        return "estado"
    if kind == "payslip":
        return "gasto"
    if kind == "payout_report":
        return "receita"
    text = (document_type or "").lower()
    if "credito" in text or "crédito" in text:
        return "gasto"
    if "recibo" in text and "fatura" not in text:
        return "gasto"
    if "guia" in text or "imposto" in text or "at " in text:
        return "estado"
    return "gasto"


PLAIN_EMAIL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reason",
        "invoice_to_request",
        "counterparty",
        "summary",
        "confidence",
        "justification",
    ],
    "properties": {
        "reason": {"type": "string", "enum": ["marketing", "request", "approval"]},
        "invoice_to_request": {"type": "boolean"},
        "counterparty": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "justification": {"type": "string"},
    },
}


def classify_plain_email(message):
    # type: (MailMessage) -> Dict[str, Any]
    """Classify an email that has no PDF. Does not invent amounts."""
    prompt = "\n".join(
        [
            "This email has no PDF attachment. Classify why it is in a bookkeeping mailbox.",
            "reason is marketing (newsletter or promotion), request (someone asks for something), or approval (a client approves work).",
            "invoice_to_request is true only when the body says hours were approved or asks the company to issue an invoice.",
            "counterparty is the client or sender organisation, or null.",
            "summary is one line. confidence is 0-1. justification is one line.",
            "Do not invent an amount.",
            "",
            "From: %s" % (message.sender or ""),
            "Subject: %s" % (message.subject or ""),
            "Date: %s" % (message.date.isoformat() if message.date else ""),
            "Body:",
            message.body or "(empty)",
        ]
    )
    return llm_json(prompt, PLAIN_EMAIL_SCHEMA)
