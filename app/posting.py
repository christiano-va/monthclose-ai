"""Direction, tipo, settlement and suspect rules. The model proposes; this decides."""
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlmodel import Session, select

from app.models import Category, Document
from app.money import ZERO, money_equal, to_money
from app.settings import FINAL_CONSUMER_NIF, company_nif
from app.validate import classify_nif

# Ledger tipos. Settlements and suspect rows stay off this list so they are
# not summed as revenue or expense.
TIPO_RECEITA = "receita"
TIPO_GASTO = "gasto"
TIPO_ESTADO = "estado"
TIPO_PESSOAL = "pessoal"
TIPO_INVESTIMENTO = "investimento"
TIPO_SETTLEMENT = "liquidacao"
TIPO_SUSPECT = "suspeito"

CATEGORY_SETTLEMENT = "SETTLEMENT"
CATEGORY_SUSPECT = "SUSPECT"
ACCOUNT_CUSTOMERS = "211"
ACCOUNT_SUPPLIERS = "221"
ACCOUNT_PARTNER = "2781"
ACCOUNT_SOCIAL_SECURITY = "245"
ACCOUNT_IRS_WITHHOLDING = "2421"
ACCOUNT_IRC = "2411/2415"
ACCOUNT_VAT = "2436"

CATEGORY_PARTNER = "SOCIO_CONTA_CORRENTE"
CATEGORY_SOCIAL_SECURITY = "ESTADO_SEGURANCA_SOCIAL"
CATEGORY_IRS_WITHHOLDING = "ESTADO_RETENCOES_IRS"

# personal_account is recorded, but alone it never makes a document suspect.
SUSPECT_RISK_FLAGS = (
    "iban_changed",
    "iban_new",
    "urgency_language",
    "short_payment_deadline",
)

LIABILITY_ACCOUNT = {
    CATEGORY_SOCIAL_SECURITY: ACCOUNT_SOCIAL_SECURITY,
    CATEGORY_IRS_WITHHOLDING: ACCOUNT_IRS_WITHHOLDING,
    "ESTADO_IRC": ACCOUNT_IRC,
    "ESTADO_IRC_PPC": ACCOUNT_IRC,
    "ESTADO_IVA": ACCOUNT_VAT,
}

OWNER_TASK_KINDS = (
    "missing_document",
    "missing_sales_invoice",
    "invoice_to_request",
    "confirm_late_document",
    "confirm_explanation",
    "awaiting_bank_line",
)

_INVOICE_RE = re.compile(
    r"\b((?:FT|FR|FS|NC|ND|RG)[\s./-]*[A-Z0-9]*\d[A-Z0-9/\-]*)",
    re.IGNORECASE,
)

_ASSET_MARKERS = (
    "cadeira",
    "mobiliario",
    "mobiliário",
    "portatil",
    "portátil",
    "computador",
    "impressora",
)

_LODGING_MARKERS = ("alojamento", "estadia", "noite", "noites", "hospede", "hóspede")
_PRINT_MARKERS = ("impressao 3d", "impressão 3d", "impressao", "impressão", "3d")


def norm_nif(value):
    # type: (Optional[str]) -> str
    kind, cleaned = classify_nif(value)
    if kind == "absent":
        return ""
    return cleaned


def is_company_nif(value):
    # type: (Optional[str]) -> bool
    return norm_nif(value) == company_nif()


def is_final_consumer(value):
    # type: (Optional[str]) -> bool
    return norm_nif(value) == FINAL_CONSUMER_NIF


def resolve_direction(issuer_nif):
    # type: (Optional[str]) -> str
    """Sale when this company issued the document, otherwise a purchase."""
    if is_company_nif(issuer_nif):
        return "sale"
    return "purchase"


def resolve_counterparty(
    direction,
    issuer_name,
    issuer_nif,
    recipient_name,
    recipient_nif,
    payee_name,
    payee_nif,
):
    # type: (str, str, Optional[str], str, Optional[str], str, Optional[str]) -> Tuple[str, Optional[str]]
    """The other party: the customer on a sale, the supplier on a purchase.

    A bank proof names the bank as issuer and the company as account holder.
    The party that was paid is the counterparty when it is not the company.
    """
    if direction == "sale":
        name = (recipient_name or "").strip() or "unknown"
        return name, norm_nif(recipient_nif) or None

    payee = (payee_name or "").strip()
    payee_id = norm_nif(payee_nif)
    if payee and not is_company_nif(payee_nif):
        return payee, payee_id or None
    name = (issuer_name or "").strip() or "unknown"
    return name, norm_nif(issuer_nif) or None


