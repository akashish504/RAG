# Chunking Strategies

Chunking is pluggable. Each strategy implements the `Chunker` protocol from
`pipeline/chunker/base.py` and is registered in
`pipeline/chunker/registry.py` under a short name. Tables choose a strategy
by name in `config/tables.yaml`:

```yaml
tables:
  profile:
    s3_prefix: raw/profile/
    chunker_strategy: resume
```

The CLI also accepts `--strategy` to override per run for experimentation:

```bash
python scripts/chunk_s3_object.py \
  --bucket claude-mcp-object-store \
  --key raw/profile/recABC/cv.txt \
  --strategy parent_child
```

## Built-in strategies

### `parent_child` (generic, default)

`ParentChildChunker` in `pipeline/chunker/parent_child.py`. Each parsed
section becomes a parent. If the section exceeds `parent_max_tokens`, it
falls back to overlapping token windows. Children are token-window splits
of the parent. Use this when the source doesn't have a strong template
(generic documents, proposals).

### `resume` (profile table)

`ResumeChunker` in `pipeline/chunker/resume.py`. Tuned for resumes/CVs.

1. **Section-wise chunking.** Each canonical resume section becomes one
   parent chunk. Heading aliases ("Profile", "Professional Summary",
   "Employment History", ...) are normalized to canonical names
   (`summary`, `skills`, `experience`, `education`, `projects`,
   `certifications`, `awards`, `languages`, `publications`, `volunteer`,
   else `other` / `unknown`). The canonical name is stored on every chunk
   as `metadata.section_canonical` for retrieval and analytics.

2. **Parent-child for list sections.** Sections that naturally hold a list
   of independent entries — `experience`, `education`, `projects`,
   `publications`, `volunteer` — get one *child* chunk per entry. Each
   child carries `parent_chunk_id` (the section parent) plus
   `metadata.entry_index` so retrieval can map a hit back to the exact
   logical unit (a job, a degree, a project).

3. **Logical units stay intact.** A single entry is never spread across
   multiple children unless it exceeds `child_max_tokens` on its own. In
   that rare case, the entry is split into overlapping token windows that
   all share the same `entry_index` plus an `entry_sub_index`, so the
   retriever can reconstruct the entry. This satisfies "do not split a
   single job entry across chunks" in the common case while still being
   safe for verbose entries.

4. **Non-list sections** (e.g. `summary`, `skills`) follow the standard
   parent-with-token-window-children fallback. Children are only emitted
   if the section is large enough to need them.

#### Entry detection

`split_section_into_entries` (also exported from the chunker package)
walks the section body paragraph by paragraph and starts a new entry
whenever the paragraph's first non-empty line:

- contains a date range like `Jan 2020 - Present`, `2018-2022`,
  `2020 – Present`, or
- looks like a pipe-separated header such as
  `Senior Engineer | Acme Corp | Jan 2020 - Present`.

If the section has none of those markers, paragraphs separated by blank
lines are used as entries. This is the universal resume convention.

#### Metadata on every chunk

```python
{
    "section_title": "Experience",          # raw heading from the .txt
    "section_canonical": "experience",      # canonical alias
    "section_level": 1,
    "section_path": ["Experience"],
    "is_list_section": True,
    "chunk_index": 2,                       # position within section/parent
    "table_name": "profile",
    "document_id": "recABC",                # primary key from S3 path
    "s3_path": "raw/profile/recABC/cv.txt",
    "strategy": "resume",
    # children only:
    "child_index_in_parent": 0,
    "entry_index": 0,                       # 0-based logical entry index
    "entry_sub_index": 0,                   # only when an entry was windowed
}
```

The children's `parent_chunk_id` always points to the section parent, so
downstream retrieval can:

- score on children for precision,
- fetch the parent for grounded context,
- cite the original `s3_path`/`document_id` for traceability.

## Adding a new strategy

1. Implement `Chunker` (`base.py`):

   ```python
   class MyStrategy:
       def chunk(self, *, document, parsed, config):
           ...
           return chunks
   ```

2. Register it in `registry.py` (or build a custom registry and pass it
   into the composition root):

   ```python
   registry.register("my_strategy", MyStrategy())
   ```

3. Point one or more tables at it in `config/tables.yaml`:

   ```yaml
   tables:
     my_table:
       s3_prefix: raw/my_table/
       chunker_strategy: my_strategy
   ```

No call sites need to change. `DocumentLoader` keeps reading and parsing
exactly the same way; only the chunker behind the registry changes.

## Design constraints honored

- **Modular, swappable** — Strategies live in their own files and are
  selected by name. Adding one is config + register + implement; nothing
  else.
- **Compatibility with retrieval** — Every chunk has `chunk_id`,
  `parent_chunk_id`, `chunk_type`, and full provenance. No retrieval-side
  code changes are required to consume the new metadata.
- **Semantic integrity** — Sections drive parent boundaries; entries
  drive child boundaries. Token windows are a fallback, not the default.
- **Logical units intact** — A single job entry is one child chunk
  unless it exceeds `child_max_tokens`, in which case all the windows
  share a stable `entry_index` so retrieval can reassemble the unit.
