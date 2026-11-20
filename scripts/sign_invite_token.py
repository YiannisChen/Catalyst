#!/usr/bin/env python3
"""Offline helper for manually issuing a contract-shaped invite token.

Set CATALYST_INVITE_SIGNING_KEY to a strong value of at least 32 bytes in this
process environment, then pass a token id and an ISO-8601 expiry. This script
does not load dotenv files or persist the signing key or token.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "api"))

from catalyst_app.access_control import sign_invite_token  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-id", required=True)
    parser.add_argument("--expires-at", required=True, help="ISO-8601 timestamp with timezone")
    args = parser.parse_args()
    try:
        token = sign_invite_token(args.token_id, args.expires_at)
    except ValueError as exc:
        parser.error(str(exc))
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
