"""Run the Gmail and Drive OAuth flow once.

Prints only "authorized" on success. Tokens stay in token.json.
"""
import sys

from app.google_auth import GoogleAuthError, authorize_interactive


def main():
    # type: () -> None
    try:
        authorize_interactive()
    except GoogleAuthError as exc:
        print(exc.code, file=sys.stderr)
        raise SystemExit(1)
    print("authorized")


if __name__ == "__main__":
    main()
