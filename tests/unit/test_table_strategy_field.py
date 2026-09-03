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
                "chunker_strategy": "parent_child",
            },
            "tables": {
                "profile": {
                    "s3_prefix": "raw/profile/",
                    "chunker_strategy": "resume",
                },
                "proposals": {
                    "s3_prefix": "raw/proposals/",
                },
            },
        }
    )


def test_profile_table_uses_resume_strategy() -> None:
    registry = make_registry()
    assert registry.get("profile").chunker_strategy == "resume"


def test_table_without_override_inherits_default_strategy() -> None:
    registry = make_registry()
    assert registry.get("proposals").chunker_strategy == "parent_child"
