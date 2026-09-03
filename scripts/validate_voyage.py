#!/usr/bin/env python3
"""Validate a Voyage API key with a minimal embed call.

Uses only the Python standard library — no pip installs needed.

Usage:
    python scripts/validate_voyage.py                    # key from env or .env
    python scripts/validate_voyage.py --key pa-xxxx      # key passed directly
    python scripts/validate_voyage.py --env-var VOYAGE_API_KEY_V2

Key lookup order: --key flag, then the env var (default VOYAGE_API_KEY)
from the process environment, then the same variable parsed out of .env
in the current directory or the repo root.

Exits 0 if the key works and the embedding matches the expected
dimensionality, non-zero otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
DEFAULT_MODEL = "voyage-4"
DEFAULT_DIMS = 1024


def read_env_file(path: Path, var: str) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith(f"{var}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def resolve_key(args: argparse.Namespace) -> str | None:
    if args.key:
        return args.key
    if os.environ.get(args.env_var):
        return os.environ[args.env_var]
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        key = read_env_file(candidate, args.env_var)
        if key:
            print(f"Using {args.env_var} from {candidate}")
            return key
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Voyage API key.")
    parser.add_argument("--key", help="API key (overrides env lookup)")
    parser.add_argument(
        "--env-var",
        default="VOYAGE_API_KEY",
        help="Env/.env variable holding the key (default: VOYAGE_API_KEY)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dims", type=int, default=DEFAULT_DIMS)
    args = parser.parse_args()

    key = resolve_key(args)
    if not key:
        print(f"FAIL: no key found (checked --key, ${args.env_var}, .env)")
        return 2

    print(f"Key prefix: {key[:6]}...")
    print(f"Model: {args.model} | expected dims: {args.dims}")

    request = urllib.request.Request(
        VOYAGE_URL,
        data=json.dumps(
            {"input": ["passage: hello world test"], "model": args.model}
        ).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"FAIL: HTTP {exc.code} from Voyage API")
        print(f"Response: {detail}")
        if exc.code == 401:
            print("The key is invalid or revoked.")
        elif exc.code == 429:
            print("The key works but is rate-limited; try again shortly.")
        return 1
    except urllib.error.URLError as exc:
        print(f"FAIL: could not reach Voyage API: {exc.reason}")
        return 1

    embedding = body["data"][0]["embedding"]
    tokens = body.get("usage", {}).get("total_tokens")
    print(f"HTTP 200 | dims returned: {len(embedding)} | tokens used: {tokens}")

    if len(embedding) != args.dims:
        print(f"FAIL: expected {args.dims} dims, got {len(embedding)}")
        return 1

    print("OK: key is valid and dimensions match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
