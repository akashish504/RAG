# Airtable Attachment Ingestion

This is a separate pipeline from embedding/chunking. It syncs selected Airtable
fields into S3 raw storage using:

```text
raw/<table_name>/<identifier>/<column_name>/<file>
```

## Current active target

- Database: `Profiles Sync (Claude)` (`app0ZvoNuWDMx4NeC`)
- Table: `Dalberg Profiles` (`tblCHqUZacTVV8WRb`)
- Identifier column: `Email`
- Attachment columns: `CV Attachment`, `Bio Attachment`

Configured in `config/airtable_ingestion.yaml` under `targets.profiles_sync`.

## Module structure

```text
src/dalberg_mcp/airtable_ingestion/
├── data_extract.py     # Reused Airtable connector logic
├── airtable_client.py  # Thin adapter + pagination + attachment extraction
├── config.py           # YAML + env loader
├── normalizers.py      # Safe table/email/column/file naming
├── s3_uploader.py      # Upload bytes/files/json to S3
├── schema.py           # Schema discovery + local/S3 persistence
└── pipeline.py         # End-to-end sync flow
```

## Scripts

- `scripts/discover_airtable_schema.py`
  - Lists all accessible bases/tables.
  - Retrieves table schemas (field names/types/ids).
  - Stores schema metadata locally and optionally in S3.

- `scripts/run_airtable_ingestion.py --target profiles_sync`
  - Reads target config.
  - Discovers + stores schema for the target table.
  - Fetches records with pagination.
  - Downloads each configured attachment to a temp file.
  - Uploads to S3:

    ```text
    raw/dalberg_profiles/john@example.com/cv_attachment/<attachment_id>__<filename>
    raw/dalberg_profiles/john@example.com/bio_attachment/<attachment_id>__<filename>
    ```

## Configuration-driven extension

To add a new table/database later:

1. Add a new `targets.<name>` entry in `config/airtable_ingestion.yaml`.
2. Set `database_id`, `table_name`, `identifier_column`.
3. Add `attachment_columns` and optional `text_columns`.
4. Enable it with `enabled: true`.

No code changes are needed for new targets.