def find_referenced_invoices(explicit, text, own_number):
    # type: (Optional[str], str, str) -> str
    """Invoice numbers this document settles, excluding its own number."""
    found = []  # type: List[str]
    own = _compact(own_number)

    def add(raw):
        # type: (str) -> None
        for match in _INVOICE_RE.finditer(str(raw or "")):
            text_value = " ".join(match.group(1).split())
            if not text_value or _compact(text_value) == own:
                continue
            if text_value not in found:
                found.append(text_value)

    if explicit:
        add(str(explicit))
    add(text or "")
    return ", ".join(found)


def _compact(value):
    # type: (Optional[str]) -> str
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def is_settlement(kind, settles_flag, referenced, text):
    # type: (str, bool, str, str) -> bool
    """A notice, receipt or payout that settles an invoice already issued.

    A fatura-recibo is the invoice itself. A rent or donation receipt is the
    expense, because it does not point at an earlier invoice.
    """
    if kind == "payment_notice":
        return True
    if kind == "payout_report":
        return True
    if kind != "receipt":
        return False
    blob = (text or "").lower()
    # The rent receipt and the donation receipt are the expense itself.
    if "renda" in blob or "donativo" in blob or "senhorio" in blob:
        return False
    if settles_flag or referenced:
        return True
    if "documentos liquidados" in blob or "fatura liquidada" in blob:
        return True
    if "liquidação total" in blob or "liquidacao total" in blob:
        return True
    return False


def is_tax_or_social(kind, text, counterparty):
    # type: (str, str, str) -> bool
    if kind == "tax_guide":
        return True
    if kind != "payment_proof":
        return False
    blob = "%s\n%s" % (counterparty or "", text or "")
    lowered = blob.lower()
    markers = (
        "segurança social",
        "seguranca social",
        "seg social",
        "autoridade tribut",
        "estado (at",
        "reten",
        "pagamento por conta",
        " irs",
        "irc",
    )
    return any(marker in lowered for marker in markers)


def tax_category_code(text, counterparty):
    # type: (str, str) -> Optional[str]
    """Liability category for a tax guide or its payment proof.

    The payment settles the liability. It is not a new cost: social security
    is 245 (not the 635 expense), IRS withholding is 2421, IRC is 2411/2415,
    VAT is 2436.
    """
    blob = ("%s\n%s" % (counterparty or "", text or "")).lower()
    compact = "".join(ch for ch in blob if ch.isalnum())
    if (
        "segurança social" in blob
        or "seguranca social" in blob
        or "seg social" in blob
        or "segurancasocial" in compact
    ):
        return CATEGORY_SOCIAL_SECURITY
    if "por conta" in blob or "pagamentoporconta" in compact:
        return "ESTADO_IRC_PPC"
    irs_word = "irs" in blob
    withholding = (
        "reten" in blob
        or "retenc" in compact
        or "fonte" in blob
        or "predia" in blob
        or "trabalho dependente" in blob
    )
    if irs_word and withholding:
        return CATEGORY_IRS_WITHHOLDING
    if irs_word and "por conta" not in blob and re.search(r"\birc\b", blob) is None:
        if "comprovativo" in blob or "guia" in blob or "autoridade" in blob:
            return CATEGORY_IRS_WITHHOLDING
    if re.search(r"\biva\b", blob) and (
        "autoridade tribut" in blob or "estado" in blob or "guia" in blob
    ):
        return "ESTADO_IVA"
    if re.search(r"\birc\b", blob):
        return "ESTADO_IRC"
    return None


def liability_account(category_code):
    # type: (Optional[str]) -> Optional[str]
    if not category_code:
        return None
    return LIABILITY_ACCOUNT.get(category_code)


def suspect_flags(flags):
    # type: (Sequence[str]) -> List[str]
    """Flags that keep a document out of the books. personal_account is not one."""
    found = []  # type: List[str]
    for name in SUSPECT_RISK_FLAGS:
        if name in (flags or []):
            found.append(name)
    return found


def is_fixed_asset(text):
    # type: (str) -> bool
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _ASSET_MARKERS)


