# FAQ & Troubleshooting

Common questions and a symptom → cause → fix guide for the Dalberg MCP stack
(Airtable ingestion → S3 → embedding pipeline → OpenSearch → retrieval/MCP).

This is problem-oriented. For design/reference see [architecture.md](architecture.md),
[retrieval-pipeline.md](retrieval-pipeline.md), [reader-chunker.md](reader-chunker.md),
[airtable-lookup-integrity.md](airtable-lookup-integrity.md), and
[environment.md](environment.md).

---

## First-line diagnostics

Run these before digging deeper — most problems announce themselves here.

```bash
# 1. Is my env even loaded / pointing at the right AWS account & region?
python -c "import os; print({k: os.environ.get(k) for k in \
  ['MCP_ENV','AWS_REGION','S3_BUCKET','OPENSEARCH_INDEX','VOYAGE_MODEL']})"

# 2. Can I reach AWS with the creds I have?
aws sts get-caller-identity            # expect account 345568587892 in dev

# 3. Is the SQS queue wired? (safe, read-only; exit 0 = main queue exists)
python scripts/check_sqs.py            # add --profile <name> for SSO dev creds

# 4. What's actually in OpenSearch for a table?
python scripts/validate_opensearch.py --table d_quals

# 5. End-to-end retrieval smoke test (needs VOYAGE_API_KEY)
python scripts/validate_opensearch.py \
  --test-query "financial inclusion in East Africa" --table d_quals
```

Set `LOG_LEVEL=DEBUG` in `.env` to make the pipeline and retrieval server verbose.

---

## FAQ

**Q: Which `.env` values are actually required to run something?**
It depends on the stage:
- **Airtable ingestion** → `AIRTABLE_PAT_TOKEN`, `BASE_ID`, `TABLE_NAME` (or `TABLE_ID`), `S3_BUCKET`.
- **Embedding pipeline** → AWS creds (env keys or `AWS_PROFILE`), `S3_BUCKET`, `OPENSEARCH_ENDPOINT`/`OPENSEARCH_INDEX`, and `VOYAGE_API_KEY` (real embeddings are the default).
- **Retrieval / MCP server** → `OPENSEARCH_*`, `VOYAGE_API_KEY` (for query embedding **and** the reranker), and `ANTHROPIC_API_KEY` for NL query planning + readable answers.
See [.env.example](../.env.example) for the annotated full list.

**Q: The embedder — is it the stub or the real Voyage one?**
Real Voyage is the default (`--embedder voyage`). The stub only runs if you pass
`--embedder stub`. If your vectors look random/garbage, confirm you didn't leave
`stub` in a script. `scripts/run_pipeline.py` and `scripts/embed_missing.py` both
default to `voyage`.

**Q: `passage:` vs `query:` — what's the prefix convention?**
Documents are embedded with a `passage:`-style prefix at index time; queries with a
`query: ` prefix at search time. This is deliberate and must stay consistent — see
commit `348e1fc` (we reverted a Voyage `input_type` change to keep this convention).
Mixing them silently degrades recall.

**Q: I re-ran the pipeline but nothing changed. Why?**
By default the pipeline **skips documents whose hash already exists** in OpenSearch.
Force a re-embed with `--no-skip-unchanged`. To only fix facets/metadata without
re-embedding, use `--refresh-metadata`.

**Q: How do I embed only the documents that are missing from the index?**
`python scripts/embed_missing.py --table d_quals` (add `--dry-run` first to see the
list, `--limit N` for a smoke test).

**Q: Spreadsheet/Excel text isn't in my results — bug?**
No. Spreadsheet-derived text is intentionally skipped at embedding time (commit
`e1cb962`). Excel content goes through the `xlsx_content` normalizer path, not the
generic text embed.

**Q: How do I know the reranker actually ran vs. silently fell back?**
Grep the retrieval server logs for `reranker_applied` (ran) vs
`reranker_failed_using_rrf_order` (fell back — usually a missing `VOYAGE_API_KEY`
in the *retrieval* process, which is separate from the indexing process).

