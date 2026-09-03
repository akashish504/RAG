#!/usr/bin/env python3
"""Export an Airtable table's schema from the Meta API into a JSON snapshot.

Now driven by ``config/retrieval_sources.yaml``: pick a logical source by
name and the script reads its ``base_id``, ``table_name``, and
``schema_snapshot_path`` from there.

Run locally with ``.env`` containing ``AIRTABLE_PAT_TOKEN`` and ``BASE_ID``
(plus any other env vars the YAML expands).

Examples::

    cd /path/to/tailoredai
    python scripts/export_airtable_schema_snapshot.py                       # default: dalberg_profiles
    python scripts/export_airtable_schema_snapshot.py --source d_quals     # any other configured source
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from pipeline.airtable_ingestion.data_extract import AirtableConnector  # noqa: E402


def _pick_table(
    tables: list[dict],
    *,
    table_name: str,
) -> dict:
    for table in tables:
        if table.get("name") == table_name:
            return table
    known = ", ".join(sorted(str(t.get("name", "")) for t in tables)) or "(none)"
    raise SystemExit(f"No table named {table_name!r}. Known: {known}")


def _base_name(conn: AirtableConnector, base_id: str) -> str | None:
    for base in conn.list_bases():
        if base.get("id") == base_id:
            n = base.get("name")
            return str(n) if n is not None else None
    return None


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)

    parser = argparse.ArgumentParser(
        description="Export an Airtable table schema snapshot for a configured source."
    )
    parser.add_argument(
        "--source",
        default="dalberg_profiles",
        help="Logical source name from config/retrieval_sources.yaml (default: dalberg_profiles).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Override the output path; defaults to the source's configured schema_snapshot_path.",
    )
    args = parser.parse_args()

    from retrieval.config import load_config
    from retrieval.settings import get_runtime_settings

    cfg = load_config()
    if args.source not in cfg.sources:
        raise SystemExit(
            f"Unknown source {args.source!r}. Configured: {sorted(cfg.sources)}"
        )
    src_cfg = cfg.sources[args.source]
    if src_cfg.airtable is None:
        raise SystemExit(f"source {args.source!r} has no Airtable configuration.")

    runtime = get_runtime_settings()
    base_id = src_cfg.airtable.base_id
    table_name = src_cfg.airtable.table_name
    out_path = Path(args.out) if args.out else src_cfg.airtable.schema_snapshot_path
    if not out_path.is_absolute():
        out_path = PROJECT_ROOT / out_path

    conn = AirtableConnector(pat_token=runtime.airtable_pat_token, base_id=base_id)
    tables = conn.fetch_tables_metadata(base_id)
    table_def = _pick_table(tables, table_name=table_name)
    fields = table_def.get("fields") or []

    payload = {
        "source": "snapshot",
        "snapshot_version": 1,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "logical_source": args.source,
        "base_id": base_id,
        "base_name": _base_name(conn, base_id),
        "table_id": table_def.get("id"),
        "table_name": table_def.get("name"),
        "primary_field_id": table_def.get("primaryFieldId"),
        "field_count": len(fields) if isinstance(fields, list) else 0,
        "table": table_def,
        "views": table_def.get("views") or [],
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
