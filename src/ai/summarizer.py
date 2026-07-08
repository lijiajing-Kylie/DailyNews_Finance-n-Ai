"""Daily summary generation — pure programmatic rendering."""

import re
from typing import List, Dict

from ..models import ContentItem


_CJK = r"[\u4e00-\u9fff\u3400-\u4dbf]"
_ASCII = r"[A-Za-z0-9]"


def _pangu(text: str) -> str:
    """Insert a space between CJK and ASCII letters/digits (Pangu spacing)."""
    text = re.sub(rf"({_CJK})({_ASCII})", r"\1 \2", text)
    text = re.sub(rf"({_ASCII})({_CJK})", r"\1 \2", text)
    return text


CATEGORY_LABELS = {
    "ai": "AI & Tech",
    "finance": "Finance & Markets",
}

LABELS = {
    "zh": {
        "header": "Horizon 每日速递 · AI & 金融",
        "source": "来源",
        "background": "背景",
        "discussion": "社区讨论",
        "references": "参考链接",
        "tags": "标签",
        "selected_items": "从 {total} 条内容中筛选出 {selected} 条重要资讯。",
        "empty_analyzed": "已分析 {total} 条内容，但没有达到重要性阈值的条目。",
        "empty_body": (
            "今日暂无重要动态，可能原因：\n"
            "- 今天关注的信息源较平静\n"
            "- 评分阈值设置过高\n"
            "- 信息源种类有待扩充\n\n"
            "建议：\n"
            "1. 在 config.json 中降低 `ai_score_threshold`\n"
            "2. 添加更多多样化的信息源\n"
            "3. 检查 AI 模型是否正常工作\n"
        ),
    },
}


