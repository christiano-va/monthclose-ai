"""Runtime settings. Secrets and the company NIF come from .env only."""
import os

from dotenv import load_dotenv

load_dotenv()

# Portuguese NIF used on invoices to a final consumer. It passes the mod-11
# check and is shared by many customers, so it is not one memory key.
FINAL_CONSUMER_NIF = "999999990"


def company_nif():
    # type: () -> str
    """NIF of the company that issues the sales invoices."""
    raw = os.getenv("COMPANY_NIF") or ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 9:
        raise RuntimeError("COMPANY_NIF must be set in .env to a 9-digit NIF")
    return digits


def scan_code():
    # type: () -> str
    """Shared access code for the phone scan page. Empty when unset."""
    return (os.getenv("SCAN_CODE") or "").strip()
