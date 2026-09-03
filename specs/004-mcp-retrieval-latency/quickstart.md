# Quickstart: validating the concurrent multi-source retrieval fix

## Prerequisites

- Repo dependencies installed (`uv sync` or existing project setup).
- Valid credentials for the sources you'll test against (Anthropic API key for the planner, OpenSearch/Airtable access for at least 2-4 enabled sources) — same environment already required to run the MCP server today.
- `pytest`, `pytest-asyncio` available (already dev dependencies).

## 1. Unit/behavioral tests

Run the existing and new tests covering `plan_retrieval_impl` and `_search_async`:

```bash
uv run pytest tests/unit/pipeline/ -k "planner or search or tools" -v
```

Expected: all pass, including (new) tests asserting:
- A single-source call produces output identical to a hand-computed expected result (regression guard for FR-005).
- A multi-source call with one source's `plan_query` forced to raise still returns correct results for the other sources, with the failing source showing the existing default fallback plan (FR-003).
- Output shape/fields for a fixed multi-source question match a snapshot taken before the change (FR-004).

## 2. Manual latency comparison (SC-001)

Pick a question that spans 4 enabled sources (check `list_sources` output or `config/retrieval_sources.yaml` for enabled names). Time the `search` tool call before and after the change:

```bash
uv run python - <<'EOF'
import time
from retrieval.mcp.tools import search_impl

t0 = time.perf_counter()
result = search_impl(question="<a real multi-domain question covering 4 sources>", sources=["*"], top_k=10)
print(f"elapsed: {time.perf_counter() - t0:.2f}s")
EOF
```

Expected: elapsed time after the fix is roughly in line with a single source's own latency (SC-001: no more than ~1.5x a single-source call), not ~4x as before. Compare against the same command run on the pre-change code (e.g. `git stash` the change temporarily, or check out the prior commit) for a true before/after.

## 3. Single-source regression check (SC-002)

Repeat the same timing command with `sources=["<one specific source>"]` before and after the change. Expected: no measurable difference beyond normal run-to-run variance.

## 4. Failure isolation check (SC-003)

Temporarily break one source's schema lookup (e.g. point `get_schema()` at an invalid config, or monkeypatch `plan_query` to raise for one source name in a test) and confirm:
- The other sources' hits are present and correct in the response.
- The broken source's diagnostics show `mode: "semantic_only"`, `uncertain: True` — the same fallback shape produced today.

## References

- Problem statement and requirements: [spec.md](./spec.md)
- Technical approach and rationale: [plan.md](./plan.md), [research.md](./research.md)
- Data flow and independence guarantees: [data-model.md](./data-model.md)
- Per-source unit-of-work behavior: [contracts/internal-functions.md](./contracts/internal-functions.md)
