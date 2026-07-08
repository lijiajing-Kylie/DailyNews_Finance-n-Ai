import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from rich.console import Console

from src.models import (
    AIConfig,
    CategoryGroupConfig,
    Config,
    ContentItem,
    FilteringConfig,
    SourceType,
    SourcesConfig,
)
from src.orchestrator import HorizonOrchestrator
from src.ai.summarizer import DailySummarizer


_UNSET = object()


def make_item(
    item_id: str,
    score: float,
    category: str | None,
    ai_category: str | None = _UNSET,
) -> ContentItem:
    metadata = {"category": category} if category is not None else {}
    # Derive ai_category from metadata category if not explicitly provided
    if ai_category is _UNSET:
        if isinstance(category, str):
            if category in ("finance", "equities", "stocks", "macro"):
                ai_category = "finance"
            elif category in ("ai", "ai-tools", "ml"):
                ai_category = "ai"
            # else: leave as None — unknown categories don't auto-derive
        else:
            ai_category = "ai"  # default for backward compat with tests that don't set category
    # Resolve sentinel to actual value
    if ai_category is _UNSET:
        ai_category = None
    return ContentItem(
        id=item_id,
        source_type=SourceType.RSS,
        title=item_id,
        url=f"https://example.com/{item_id}",
        published_at=datetime.now(timezone.utc),
        ai_category=ai_category,
        ai_relevant=ai_category is not None,
        ai_score=score,
        metadata=metadata,
    )


def make_orchestrator(filtering: FilteringConfig) -> HorizonOrchestrator:
    orchestrator = HorizonOrchestrator.__new__(HorizonOrchestrator)
    orchestrator.config = SimpleNamespace(filtering=filtering)
    orchestrator.console = Console(record=True)
    return orchestrator


def test_unconfigured_balanced_digest_preserves_old_behavior() -> None:
    items = [make_item("lower", 7.0, "ai"), make_item("higher", 9.0, "finance")]
    result = make_orchestrator(FilteringConfig()).apply_balanced_digest(items)

    assert result.enabled is False
    assert result.items is items


def test_category_groups_apply_limits_and_default_group_limit() -> None:
    filtering = FilteringConfig(
        category_groups={
            "ai": CategoryGroupConfig(limit=2, categories=["ai", "ml"]),
            "finance": CategoryGroupConfig(limit=1, categories=["finance"]),
        },
        default_group_limit=1,
    )
    items = [
        make_item("ai-low", 7.0, "ai"),
        make_item("finance-low", 6.0, "finance"),
        make_item("other-high", 9.5, "world"),
        make_item("ai-high", 9.0, "ml"),
        make_item("finance-high", 8.5, "finance"),
        make_item("ai-mid", 8.0, "ai"),
        make_item("other-low", 5.0, None),
    ]

    result = make_orchestrator(filtering).apply_balanced_digest(items)

    assert [item.id for item in result.items] == [
        "other-high",
        "ai-high",
        "finance-high",
        "ai-mid",
    ]
    assert result.group_counts == {"other": 1, "ai": 2, "finance": 1}


def test_max_items_applies_after_group_limits() -> None:
    filtering = FilteringConfig(
        max_items=2,
        category_groups={
            "ai": CategoryGroupConfig(limit=2, categories=["ai"]),
            "finance": CategoryGroupConfig(limit=2, categories=["finance"]),
        },
    )
    items = [
        make_item("finance", 8.0, "finance"),
        make_item("ai-top", 10.0, "ai"),
        make_item("ai-second", 9.0, "ai"),
    ]

    result = make_orchestrator(filtering).apply_balanced_digest(items)

    assert [item.id for item in result.items] == ["ai-top", "ai-second"]
    assert result.group_counts == {"ai": 2}


def test_max_items_works_without_category_groups() -> None:
    filtering = FilteringConfig(max_items=1)
    items = [make_item("lower", 7.0, None), make_item("higher", 9.0, None)]

    result = make_orchestrator(filtering).apply_balanced_digest(items)

    assert [item.id for item in result.items] == ["higher"]