def asset_category_code(text):
    # type: (str) -> str
    lowered = (text or "").lower()
    if "3d" in lowered or "impressora" in lowered:
        return "EQUIPAMENTO_3D"
    return "EQUIPAMENTO_ADMIN"


def consumer_sale_category(text):
    # type: (str) -> Optional[str]
    """999999990 is shared by lodging and 3D sales, so the main line decides."""
    lowered = (text or "").lower()
    if any(marker in lowered for marker in _LODGING_MARKERS):
        return "REC_AL"
    if any(marker in lowered for marker in _PRINT_MARKERS):
        return "REC_3D"
    if "consultoria" in lowered:
        return "REC_CONSULTORIA"
    return None


def is_meal_topup(text):
    # type: (str) -> bool
    lowered = (text or "").lower()
    return "refei" in lowered or "carregamento" in lowered or "subsídio de refeição" in lowered or "subsidio de refeicao" in lowered


def resolve_tipo(direction, kind, category_code, settlement, tax_payment, asset):
    # type: (str, str, Optional[str], bool, bool, bool) -> str
    """Map direction and kind onto the history ledger tipos.

    A settlement is not receita or gasto. A suspect document has no tipo
    that would enter the ledger; the caller uses TIPO_SUSPECT instead.
    """
    if settlement:
        return TIPO_SETTLEMENT
    if kind == "payslip" or (category_code or "").startswith("PESSOAL_"):
        if tax_payment:
            return TIPO_ESTADO
        return TIPO_PESSOAL
    if tax_payment or kind == "tax_guide":
        return TIPO_ESTADO
    if asset or (category_code or "").startswith("EQUIPAMENTO_"):
        return TIPO_INVESTIMENTO
    if direction == "sale":
        return TIPO_RECEITA
    return TIPO_GASTO


def deductible_vat(tipo, direction, vat):
    # type: (str, str, Decimal) -> Decimal
    if tipo in (TIPO_GASTO, TIPO_INVESTIMENTO) and direction == "purchase":
        return to_money(vat)
    return ZERO


def should_split_lines(lines):
    # type: (Sequence[Dict[str, Any]]) -> bool
    """A top-up bundled with a fee is booked in parts, not as the small fee."""
    natures = set()
    for line in lines:
        natures.add(line.get("nature") or "supply")
    return "topup" in natures and "fee" in natures


def main_line(lines):
    # type: (Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]
    if not lines:
        return None
    best = lines[0]
    for line in lines:
        if abs(line["total"]) > abs(best["total"]):
            best = line
    return best


def group_by_nature(lines):
    # type: (Sequence[Dict[str, Any]]) -> List[Tuple[str, List[Dict[str, Any]]]]
    order = []  # type: List[str]
    groups = {}  # type: Dict[str, List[Dict[str, Any]]]
    for line in lines:
        nature = str(line.get("nature") or "supply")
        if nature not in groups:
            order.append(nature)
            groups[nature] = []
        groups[nature].append(line)
    ranked = sorted(order, key=lambda nature: _group_abs(groups[nature]), reverse=True)
    return [(nature, groups[nature]) for nature in ranked]


def _group_abs(lines):
    # type: (Sequence[Dict[str, Any]]) -> Decimal
    total = ZERO
    for line in lines:
        total += abs(line["total"])
    return total


def sum_lines(lines):
    # type: (Sequence[Dict[str, Any]]) -> Tuple[Decimal, Decimal, Decimal]
    base = ZERO
    vat = ZERO
    total = ZERO
    for line in lines:
        base += line["base"]
        vat += line["vat"]
        total += line["total"]
    return to_money(base), to_money(vat), to_money(total)


def primary_rate(lines):
    # type: (Sequence[Dict[str, Any]]) -> Decimal
    if not lines:
        return ZERO
    best = main_line(lines)
    if best is None:
        return ZERO
    return to_money(best["vat_rate"])