**Q: Do I need a new docker-compose service for a new script?**
No. The single `pipeline` service runs any script:
`docker compose run --rm pipeline python scripts/<script>.py …`. Add a service only
for a genuinely different long-running process (future MCP server / SQS worker). See
[docker-ec2.md](docker-ec2.md).

**Q: Where does a chunk trace back to its source record?**
Every chunk carries `table_name`, `primary_key`, `column_name`, and `source_s3_key`
provenance. The S3 layout is `raw/{table_name}/{primary_key}/{column_name}.txt`.

---

## Troubleshooting by area

### Environment & AWS credentials

| Symptom | Cause | Fix |
|---|---|---|
| `ProfileNotFound` / boto3 tries to load a profile named `""` | `AWS_PROFILE=` (empty) in `.env` is still read by boto3 | Leave it unset, or the code that pops empty `AWS_PROFILE` handles it (see [check_sqs.py](../scripts/check_sqs.py)). Prefer real creds or a named profile. |
| `NoCredentialsError` / `Unable to locate credentials` | No env keys, no `AWS_PROFILE`, not on an EC2 role | Set `AWS_PROFILE` for local SSO dev, or run on the EC2 instance with `mcp-dev-ec2-role`. |
| Calls hit the wrong account/region | `AWS_REGION` unset or wrong | Dev is `eu-west-1`, account `345568587892`. Confirm with `aws sts get-caller-identity`. |
| OpenSearch `403 AuthorizationException` | Endpoint is a **VPC** domain and you're outside the VPC, or SigV4 identity lacks access | Run from EC2/VPC (SigV4 via IAM), or set `OPENSEARCH_USERNAME`/`OPENSEARCH_PASSWORD` for basic-auth dev access. Leave both blank to use SigV4. |

### Airtable ingestion

| Symptom | Cause | Fix |
|---|---|---|
| `ValueError: Missing Airtable PAT: set AIRTABLE_PAT_TOKEN in .env` | PAT not set | Create a PAT at https://airtable.com/create/tokens with `data.records:read` (+ `schema.bases:read` for metadata). |
| `ValueError: No bases accessible with this PAT token.` | PAT has no base access, or wrong base shared | Share the target base with the token; verify `BASE_ID` (starts with `app…`). |
| `KeyError: Unknown ingestion target '…'` | Bad `--target` name | Use a target defined in [config/airtable_ingestion.yaml](../config/airtable_ingestion.yaml) (e.g. `profiles_sync`). |
| `FileNotFoundError: Missing ingestion config` | `AIRTABLE_INGESTION_CONFIG_PATH` wrong | Default is `config/airtable_ingestion.yaml`; check the path. |
| Linked/lookup fields resolve to record IDs instead of values | Lookup-integrity edge cases | See [airtable-lookup-integrity.md](airtable-lookup-integrity.md); re-run schema discovery: `python scripts/discover_airtable_schema.py`. |
| Schema drift / planner using stale fields | Frozen schema snapshot out of date | Regenerate: `python scripts/export_airtable_schema_snapshot.py` (writes `config/*_schema.json`). |

### Embedding pipeline (S3 → chunk → embed → index)

