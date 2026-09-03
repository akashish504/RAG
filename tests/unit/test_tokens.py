import pytest

from pipeline.embedding_pipeline.chunker.tokens import (
    count_tokens,
    split_to_token_window,
)


def test_count_tokens_handles_empty_string() -> None:
    assert count_tokens("") == 0


def test_count_tokens_for_simple_string_is_positive() -> None:
    assert count_tokens("hello world") > 0


def test_split_to_token_window_returns_single_window_when_short() -> None:
    text = "hello world"
    pieces = split_to_token_window(text, max_tokens=100, overlap=0)
    assert pieces == [text]


def test_split_to_token_window_creates_multiple_overlapping_windows() -> None:
    text = " ".join(["alpha"] * 200)
    pieces = split_to_token_window(text, max_tokens=50, overlap=10)
    assert len(pieces) >= 2
    for piece in pieces:
        assert count_tokens(piece) <= 50


def test_split_to_token_window_validates_overlap() -> None:
    with pytest.raises(ValueError):
        split_to_token_window("text", max_tokens=10, overlap=10)
