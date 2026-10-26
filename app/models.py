from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Column, Numeric, UniqueConstraint
from sqlmodel import Field, SQLModel


class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    snc_account: Optional[str] = None
    description: Optional[str] = None


class Period(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    month: str = Field(index=True, unique=True)  # YYYY-MM
    status: str = Field(default="open", index=True)  # open | closed
    closed_at: Optional[datetime] = None
    notes: Optional[str] = None


class Document(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: Optional[str] = Field(default=None, index=True, unique=True)
    period: str = Field(index=True)
    document_date: date
    payment_date: Optional[date] = None
    tipo: str = Field(index=True)
    category_id: Optional[int] = Field(default=None, foreign_key="category.id")
    entity: str
    nif: Optional[str] = None
    document_number: str = Field(index=True)
    base: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    vat_rate: Decimal = Field(sa_column=Column(Numeric(6, 2), nullable=False))
    vat: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    total: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    deductible_vat: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    payment_method: Optional[str] = None
    status: str = Field(default="pendente", index=True)
    notes: Optional[str] = None
    file_path: Optional[str] = None
    original_filename: Optional[str] = None
    direction: Optional[str] = None  # sale | purchase
    referenced_invoice: Optional[str] = None
    lines_json: Optional[str] = None
    drive_link: Optional[str] = None
    confidence: float = 1.0
    justification: str = "manual"


class BankLine(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    period: str = Field(index=True)
    account_iban: Optional[str] = None
    movement_date: date
    value_date: Optional[date] = None
    description: str
    debit: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(14, 2), nullable=True)
    )
    credit: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(14, 2), nullable=True)
    )
    amount: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    balance: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(14, 2), nullable=True)
    )
    status: str = Field(default="unsupported", index=True)
    confidence: float = 1.0
    justification: str = "imported from bank statement"


class Link(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("document_id", "bank_line_id", name="uq_link_doc_bank"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: int = Field(foreign_key="document.id", index=True)
    bank_line_id: int = Field(foreign_key="bankline.id", index=True)
    allocated_amount: Decimal = Field(
        sa_column=Column(Numeric(14, 2), nullable=False)
    )
    method: str
    explanation: str
    confidence: float


class OpenItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    document_id: Optional[int] = Field(default=None, foreign_key="document.id")
    period: str = Field(index=True)
    kind: str  # receivable | payable | invoice_to_request | missing_document
    description: str
    amount: Decimal = Field(sa_column=Column(Numeric(14, 2), nullable=False))
    due_date: Optional[date] = None
    status: str = Field(default="open", index=True)
    notes: Optional[str] = None
    assignee: Optional[str] = None  # owner | (unset for ledger items)
    draft_email: Optional[str] = None
    confidence: float = 1.0
    justification: str = "open item from history or inbox"


class DocumentCreate(SQLModel):
    source_id: Optional[str] = None
    document_date: date
    payment_date: Optional[date] = None
    tipo: str
    category_code: str
    snc_account: Optional[str] = None
    entity: str
    nif: Optional[str] = None
    document_number: str
    base: Decimal
    vat_rate: Decimal
    vat: Decimal
    total: Decimal
    deductible_vat: Decimal
    payment_method: Optional[str] = None
    status: str = "pendente"
    notes: Optional[str] = None
    file_path: Optional[str] = None
    original_filename: Optional[str] = None
    direction: Optional[str] = None
    referenced_invoice: Optional[str] = None
    lines_json: Optional[str] = None
    drive_link: Optional[str] = None
    confidence: float = 1.0
    justification: str = "manual"
    period: Optional[str] = None


class BankStatementImport(SQLModel):
    path: Optional[str] = None
    content: Optional[str] = None


class LinkCreate(SQLModel):
    document_id: int
    bank_line_id: int
    allocated_amount: Decimal
    method: str
    explanation: str
    confidence: float
