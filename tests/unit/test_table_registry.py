import pytest

from pipeline.embedding_pipeline.reader.tables import TableRegistry


def make_registry() -> TableRegistry:
    return TableRegistry.from_mapping(
        {
            "default": {
                "chunking": {
                    "parent_max_tokens": 800,
                    "child_max_tokens": 200,
                    "child_overlap_tokens": 50,
                },
            },
            "tables": {
                "profile": {"s3_prefix": "raw/profile/"},
                "proposals": {
                    "s3_prefix": "raw/proposals/",
                    "chunking": {"parent_max_tokens": 1000},
                },
                "finance": {"s3_prefix": "raw/finance/"},
            },
        }
    )


def test_resolve_from_key_matches_table_prefix() -> None:
    registry = make_registry()
    table = registry.resolve_from_key("raw/profile/recABC/cv.txt")
    assert table is not None
    assert table.name == "profile"


def test_resolve_from_key_returns_none_for_unknown_prefix() -> None:
    registry = make_registry()
    assert registry.resolve_from_key("other/foo/bar.txt") is None


def test_per_table_chunking_overrides_default() -> None:
    registry = make_registry()
    proposals = registry.get("proposals")
    profile = registry.get("profile")
    assert proposals.chunking.parent_max_tokens == 1000
    assert profile.chunking.parent_max_tokens == 800


def test_missing_s3_prefix_raises() -> None:
    with pytest.raises(KeyError):
        TableRegistry.from_mapping({"tables": {"x": {}}})
