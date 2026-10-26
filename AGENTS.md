# MonthClose AI
Goal: an AI agent that closes the month for a Portuguese micro-company.
Rule: no bank line without a supporting document.

Flow: mailbox (emails + PDF attachments) -> extract data with Grok (JSON)
-> validate in code (totals, VAT, tax ID, duplicates) -> file in Google Drive
-> record in bookkeeping API with the Drive link -> import bank statement CSV
-> link every bank line to its documents -> close checklist -> monthly report.

Stack: Python, FastAPI + SQLite (SQLModel), xAI API (OpenAI-compatible), pdfplumber.
Principles: AI decides, code checks the math, human approves low-confidence items.
All data is synthetic (data/inbox, data/history). Never read the answer key.
Language: code, comments and README in English.
Mailbox is an abstraction: first source = local folder of .eml files,
later = Gmail API. Same interface.

- Python 3.9: use Optional[X], never "X | None". No match statements.
- Money is Decimal, never float. The LLM extracts and proposes; code verifies all arithmetic.
- Store confidence (0-1) and a one-line justification for every AI decision.
- Never open or read the pacote_lab folder or the gabarito.
- Secrets only in .env, never in code or chat.