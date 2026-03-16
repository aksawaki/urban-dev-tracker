"""
reporter.py - Markdown レポート生成

収集した記事をエリア別・優先度別に整理したレポートを生成する。
"""

import os
from datetime import datetime
from pathlib import Path


PRIORITY_LABEL = {"high": "🔴 高", "medium": "🟡 中", "normal": "⚪ 通常"}


def generate_report(articles: list[dict], output_dir: str = "reports") -> str:
    """Markdown レポートを生成してファイルパスを返す"""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = f"{output_dir}/report_{ts}.md"
    summary_path = f"{output_dir}/latest.md"

    content = _build_report(articles)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(content)

    return report_path


def _build_report(articles: list[dict]) -> str:
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    lines = [
        f"# 都市開発計画 収集レポート",
        f"",
        f"生成日時: {now}  ",
        f"収集件数: **{len(articles)} 件**",
        f"",
    ]

    # 優先度でフィルタ
    high = [a for a in articles if a.get("priority") == "high"]
    medium = [a for a in articles if a.get("priority") == "medium"]
    normal = [a for a in articles if a.get("priority") == "normal"]

    # === 重要情報 ===
    if high:
        lines += [
            "---",
            "",
            "## 🔴 重要度：高",
            "",
        ]
        for a in high:
            lines += _article_block(a)

    if medium:
        lines += [
            "---",
            "",
            "## 🟡 重要度：中",
            "",
        ]
        for a in medium:
            lines += _article_block(a)

    # === エリア別 ===
    lines += [
        "---",
        "",
        "## 📍 エリア別一覧",
        "",
    ]

    area_map: dict[str, list[dict]] = {}
    for a in articles:
        area = a.get("area", "その他")
        area_map.setdefault(area, []).append(a)

    for area, items in sorted(area_map.items()):
        lines.append(f"### {area} ({len(items)} 件)")
        lines.append("")
        for a in items:
            priority_label = PRIORITY_LABEL.get(a.get("priority", "normal"), "")
            title = a.get("title", "（タイトルなし）")
            url = a.get("url", "")
            fetched = a.get("fetched_at", "")[:10]
            tags = " ".join(f"`{t}`" for t in a.get("tags", []))
            lines.append(f"- {priority_label} [{title}]({url})  ")
            lines.append(f"  取得: {fetched}  {tags}")
            summary = a.get("summary", "")
            if summary:
                lines.append(f"  > {summary[:120]}...")
            lines.append("")

    # === ソース別サマリ ===
    lines += [
        "---",
        "",
        "## 📊 ソース別件数",
        "",
        "| ソース | 件数 |",
        "|--------|------|",
    ]
    source_map: dict[str, int] = {}
    for a in articles:
        src = a.get("source_name", "unknown")
        source_map[src] = source_map.get(src, 0) + 1
    for src, count in sorted(source_map.items(), key=lambda x: -x[1]):
        lines.append(f"| {src} | {count} |")

    lines += ["", "---", "", "*このレポートは urban-dev-tracker が自動生成しました*", ""]
    return "\n".join(lines)


def _article_block(a: dict) -> list[str]:
    title = a.get("title", "（タイトルなし）")
    url = a.get("url", "")
    area = a.get("area", "")
    tags = " ".join(f"`{t}`" for t in a.get("tags", []))
    published = a.get("published_at") or a.get("fetched_at", "")
    published = published[:10] if published else ""
    summary = a.get("summary", "")

    lines = [
        f"#### [{title}]({url})",
        f"",
        f"- エリア: {area}　{tags}",
        f"- 日付: {published}",
    ]
    if summary:
        lines += [f"- 概要: {summary[:200]}"]
    lines += [""]
    return lines
