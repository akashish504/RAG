# Reader + Chunker Design

This document describes the layered reader and chunker that turn `.txt`
objects in S3 into chunks ready for embedding and OpenSearch indexing.
The design is deliberately config-driven: adding a new table only requires
editing `config/tables.yaml`, not Python code.

## Format Contract

The **production** path is plain text: ingestion writes `.txt` under:

```text
s3://{bucket}/raw/{table_name}/{primary_key}/{column_name}.txt
```

Default `supported_extensions` in `config/tables.yaml` is `[.txt]`. Anything not
listed is skipped by `DocumentLoader` before parsing.

**Optional:** parsers for `.pdf`, `.docx`, and `.pptx` are shipped in code for
staging, experiments, or tables that deliberately allow binaries. Install
optional deps with `pip install 'dalberg-mcp[parsers]'`. Calls to those parsers
without the deps raise a clear `NotImplementedError`.

## Pipeline Shape

```text
S3 object (typically .txt)
   |
   v
S3Reader            -> Document (bytes + provenance)
   |
   v
TableRegistry       -> TableConfig (chunking config + S3 prefix)
   |
   v
DocumentLoader      -> filters by table.supported_extensions
   |
   v
ParserRegistry      -> ParsedDocument (sections + headings)
   |
   v
Chunker             -> [Chunk]
```

`DocumentLoader` is the small orchestrator that binds the first three layers
together and yields `LoadedDocument(document, parsed, table)` for the chunker.

## Table Configuration

`config/tables.yaml`:

```yaml
default:
  chunking:
    parent_max_tokens: 800
    parent_overlap_tokens: 100
    child_max_tokens: 200
    child_min_tokens: 30
    child_overlap_tokens: 50
  supported_extensions: [.txt]

tables:
  profile:
    s3_prefix: raw/profile/
  proposals:
    s3_prefix: raw/proposals/
    chunking:
      parent_max_tokens: 1000
      child_max_tokens: 250
  finance:
    s3_prefix: raw/finance/
    chunking:
      parent_max_tokens: 600
      child_max_tokens: 150
```

Adding a new table is config-only:

```yaml
tables:
  contracts:
    s3_prefix: raw/contracts/
    description: "Signed contracts and amendments."
    chunking:
      parent_max_tokens: 700
```

`TableRegistry.resolve_from_key(s3_key)` matches the key against the longest
prefix, so nested prefixes like `raw/proposals/2026/...` resolve to `proposals`.

## Reader: `S3Reader`

- Lists objects under the configured prefix, paginated.
- Skips zero-byte directory placeholders (keys ending with `/`).
- Optionally filters by extension.
- For text-like extensions (`.txt`, `.md`, ...) decodes UTF-8 into
`Document.text`.
- Always keeps raw bytes in `Document.body`.
- Computes `document_hash = sha256(body)` as the single source of truth for
change detection.
- Parses the S3 key into provenance: `table_name`, `primary_key`,
`column_name`, `filename`, plus `s3_bucket`, `s3_key`, and `source_url`.

Convention:

```text
raw/{table_name}/{primary_key}/{column_name}.txt
```

A flat key like `sample.txt` still works and falls back to a `standalone`
table for early development.

## Parser layer

`ParserRegistry` **default** parsers:


| Parser       | Extensions                 | Notes                                                |
| ------------ | -------------------------- | ---------------------------------------------------- |
| `TextParser` | `.txt`, `.md`, `.markdown` | Always usable; headings from `#` and ALL-CAPS lines. |
| `PDFParser`  | `.pdf`                     | Requires `pdfplumber` (`dalberg-mcp[parsers]`).      |
| `DOCXParser` | `.docx`                    | Requires `python-docx`.                              |
| `PPTXParser` | `.pptx`                    | Requires `python-pptx`.                              |


All parsers produce `ParsedSection` lists with:

- `text`
- `heading` (optional)
- `level` (e.g. 1 for `#`, 2 for `##`)
- `section_path` (breadcrumb of ancestor headings)
- `metadata`

Flat (not nested) is intentional: chunkers stay format-agnostic.

**Production** still restricts objects via `supported_extensions: [.txt]`.
Adding `.pdf` to a table and installing `[parsers]` enables those keys without code changes in the reader.

If the optional dependency for a chosen parser is missing, `parse()` raises `NotImplementedError` with install instructions.

## Chunker: parent-child with structural fallback

`ParentChildChunker.chunk(document, parsed, config)`:

1. Iterate sections in document order.
2. Compose section text as `heading + "\n\n" + body` so each parent chunk is
  self-contained even if retrieved without context.
3. If the section fits inside `parent_max_tokens`, emit it as one parent.
4. If it exceeds the limit, fall back to overlapping token windows
  (`split_to_token_window`). Each window becomes its own parent.
5. Each parent is also split into children using `child_max_tokens` and
  `child_overlap_tokens`. Children carry `parent_chunk_id`, so KNN
   retrieval over child vectors can pull the full parent text via `mget`.
6. Each chunk carries full provenance (`table_name`, `document_id`,
  `s3_path`, `chunk_id`, `chunk_index`, optional `section_title` and
   `section_path`).

Token counting uses tiktoken `cl100k_base`. The encoder is cached with
`functools.lru_cache` so 2k-document runs do not re-instantiate it.

### Why structural-then-token, not naive token windows

Naive token chunking has three weaknesses for consulting documents:

1. **Mid-thought splits**. A 400-token window can land in the middle of a
  bullet, separating an argument from its supporting detail.
2. **Lost context**. The retrieved chunk loses the section heading that
  gives it meaning.
3. **Section boundaries**. Most consulting outputs have natural sections
  that token windows ignore, returning entire blocks on every match.

Structural chunking fixes 1 and 2 by aligning chunk boundaries with section
boundaries and preserving headings on every chunk. The token-window fallback
handles oversized sections without re-introducing boundary problems, because
the heading is still prefixed on every window.

The parent-child design layers in retrieval precision: children are small
enough for high-quality KNN scores, while parents preserve full context when
returning results to Claude.

### Tradeoffs vs naive token chunking


| Concern               | Naive token              | Structural + token                         |
| --------------------- | ------------------------ | ------------------------------------------ |
| Boundary alignment    | Mid-bullet, mid-sentence | Heading aware                              |
| Context preservation  | Heading is often missing | Heading prefixed on every chunk            |
| Implementation effort | Trivial                  | A few hundred lines                        |
| Retrieval precision   | Mediocre                 | High on children, full context via parents |
| Behavior on flat text | Identical                | Identical (single section fallback)        |


## Adding A New Text-Shaped Format

1. Implement `Parser` for the new extension.
2. Register it in `ParserRegistry`.
3. Add the new extension to `supported_extensions` in `config/tables.yaml`.

## Adding A New Chunking Strategy

1. Implement `Chunker` from `dalberg_mcp.pipeline.chunker.base`.
2. Switch the script or pipeline orchestrator to use it.
3. Tables can carry their own `ChunkingConfig`, so different tables can use
  the same chunker with different parameters.

## End-To-End Smoke Test

```bash
python scripts/chunk_s3_object.py \
  --bucket claude-mcp-object-store \
  --key raw/profile/recABC/cv.txt
```

This reads the `.txt` object, runs `TextParser`, applies the chunker for the
table, and prints a summary of the chunks plus their provenance.