"""Token utilities for chunking.

A single tiktoken encoder instance is reused per encoding name. Re-creating
the encoder on every call adds noticeable overhead at 2k-document scale.
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken


@lru_cache(maxsize=4)
def get_encoding(name: str = "cl100k_base") -> tiktoken.Encoding:
    """Return the cached tiktoken encoder for ``name``."""

    return tiktoken.get_encoding(name)


def count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """Count the number of tokens in ``text``."""

    if not text:
        return 0
    return len(get_encoding(encoding_name).encode(text))


def split_to_token_window(
    text: str,
    *,
    max_tokens: int,
    overlap: int = 0,
    encoding_name: str = "cl100k_base",
) -> list[str]:
    """Split ``text`` into overlapping windows of at most ``max_tokens`` tokens.

    Returns a list with at least one element when ``text`` has tokens. Empty
    input yields an empty list. Overlap must be in ``[0, max_tokens)``.
    """

    if max_tokens <= 0:
        msg = "max_tokens must be > 0"
        raise ValueError(msg)
    if overlap < 0 or overlap >= max_tokens:
        msg = "overlap must satisfy 0 <= overlap < max_tokens"
        raise ValueError(msg)

    encoding = get_encoding(encoding_name)
    tokens = encoding.encode(text)
    if not tokens:
        return []
    if len(tokens) <= max_tokens:
        return [text]

    step = max_tokens - overlap
    chunks: list[str] = []
    n = len(tokens)
    for start in range(0, n, step):
        window = tokens[start : start + max_tokens]
        decoded = encoding.decode(window)
        is_last = start + max_tokens >= n

        # Snap to word boundaries so chunks never start or end mid-word.
        # Only trim the start for non-first windows; only trim the end for
        # non-last windows (last chunk keeps its natural ending).
        if start > 0 and decoded and not decoded[0].isspace():
            space = next((i for i, c in enumerate(decoded) if c in " \t\n\r"), -1)
            decoded = decoded[space:].lstrip() if space != -1 else decoded
        if not is_last and decoded and not decoded[-1].isspace():
            space = next((i for i in range(len(decoded) - 1, -1, -1) if decoded[i] in " \t\n\r"), -1)
            decoded = decoded[:space].rstrip() if space != -1 else decoded

        if decoded:
            chunks.append(decoded)
        if is_last:
            break
    return chunks