def ensure_category(session, code, snc, description):
    # type: (Session, str, Optional[str], str) -> Category
    row = session.exec(select(Category).where(Category.code == code)).first()
    if row is None:
        row = Category(code=code, snc_account=snc, description=description)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    if snc and not row.snc_account:
        row.snc_account = snc
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def ensure_special_categories(session):
    # type: (Session) -> None
    ensure_category(
        session,
        CATEGORY_SETTLEMENT,
        "%s/%s" % (ACCOUNT_CUSTOMERS, ACCOUNT_SUPPLIERS),
        "Settlement of an invoice already issued (211 customers, 221 suppliers)",
    )
    ensure_category(
        session,
        CATEGORY_SUSPECT,
        None,
        "Suspect document: no accounting entry, kept for review, excluded from linking",
    )
    ensure_category(
        session,
        CATEGORY_PARTNER,
        ACCOUNT_PARTNER,
        "Partner or manager current account for a reimbursement of expenses",
    )
    ensure_category(
        session,
        CATEGORY_SOCIAL_SECURITY,
        ACCOUNT_SOCIAL_SECURITY,
        "Social security liability settled by the payment guide (not the TSU expense)",
    )
    ensure_category(
        session,
        CATEGORY_IRS_WITHHOLDING,
        ACCOUNT_IRS_WITHHOLDING,
        "IRS withheld at source, payable to the tax authority",
    )


def history_tipo_for_nif(session, nif, tipo):
    # type: (Session, Optional[str], str) -> bool
    wanted = norm_nif(nif)
    if not wanted or is_final_consumer(wanted) or is_company_nif(wanted):
        return False
    rows = session.exec(
        select(Document).where(Document.justification == "loaded from data/history JSON")
    ).all()
    for document in rows:
        if norm_nif(document.nif) == wanted and document.tipo == tipo:
            return True
    return False


def settlement_account(session, kind, direction, counterparty_nif):
    # type: (Session, str, str, Optional[str]) -> str
    """211 when the other party is a customer, 221 when it is a supplier."""
    if kind == "payout_report" or direction == "sale":
        return ACCOUNT_CUSTOMERS
    if history_tipo_for_nif(session, counterparty_nif, TIPO_RECEITA):
        return ACCOUNT_CUSTOMERS
    if history_tipo_for_nif(session, counterparty_nif, TIPO_GASTO):
        return ACCOUNT_SUPPLIERS
    return ACCOUNT_SUPPLIERS


def excluded_from_linking(category_code, notes, status=None):
    # type: (Optional[str], Optional[str], Optional[str]) -> bool
    if status in ("duplicate", "error", "no_document"):
        return True
    if category_code == CATEGORY_SUSPECT:
        return True
    text = notes or ""
    return "excluded_from_linking: yes" in text


def excluded_from_totals(category_code, notes, status=None):
    # type: (Optional[str], Optional[str], Optional[str]) -> bool
    """Duplicates and suspect rows do not enter revenue, expense or tax totals."""
    if status in ("duplicate", "error", "no_document"):
        return True
    if category_code == CATEGORY_SUSPECT:
        return True
    text = notes or ""
    return "excluded_from_totals: yes" in text


def counts_as_cost(tipo, status, category_code):
    # type: (Optional[str], Optional[str], Optional[str]) -> bool
    """Liability settlements, suspect rows and duplicates are not costs."""
    if excluded_from_totals(category_code, None, status):
        return False
    if tipo in (TIPO_SETTLEMENT, TIPO_SUSPECT, None, ""):
        return False
    return tipo in (TIPO_GASTO, TIPO_PESSOAL, TIPO_ESTADO, TIPO_INVESTIMENTO)


def note_value(notes, key):
    # type: (Optional[str], str) -> Optional[str]
    prefix = key + ": "
    for part in (notes or "").split(" | "):
        if part.startswith(prefix):
            return part[len(prefix) :].strip()
    return None


def set_note(notes, key, value):
    # type: (Optional[str], str, str) -> str
    prefix = key + ": "
    parts = []  # type: List[str]
    replaced = False
    for part in (notes or "").split(" | "):
        if not part:
            continue
        if part.startswith(prefix):
            parts.append(prefix + value)
            replaced = True
        else:
            parts.append(part)
    if not replaced:
        parts.append(prefix + value)
    return " | ".join(parts)


def risk_flag_names(notes):
    # type: (Optional[str]) -> List[str]
    raw = note_value(notes, "risk_flags") or ""
    found = []  # type: List[str]
    for piece in raw.split(";"):
        name = piece.strip()
        if name and name not in found:
            found.append(name)
    return found


def task_assignee(kind):
    # type: (str) -> Optional[str]
    if kind in OWNER_TASK_KINDS:
        return "owner"
    return None


def party_label(entity, nif):
    # type: (Optional[str], Optional[str]) -> str
    name = (entity or "").strip()
    if nif:
        return "%s (%s)" % (name, nif)
    return name
