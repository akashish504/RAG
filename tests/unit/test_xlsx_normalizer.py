"""XLSX → markdown tables via _process_xlsx (deterministic, no LLM)."""

from __future__ import annotations

import io

import pytest

openpyxl = pytest.importorskip("openpyxl")

from pipeline.preprocessing.normalizers.llm_content import _process_xlsx  # noqa: E402


def _wb_bytes(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_multi_sheet_to_markdown() -> None:
    data = _wb_bytes({
        "Costs": [["Item", "USD"], ["Phase 1", 1000], ["Phase 2", 2500]],
        "Summary": [["Metric", "Value"], ["Total", 3500]],
    })
    out = _process_xlsx(data, "model.xlsx")
    assert out is not None
    assert "## Costs" in out and "## Summary" in out
    assert "| Item | USD |" in out
    assert "| Phase 1 | 1000 |" in out
    assert "| Total | 3500 |" in out


def test_empty_rows_skipped_and_empty_workbook_returns_none() -> None:
    # All-empty rows are dropped; a workbook with no content returns None.
    data = _wb_bytes({"Blank": [[None, None], [None, None]]})
    assert _process_xlsx(data, "blank.xlsx") is None


def test_large_sheet_capped(monkeypatch) -> None:
    import pipeline.preprocessing.normalizers.llm_content as mod
    monkeypatch.setattr(mod, "_XLSX_MAX_ROWS", 5)
    rows = [["h"]] + [[i] for i in range(50)]
    out = mod._process_xlsx(_wb_bytes({"Big": rows}), "big.xlsx")
    assert "[Sheet truncated at 5 rows]" in out
