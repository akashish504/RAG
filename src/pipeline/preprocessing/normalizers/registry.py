"""NormalizerRegistry — maps string names to AttachmentNormalizer factories.

Adding a new normalizer:
    1. Create a new module in this package (e.g. ``ocr.py``).
    2. Add an entry to ``_FACTORIES`` below.
    3. Set ``normalizer: <name>`` on the target in ``airtable_ingestion.yaml``.

No other code needs to change.
"""

from __future__ import annotations

import os
from typing import Callable

from pipeline.preprocessing.normalizers.base import AttachmentNormalizer
from pipeline.preprocessing.normalizers.passthrough import PassthroughNormalizer


def _require_env(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        msg = f"Environment variable {var!r} is required by the selected normalizer but is not set."
        raise RuntimeError(msg)
    return val


def _make_llm_cv() -> AttachmentNormalizer:
    from pipeline.preprocessing.normalizers.llm_cv import LLMCVNormalizer  # noqa: PLC0415

    return LLMCVNormalizer(api_key=_require_env("ANTHROPIC_API_KEY"))


def _make_llm_bio() -> AttachmentNormalizer:
    from pipeline.preprocessing.normalizers.llm_bio import LLMBioNormalizer  # noqa: PLC0415

    return LLMBioNormalizer(api_key=_require_env("ANTHROPIC_API_KEY"))


def _make_llm_content() -> AttachmentNormalizer:
    from pipeline.preprocessing.normalizers.llm_content import LLMContentNormalizer  # noqa: PLC0415

    return LLMContentNormalizer(api_key=_require_env("ANTHROPIC_API_KEY"))


def _make_slides_deck() -> AttachmentNormalizer:
    from pipeline.preprocessing.normalizers.slides_deck import SlidesDeckNormalizer  # noqa: PLC0415

    return SlidesDeckNormalizer(api_key=_require_env("ANTHROPIC_API_KEY"))


# Registry: name → zero-argument factory that returns an AttachmentNormalizer.
# Factories are callables (not instances) so the normalizer is only instantiated
# (and env vars checked) when a target actually requests it.
#
# To add a new normalizer:
#   1. Create a new module in this package (e.g. ocr.py).
#   2. Add a factory function above and an entry in _FACTORIES.
#   3. Set the name in attachment_column_normalizers or normalizer in the YAML config.
_FACTORIES: dict[str, Callable[[], AttachmentNormalizer]] = {
    "passthrough": PassthroughNormalizer,
    "llm_cv": _make_llm_cv,
    "llm_bio": _make_llm_bio,
    "llm_content": _make_llm_content,
    "slides_deck": _make_slides_deck,
}


def get_normalizer(name: str | None) -> AttachmentNormalizer:
    """Return an instantiated normalizer for the given name, or a PassthroughNormalizer."""
    if not name or name == "null":
        return PassthroughNormalizer()
    if name not in _FACTORIES:
        known = sorted(_FACTORIES)
        msg = f"Unknown normalizer {name!r}. Registered normalizers: {known}"
        raise ValueError(msg)
    return _FACTORIES[name]()


def registered_names() -> list[str]:
    """Return sorted list of all registered normalizer names."""
    return sorted(_FACTORIES)
