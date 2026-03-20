from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


THEMES = {
    "AI / 自动化": ["ai", "openai", "gpt", "claude", "agent", "automation", "自动化", "模型", "提示词", "mcp", "llm"],
    "产品 / 运营": ["产品", "运营", "投放", "增长", "创业", "营销", "用户", "转化", "变现"],
    "工程 / 开发": ["python", "javascript", "typescript", "react", "node", "docker", "api", "sqlite", "rss", "github", "脚本", "开源"],
    "工具 / 效率": ["工具", "workflow", "效率", "浏览器", "插件", "app", "saas", "工作流", "知识库"],
}

NOISY_DOMAINS = {
    "",
    "m.okjike.com",
    "web.okjike.com",
    "www.okjike.com",
}


def _score_item(row) -> Tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []

    if row["link_url"]:
        score += 3
        reasons.append("带外链")
    if row["link_title"]:
        score += 2
        reasons.append("外链有标题")
    if row["has_video"]:
        score += 2
        reasons.append("包含视频")
    if row["has_audio"]:
        score += 2
        reasons.append("包含音频")
    if row["has_images"]:
        score += 1
        reasons.append("包含图片")
    if row["topic_name"]:
        score += 1
        reasons.append("有明确主题")
    if row["domain"] and row["domain"] not in NOISY_DOMAINS:
        score += 2
        reasons.append(f"来源域名 {row['domain']}")

    content_length = len((row["content"] or "").strip())
    if content_length >= 180:
        score += 3
        reasons.append("正文较长")
    elif content_length >= 80:
        score += 2
        reasons.append("正文有一定信息量")
    elif content_length >= 30:
        score += 1
        reasons.append("正文简短但可检索")

    return score, reasons


def _theme_hits(text: str) -> List[str]:
    lowered = text.lower()
    hits: List[str] = []
    for theme, keywords in THEMES.items():
        if any(keyword in lowered for keyword in keywords):
            hits.append(theme)
    return hits


def _format_top(counter: Counter, limit: int = 10) -> List[str]:
    lines = []
    for value, count in counter.most_common(limit):
        if not value:
            continue
        lines.append(f"- {value}: {count}")
    return lines


def build_report(rows: Iterable[object], days: int) -> str:
    rows = list(rows)
    total = len(rows)
    domain_counter: Counter = Counter()
    topic_counter: Counter = Counter()
    author_counter: Counter = Counter()
    type_counter: Counter = Counter()
    theme_map: Dict[str, List[object]] = defaultdict(list)
    useful_items: List[Tuple[int, List[str], object]] = []

    for row in rows:
        domain_counter[row["domain"]] += 1
        topic_counter[row["topic_name"]] += 1
        author_counter[row["author_screen_name"]] += 1
        type_counter[row["item_type"]] += 1

        text = " ".join(
            part
            for part in [
                row["title"],
                row["content"],
                row["link_title"],
                row["topic_name"],
                row["domain"],
            ]
            if part
        )
        for theme in _theme_hits(text):
            theme_map[theme].append(row)

        score, reasons = _score_item(row)
        useful_items.append((score, reasons, row))

    useful_items.sort(key=lambda item: (item[0], item[2]["created_at"]), reverse=True)
    report_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: List[str] = [
        "# 即刻收藏分析报告",
        "",
        f"- 生成时间: {report_date}",
        f"- 统计周期: 最近 {days} 天",
        f"- 条目数量: {total}",
        "",
        "## 总览",
        "",
    ]

    if total == 0:
        lines.extend(["最近这个周期没有抓到收藏。", ""])
        return "\n".join(lines)

    lines.extend(_format_top(type_counter))
    lines.append("")
    lines.append("## 高频来源")
    lines.append("")
    lines.extend(_format_top(Counter({k: v for k, v in domain_counter.items() if k not in NOISY_DOMAINS})))
    lines.append("")
    lines.append("## 高频主题")
    lines.append("")
    lines.extend(_format_top(topic_counter))
    lines.append("")
    lines.append("## 高频作者")
    lines.append("")
    lines.extend(_format_top(author_counter))
    lines.append("")
    lines.append("## 值得优先整理")
    lines.append("")

    for score, reasons, row in useful_items[:15]:
        reason_text = "，".join(reasons[:4]) if reasons else "一般收藏"
        lines.extend(
            [
                f"### {row['title']}",
                "",
                f"- 分数: {score}",
                f"- 判断依据: {reason_text}",
                f"- 作者: {row['author_screen_name'] or '未知'}",
                f"- 主题: {row['topic_name'] or '无'}",
                f"- 时间: {row['created_at']}",
                f"- 链接: {row['link_url'] or row['source_url']}",
                "",
                (row["content"] or "").strip()[:280] + ("…" if len((row["content"] or "").strip()) > 280 else ""),
                "",
            ]
        )

    lines.append("## 主题线索")
    lines.append("")
    for theme, themed_rows in theme_map.items():
        if not themed_rows:
            continue
        lines.append(f"### {theme}")
        lines.append("")
        for row in themed_rows[:8]:
            lines.append(f"- {row['title']} | {row['link_url'] or row['source_url']}")
        lines.append("")

    return "\n".join(lines)


def write_report(report_text: str, reports_dir: Path, days: int) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = reports_dir / f"jike-report-{days}d-{timestamp}.md"
    path.write_text(report_text, encoding="utf-8")
    latest_path = reports_dir / "latest.md"
    latest_path.write_text(report_text, encoding="utf-8")
    return path