class DailySummarizer:
    """Generates daily Markdown summaries from pre-analyzed content items."""

    def __init__(self):
        pass

    async def generate_summary(
        self,
        items: List[ContentItem],
        date: str,
        total_fetched: int,
        language: str = "zh",
        include_header: bool = True,
        include_discussion: bool = True,
    ) -> str:
        """Generate daily summary in Markdown format.

        Items are grouped by ai_category (AI / Finance) then sorted by score
        descending within each group.

        Args:
            items: High-scoring content items (already enriched)
            date: Date string (YYYY-MM-DD)
            total_fetched: Total number of items fetched before filtering
            language: Output language, either "en" or "zh"
            include_header: Whether to include the leading "# {title} - {date}"
                line (some delivery channels render their own title)
            include_discussion: Whether to include the community discussion
                block for each item

        Returns:
            str: Markdown formatted summary
        """
        labels = LABELS["zh"]

        if not items:
            return self._generate_empty_summary(
                date, total_fetched, labels, include_header=include_header
            )

        # Partition items by ai_category
        ai_items = [item for item in items if item.ai_category == "ai"]
        finance_items = [item for item in items if item.ai_category == "finance"]
        other_items = [item for item in items if item.ai_category not in ("ai", "finance")]

        # Build category stats line
        stats_parts = [f"AI: {len(ai_items)}", f"Finance: {len(finance_items)}"]
        if other_items:
            stats_parts.append(f"Other: {len(other_items)}")
        stats_line = " | ".join(stats_parts)

        header = (
            (f"# {labels['header']} - {date}\n\n" if include_header else "")
            + f"> {labels['selected_items'].format(total=total_fetched, selected=len(items))}\n"
            f"> {stats_line}\n\n"
            "---\n\n"
        )

        # Build TOC grouped by category
        toc_parts = []
        global_idx = 0

        def _append_toc(cat_items, cat_label):
            nonlocal global_idx
            if not cat_items:
                return
            toc_parts.append(f"## {cat_label}\n")
            for item in cat_items:
                global_idx += 1
                _t = item.metadata.get("title_zh") or item.title
                t = str(_t).replace("[", "(").replace("]", ")")
                t = _pangu(t)
                score = item.ai_score or "?"
                toc_parts.append(f"{global_idx}. [{t}](#item-{global_idx}) \u2b50\ufe0f {score}/10")
            toc_parts.append("")

        _append_toc(ai_items, CATEGORY_LABELS["ai"])
        _append_toc(finance_items, CATEGORY_LABELS["finance"])
        _append_toc(other_items, "Other")
        toc = "\n".join(toc_parts) + "\n---\n\n"

        # Build item details grouped by category
        parts = []
        global_idx = 0

        def _append_items(cat_items, cat_label):
            nonlocal global_idx
            if not cat_items:
                return
            parts.append(f"## {cat_label}\n\n")
            for item in cat_items:
                global_idx += 1
                parts.append(
                    self._format_item(
                        item, labels, language, global_idx,
                        include_discussion=include_discussion,
                    )
                )

        _append_items(ai_items, CATEGORY_LABELS["ai"])
        _append_items(finance_items, CATEGORY_LABELS["finance"])
        _append_items(other_items, "Other")

        return header + toc + "".join(parts)

    def generate_webhook_overview(
        self,
        items: List[ContentItem],
        date: str,
        total_fetched: int,
        language: str = "zh",
    ) -> str:
        """Generate a compact overview for multi-message webhook delivery."""
        labels = LABELS["zh"]
        if not items:
            return self._generate_empty_summary(date, total_fetched, labels)

        header = (
            f"# {labels['header']} - {date}\n\n"
            f"> 从 {total_fetched} 条内容中筛选出 {len(items)} 条重要资讯。\n\n"
            "下面会按新闻逐条发送详情，你可以只看感兴趣的标题。\n\n"
        )

        entries = []
        for i, item in enumerate(items, start=1):
            title = str(item.metadata.get("title_zh") or item.title).replace("[", "(").replace("]", ")")
            title = _pangu(title)
            score = item.ai_score or "?"
            entries.append(f"{i}. [{title}]({item.url}) \u2b50\ufe0f {score}/10")

        return header + "\n".join(entries)

    def generate_webhook_item(
        self,
        item: ContentItem,
        language: str,
        index: int,
        total: int,
    ) -> str:
        """Generate one item message for multi-message webhook delivery."""
        labels = LABELS["zh"]
        prefix = f"第 {index}/{total} 条\n\n"
        return prefix + self._format_item(item, labels, language, index).rstrip("-\n ")

    def _format_item(
        self,
        item: ContentItem,
        labels: dict,
        language: str,
        index: int,
        include_discussion: bool = True,
    ) -> str:
        """Format a single ContentItem into Markdown."""
        _title = item.metadata.get("title_zh") or item.title
        title = str(_title).replace("[", "(").replace("]", ")")
        url = str(item.url)
        score = item.ai_score or "?"
        meta = item.metadata

        summary = (
            meta.get("detailed_summary_zh")
            or meta.get("detailed_summary")
            or item.ai_summary
            or ""
        )
        discussion = (
            meta.get("community_discussion_zh")
            or meta.get("community_discussion")
            or ""
        )

        title = _pangu(title)
        summary = _pangu(summary)
        discussion = _pangu(discussion)

        # Source line with parts joined by " · ", link appended at end
        source_type = item.source_type.value
        source_parts = [source_type]
        if meta.get("subreddit"):
            source_parts.append(f"r/{meta['subreddit']}")
        if meta.get("feed_name"):
            source_parts.append(meta["feed_name"])
        else:
            source_parts.append(item.author or "unknown")
        if item.published_at:
            source_parts.append(
                f"{item.published_at.month}月{item.published_at.day}日 "
                f"{item.published_at:%H:%M}"
            )
        source_line = " \u00b7 ".join(source_parts)  # ·

        discussion_url = meta.get("discussion_url")
        if discussion_url:
            discussion_url = str(discussion_url)
            if discussion_url != url:
                source_line += f' · [{labels["discussion"]}]({discussion_url})'

        lines = [
            f'<a id="item-{index}"></a>',
            f"## [{title}]({url}) ⭐️ {score}/10",  # ⭐️
            "",
            summary,
            "",
            source_line,
        ]

        reason = item.metadata.get("reason_zh") or item.ai_reason or ""
        if reason:
            lines.insert(4, f"> **评分理由**: {reason}")

        if discussion and include_discussion:
            lines.append("")
            lines.append(f"**{labels['discussion']}**: {discussion}")

        lines.append("")
        lines.append("---")

        return "\n".join(lines) + "\n\n"

    def _generate_empty_summary(
        self, date: str, total_fetched: int, labels: dict, include_header: bool = True
    ) -> str:
        """Generate summary when no high-scoring items were found."""
        return (
            (f"# {labels['header']} - {date}\n\n" if include_header else "")
            + f"> {labels['empty_analyzed'].format(total=total_fetched)}\n\n"
            + labels["empty_body"]
        )
