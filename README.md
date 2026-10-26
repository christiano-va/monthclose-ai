# MonthClose AI

MonthClose closes the books of a Portuguese micro-company for one month.

The rule is: no bank line without a supporting document. Mail comes in as `.eml` files (later the same mailbox interface can talk to Gmail). Each PDF is read into JSON by the model. Code then checks the totals, the VAT and the tax id, files the document, and records it. A bank statement CSV is imported and every line is linked to the documents that support it. What is still missing becomes an owner task. The owner can accept those items and close the month. A later document or a plain-text explanation can resolve an item that was still open, including one the owner already accepted. A closed month is not reopened; the resolution is reported beside the acceptance it replaces.

The model proposes. Code checks the arithmetic. A low-confidence decision waits for a person. Amounts are `Decimal`, never `float`. The sample company data under `data/inbox` and `data/history` is synthetic.

## Install

Python 3.9.

```bash
python3.9 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project root. Do not commit it. It needs:

- `XAI_API_KEY` — key for the xAI API
- `XAI_MODEL` — model name sent to that API
- `COMPANY_NIF` — 9-digit tax id of the company that issues the sales invoices

## Run the API

```bash
source venv/bin/activate
uvicorn app.main:app --reload
```

Interactive docs are at `http://127.0.0.1:8000/docs`.

## Commands

Rebuild the demo database from scratch. This deletes `monthclose.db`, loads `data/history`, processes `data/inbox`, imports the September statement, and links bank lines to documents. September 2026 stays **open**, with its pending tasks still open.

```bash
python -m app.demo_reset
```

The same rebuild, then the owner accepts the pending items and September 2026 stays **closed**:

```bash
python -m app.demo_reset --closed
```

Process every email in the local inbox (safe to run again; stored attachments are skipped):

```bash
python -m app.process_all
python -m app.process_all data/inbox --reprocess
```

Process one email:

```bash
python -m app.process_one data/inbox/some-message.eml
```

Process one scanned PDF. There is no sender and no email body. `--note` is optional text typed with the scan. The match uses the document content (amount, counterparty, date, purchase or sale), not the file name.

```bash
python -m app.process_scan data/demo/documentos/compra_PTDomain_FT_PTD-2026-77310.pdf
python -m app.process_scan path/to/scan.pdf --note "fatura da renovação de 17/09"
```

Link every bank line. `--relink` drops previous links and computes them again. The command also prints the September checklist.

```bash
python -m app.link_all
python -m app.link_all --relink --month 2026-09
```

Load history on its own (also done by `demo_reset`):

```bash
python -m app.load_history
```

## What a late document does

A new document, from an email or from a scan, is compared with pending items of every month, including closed months. The candidates are open or owner-accepted `missing_document` and `missing_sales_invoice` tasks, and bank lines that are still unsupported.

The score uses the amount (the bank line, or the gross of a missing sales invoice), the counterparty name or tax id, a nearby date, and whether the document is a purchase or a sale. When the email body or the scan note adds context, that text and the candidates go to the model, which returns one JSON choice and a confidence. The sender is ignored.

- Confidence at least 0.90 and an exact amount: the document is linked with method `late_document` and the pending item becomes `resolved_by_late_document`. An earlier owner acceptance stays on the item. A sales invoice that fills a missing payout booking is linked for the gross, and the payout report becomes evidence.
- Medium confidence: nothing is linked. The owner gets a `confirm_late_document` task with the proposed match and the reasons.
- Low confidence, or no candidate: the file is stored as an ordinary new document.

An email with no attachment can be an explanation. The text is matched the same way. The email is stored as an explanation record. A customer advance is proposed to account 218, not as revenue. It is linked only at confidence at least 0.90 with an exact amount; otherwise the owner gets a `confirm_explanation` task.

A document resolves one pending item, or the exact set of lines its amount covers. A document that is already fully linked is not reused.

If the resolved item belongs to a closed month, the month stays closed. A `post_close_resolution` row is stored and `GET /reports/{month}` lists it under `resolved_after_close`, next to the accepted item it replaces.

When an invoice arrives from an issuer that still has an unapplied credit note, the credit is applied. The amount payable is the invoice total minus the credit. If the invoice already prints that deduction, it is not subtracted a second time. The application appears on the checklist and on the monthly report as `credit_applications`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Service name |
| `POST` | `/documents` | Store one document. `base + vat` must equal `total`. |
| `GET` | `/documents?period=YYYY-MM` | List documents, optionally for one month |
| `POST` | `/bank-statements` | Import a statement CSV from `path` or `content` |
| `GET` | `/bank-lines?period=YYYY-MM&status=unsupported` | List bank lines |
| `POST` | `/links` | Link a document to a bank line |
| `POST` | `/scans` | Process one scanned PDF. Body: `{"path": "....pdf", "note": ""}` |
| `GET` | `/periods/{month}/checklist` | Close checklist, accepted pendings, resolutions after close, credit applications |
| `POST` | `/periods/{month}/close` | Close the month. With open pendings, send `approvals`, `approved_by` and `timestamp` |
| `GET` | `/reports/{month}` | Month status, `accepted_pending_items`, `resolved_after_close`, `credit_applications` |

`month` is `YYYY-MM`.
