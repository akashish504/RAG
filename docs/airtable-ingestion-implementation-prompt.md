# Airtable Attachment Ingestion — Implementation Prompt

Implement a **separate Airtable ingestion pipeline** that syncs attachment
fields from Airtable into S3 raw storage. Keep this module isolated from the
existing embedding/chunking pipeline.

## Constraints

- Reuse `src/dalberg_mcp/airtable_ingestion/data_extract.py` for Airtable API
  connectivity and metadata access. Do not duplicate Airtable connection logic.
- Do not modify reader/parser/chunker/embedder/indexer behavior in the
  embedding pipeline.
- Keep all secrets in `.env`; do not hardcode credentials.

## Current Scope

- Database: `Profiles Sync (Claude)` (`app0ZvoNuWDMx4NeC`)
- Table: `Dalberg Profiles` (`tblCHqUZacTVV8WRb`)
- Identifier column: `Email`
- Attachment columns:
  - `CV Attachment`
  - `Bio Attachment`

## Required Output Layout

```text
raw/<table_name>/<identifier>/<column_name>/<attachment_id>__<filename>
```

Example:

```text
raw/dalberg_profiles/john@example.com/cv_attachment/att123__john_cv.pdf
raw/dalberg_profiles/john@example.com/bio_attachment/att456__john_bio.pdf
```

## Functional Requirements

1. Load ingestion config from `config/airtable_ingestion.yaml`.
2. Discover and persist Airtable schema metadata:
   - list bases and tables
   - table columns and types
   - save locally and optionally upload to S3 metadata prefix
3. Fetch table records with pagination.
4. For each configured attachment field:
   - download attachment to a temp file
   - upload to S3 using normalized path components
5. Support optional text field sync (JSON/TXT) for future expansion.
6. Emit run summary and error list.

## Extensibility Requirements

- New tables/databases/columns should be added by config only.
- Keep extraction, transformation, and S3 upload as separate components.
- Preserve normalized metadata for future incremental sync and audit logging.

## Deliverables

- `src/dalberg_mcp/airtable_ingestion/` module
- `config/airtable_ingestion.yaml`
- `scripts/discover_airtable_schema.py`
- `scripts/run_airtable_ingestion.py`
- `requirements-airtable-ingestion.txt`
- docs: `docs/airtable-ingestion.md`