def test_duplicate_category_warns_and_first_group_wins() -> None:
    filtering = FilteringConfig(
        category_groups={
            "first": CategoryGroupConfig(limit=1, categories=["shared"]),
            "second": CategoryGroupConfig(limit=2, categories=["shared"]),
        }
    )
    orchestrator = make_orchestrator(filtering)

    result = orchestrator.apply_balanced_digest(
        [make_item("top", 9.0, "shared"), make_item("second", 8.0, "shared")]
    )

    assert [item.id for item in result.items] == ["top"]
    assert result.duplicate_categories == ["shared"]
    assert "using 'first'" in orchestrator.console.export_text()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_items": 0},
        {"default_group_limit": 0},
        {"category_groups": {"ai": {"limit": 0, "categories": ["ai"]}}},
        {"category_groups": {"ai": {"limit": 1, "categories": []}}},
    ],
)
def test_balanced_digest_config_rejects_non_positive_or_empty_limits(kwargs) -> None:
    with pytest.raises(ValidationError):
        FilteringConfig(**kwargs)


def test_run_applies_balanced_digest_before_enrichment(tmp_path, monkeypatch) -> None:
    config = Config(
        ai=AIConfig(
            provider="openai",
            model="test",
            api_key_env="TEST_API_KEY",
            languages=[],
        ),
        sources=SourcesConfig(),
        filtering=FilteringConfig(
            ai_score_threshold=7.0,
            max_items=1,
            category_groups={
                "ai": CategoryGroupConfig(limit=1, categories=["ai"]),
                "finance": CategoryGroupConfig(limit=1, categories=["finance"]),
            },
        ),
    )
    storage = SimpleNamespace(
        save_daily_summary=lambda *a, **kw: None,
        load_subscribers=lambda: [],
    )
    orchestrator = HorizonOrchestrator(config, storage)
    items = [
        make_item("ai", 9.0, "ai"),
        make_item("finance", 8.0, "finance"),
        make_item("below-threshold", 6.0, "ai"),
    ]
    enriched_ids: list[str] = []

    async def fetch_all_sources(since):  # type: ignore[no-untyped-def]
        return items

    async def analyze_content(input_items):  # type: ignore[no-untyped-def]
        return input_items

    async def merge_topic_duplicates(input_items):  # type: ignore[no-untyped-def]
        return input_items

    async def expand_twitter_discussion(input_items):  # type: ignore[no-untyped-def]
        return None

    async def classify_topics(input_items):  # type: ignore[no-untyped-def]
        return None

    async def enrich_important_items(input_items):  # type: ignore[no-untyped-def]
        enriched_ids.extend(item.id for item in input_items)

    async def generate_summary(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return ""

    monkeypatch.setattr(orchestrator, "fetch_all_sources", fetch_all_sources)
    monkeypatch.setattr(orchestrator, "_analyze_content", analyze_content)
    monkeypatch.setattr(orchestrator, "merge_topic_duplicates", merge_topic_duplicates)
    monkeypatch.setattr(orchestrator, "_classify_topics", classify_topics)
    monkeypatch.setattr(orchestrator, "_expand_twitter_discussion", expand_twitter_discussion)
    monkeypatch.setattr(orchestrator, "_enrich_important_items", enrich_important_items)
    monkeypatch.setattr(DailySummarizer, "generate_summary", generate_summary)
    monkeypatch.chdir(tmp_path)

    asyncio.run(orchestrator.run())

    assert enriched_ids == ["ai"]


def test_ai_category_routes_items_to_groups() -> None:
    """Items should be routed to groups based on ai_category when metadata category is absent."""
    filtering = FilteringConfig(
        category_groups={
            "ai": CategoryGroupConfig(limit=2, categories=["ai"]),
            "finance": CategoryGroupConfig(limit=1, categories=["finance"]),
        },
    )
    items = [
        make_item("ai-1", 9.0, None, ai_category="ai"),
        make_item("fin-1", 8.5, None, ai_category="finance"),
        make_item("ai-2", 8.0, None, ai_category="ai"),
        make_item("fin-2", 7.5, None, ai_category="finance"),
        make_item("ai-3", 7.0, None, ai_category="ai"),
    ]

    result = make_orchestrator(filtering).apply_balanced_digest(items)

    assert [item.id for item in result.items] == ["ai-1", "fin-1", "ai-2"]
    assert result.group_counts == {"ai": 2, "finance": 1}


def test_metadata_category_takes_priority_over_ai_category() -> None:
    """Source-level metadata category should override ai_category in group matching."""
    filtering = FilteringConfig(
        category_groups={
            "ai": CategoryGroupConfig(limit=2, categories=["ai"]),
            "finance": CategoryGroupConfig(limit=2, categories=["finance"]),
            "custom": CategoryGroupConfig(limit=1, categories=["custom-ai"]),
        },
    )
    items = [
        # metadata category "custom-ai" routes to "custom" group, despite ai_category="ai"
        make_item("custom", 9.0, "custom-ai", ai_category="ai"),
        make_item("ai-1", 8.5, None, ai_category="ai"),
        make_item("fin-1", 8.0, None, ai_category="finance"),
    ]

    result = make_orchestrator(filtering).apply_balanced_digest(items)

    assert [item.id for item in result.items] == ["custom", "ai-1", "fin-1"]
    assert result.group_counts == {"custom": 1, "ai": 1, "finance": 1}


def test_ai_category_none_does_not_match_groups() -> None:
    """Items with ai_category=None should fall through to default_group."""
    filtering = FilteringConfig(
        category_groups={
            "ai": CategoryGroupConfig(limit=2, categories=["ai"]),
        },
        default_group_limit=1,
    )
    items = [
        make_item("ai-item", 9.0, None, ai_category="ai"),
        make_item("none-item", 8.0, None, ai_category=None),
    ]

    result = make_orchestrator(filtering).apply_balanced_digest(items)

    assert [item.id for item in result.items] == ["ai-item", "none-item"]
    assert result.group_counts == {"ai": 1, "other": 1}


def test_threshold_for_returns_correct_per_category_values() -> None:
    """FilteringConfig.threshold_for() should return per-category thresholds."""
    from src.models import FilteringConfig

    # finance_score_threshold explicitly set
    cfg = FilteringConfig(
        ai_score_threshold=7.0,
        finance_score_threshold=6.0,
    )
    assert cfg.threshold_for("ai") == 7.0
    assert cfg.threshold_for("finance") == 6.0
    assert cfg.threshold_for(None) == 7.0  # falls back to ai_score_threshold

    # finance_score_threshold not set — falls back to ai_score_threshold
    cfg2 = FilteringConfig(ai_score_threshold=8.0)
    assert cfg2.threshold_for("ai") == 8.0
    assert cfg2.threshold_for("finance") == 8.0
    assert cfg2.threshold_for("other") == 8.0


def test_orchestrator_applies_per_category_thresholds() -> None:
    """Items should only pass if their score meets their category's own threshold.

    Note: ai_category=None items are already filtered out by the relevance gate
    (step 4.5) before the threshold step. This test focuses on the threshold logic
    for items that survived relevance filtering.
    """
    filtering = FilteringConfig(
        ai_score_threshold=7.0,
        finance_score_threshold=6.0,
    )
    # Only items that passed the relevance gate (ai_category is not None) reach here
    relevant_items = [
        make_item("ai-good", 8.0, None, ai_category="ai"),
        make_item("ai-bad", 6.5, None, ai_category="ai"),
        make_item("fin-good", 6.5, None, ai_category="finance"),
        make_item("fin-bad", 5.0, None, ai_category="finance"),
    ]

    # Simulate the threshold filtering step (after relevance gate)
    above: list[str] = []
    below: list[str] = []
    for item in relevant_items:
        if item.ai_score is None:
            continue
        effective = filtering.threshold_for(item.ai_category)
        if item.ai_score >= effective:
            above.append(item.id)
        else:
            below.append(item.id)

    assert set(above) == {"ai-good", "fin-good"}
    assert set(below) == {"ai-bad", "fin-bad"}