| Symptom | Log / error | Cause & fix |
|---|---|---|
| A document silently produced no chunks | `document_failed` (with `exc_info`) | Parser/normalizer threw. Check the source file; corrupt docs get a chunk cap (commit `dc74e9f`). Failed docs are recorded to a re-extraction manifest (commit `c148221`). |
| Some chunks embedded, some didn't | `document_embedding_incomplete` (`missing=…`) | A Voyage batch failed for part of the doc. Re-run with `--no-skip-unchanged`, or `embed_missing.py` to backfill. |
| Indexing threw for one doc | `document_index_failed` | OpenSearch rejected the bulk write (mapping mismatch, oversized field). Inspect with `validate_opensearch.py --s3-key <key> --show-chunks`. |
| `duplicate_chunk_ids_after_chunking` (warning) | Same chunk id generated twice | Handled (dupes removed) but signals a chunker/id edge case; note the `key` and check the source. |
| PPTX slide count looks wrong / slides missing | Slide guard + parent-child chunker | See [chunking-strategies.md](chunking-strategies.md); the `pptx_slide` chunker treats slide=parent, blocks=children. |
| `render: LibreOffice unavailable — text-only extraction (no images)` | LibreOffice not installed in the runtime | Slide image rendering is skipped, so visual enrichment degrades. Install LibreOffice in the image (it's in the pipeline Dockerfile) or accept text-only. |
| Large deck rejected / truncated | Per-doc chunk limits | Limits were raised for large decks (commit `e1cb962`); if still hit, the doc is likely pathological — check the manifest. |

### Voyage embedder

| Symptom | Cause | Fix |
|---|---|---|
| `ImportError` on `voyageai` | `[voyage]`/deps not installed | Install the embedder extras; the client is imported lazily and raises a clear `ImportError`. |
| `voyage_batch_failed` in logs | 429 rate limit / 5xx / timeout | Retryable errors auto-retry (tenacity). Persistent 429 → lower `EMBED_BATCH_SIZE` (default 32) or throttle. Non-retryable → that batch is skipped and surfaced as `document_embedding_incomplete`. |
| Auth error from Voyage | Bad/absent `VOYAGE_API_KEY` | Set it in the process that's embedding. **Indexing and retrieval are separate processes** — both need their own key. |
| Dim mismatch on index | `VOYAGE_EMBED_DIMS` ≠ index mapping | Index mapping is `dims=1024`; keep `VOYAGE_EMBED_DIMS=1024` and `VOYAGE_MODEL=voyage-4` aligned with the created index. |

### OpenSearch indexing & validation

| Symptom | Cause | Fix |
|---|---|---|
| `index_not_found_exception` | Index never created | One-time: `python scripts/create_opensearch_index.py` (see [Quick start](../README.md)). |
| KNN returns nothing but docs exist | Wrong `chunk_type` searched, or dim/mapping mismatch | Only `chunk_type=child` vectors are searched (parents are hydrated by id). Verify counts with `validate_opensearch.py --table <t>`. |
| Bulk write partial failures | Oversized field / mapping conflict | Inspect the offending doc: `validate_opensearch.py --s3-key <key> --show-chunks`. |
| Index writes look non-atomic mid-run | Expected — writes are made atomic per commit `c501c15` | If you see a half-written index, re-run; the atomic index swap protects the live alias. |

### Retrieval / MCP query results

| Symptom | Log signal | Cause & fix |
|---|---|---|
| Results not reranked (worse ordering) | `reranker_failed_using_rrf_order` | `VOYAGE_API_KEY` missing in the **retrieval** process. Set it; reranker uses `rerank-2.5`. Fallback is graceful (RRF order), so it's easy to miss. |
| Lexical/BM25 seems off | `bm25_query_cleaned` shows the cleaned query | Confirms stop-word strip + synonym expansion ran (`dalberg_english` analyzer). If a domain term isn't matching, check the synonym list. |
| Facet filter too aggressive / empty results | `facet_filters_applied` | The `FacetPlanner` matched the query against real index facet values (cached 5 min). Caller-supplied filters always win; disable via `facet_filtering: false` in [config/retrieval_sources.yaml](../config/retrieval_sources.yaml). |
| One project fills all result slots | Dedup expected to prevent this | Dedup is by `primary_key`. Use `--no-dedup` in `validate_opensearch.py --test-query` to see raw hits while debugging. |
| NL planning / readable answer errors | `shape_classify_failed`, `answer_synth_failed` | Anthropic call failed. Check `ANTHROPIC_API_KEY`; set `ANTHROPIC_NL_READABLE_ANSWER=false` to skip the second pass and save tokens. |
| Airtable-backed source returns 503 | `source '…' has no Airtable adapter` | That logical source isn't wired to Airtable in [config/retrieval_sources.yaml](../config/retrieval_sources.yaml). |

### SQS

| Symptom | Cause | Fix |
|---|---|---|
| `check_sqs.py` → "No queue name" (exit 2) | Neither `SQS_QUEUE_NAME` nor `SQS_QUEUE_URL` set | Set one in `.env`. |
| `check_sqs.py` → "Main queue does NOT exist" (exit 1) | Queue not created in this account/region | Create the queue, or fix `AWS_REGION`/profile. |
| Exit 3 (READY-ish but warns) | Main queue exists, no DLQ | DLQ absence is a warning, not fatal. Wire a `RedrivePolicy` or create `<name>-dlq`. |
| Messages vanish but nothing processes | Visibility timeout / no worker | `SQS_VISIBILITY_TIMEOUT` default 300s; ensure the `worker` compose service is running (`docker compose ps worker`; start it with `scripts/reset_deployment.sh` or `docker compose up -d worker`). |
| Long records redelivered mid-job | Job exceeds the 300s visibility timeout | Idempotent skips + the job ledger make the duplicate pass cheap, but if it recurs raise the queue's visibility timeout (~900s) in AWS. |
| Record retried forever? | It can't — attempts are capped | After `WORKER_MAX_ATTEMPTS` (default 5) the record is marked `dead` in the S3 ledger and the message is deleted. Inspect with `python scripts/list_ingestion_failures.py --status dead`. |
| Attachment logged as `password-protected — SKIPPED` | File is encrypted (PDF with a user password, or a password-protected Office file) | Deliberate: locked files are skipped after download but before S3 upload/extraction (counted as `Attachments skipped (password-protected)` in the run summary). Remove the password and re-upload the file in Airtable. PDFs with only an owner password (open freely, edit-restricted) still process normally; legacy pre-2007 `.doc`/`.ppt` password schemes are not detected and fail downstream instead. |

### Citations & HTTP API

| Symptom | HTTP status | Cause & fix |
|---|---|---|
| `401`/`403` on API calls | Auth | Send `Authorization: Bearer <API_BEARER_TOKEN>`; set the token in `.env`. |
| `404 Invalid or expired citation link` | 404 | The `/cite/<token>` link expired (`CITATION_LINK_TTL_SECONDS`, default 24h) or the signing secret rotated (rotation revokes all links). |
| `403 Citation key out of scope` | 403 | The token's S3 key isn't in the allowed scope — regenerate the citation. |
| `404 Source document not found` | 404 | Underlying S3 object was moved/deleted. Presigned lifetime is `S3_CITATION_EXPIRY_SECONDS` (default 3600). |
| Citation URLs are huge | — | Short links need **both** `CITATION_SIGNING_SECRET` and `CITATION_PUBLIC_BASE_URL` set; otherwise raw presigned URLs are used. |

### Docker / EC2

| Symptom | Cause | Fix |
|---|---|---|
| `.env` values not picked up in container | Not passed into the service | Compose loads `.env`; confirm the service inherits it. Rebuild after dependency changes: `docker compose build`. |
| Script can't find `src` package | `PYTHONPATH` | Scripts insert `src/` on `sys.path` themselves; run from repo root. |
| Parsers missing (PDF/DOCX/PPTX) | Optional extras not installed | Install `dalberg-mcp[parsers]` and extend `supported_extensions` per table. See [reader-chunker.md](reader-chunker.md). |

---

## When you're still stuck

1. Re-run the failing command with `LOG_LEVEL=DEBUG`.
2. Isolate the stage: ingestion (`run_airtable_ingestion.py`), embedding
   (`run_pipeline.py --dry-run` to chunk-only), index (`validate_opensearch.py`),
   retrieval (`validate_opensearch.py --test-query`).
3. Check the re-extraction manifest for documents that failed extraction.
4. Confirm which **process** owns the missing key — indexing and retrieval are
   separate and each need `VOYAGE_API_KEY`.
