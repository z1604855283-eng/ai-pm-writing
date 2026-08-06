#!/usr/bin/env python3
"""检查 AI 产品经理中文文章的高风险写作形状，只报警，不自动改稿。"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path


PLACEHOLDER_PATTERNS = (
    re.compile(r"\b(?:TODO|TBD|XXX)\b", re.IGNORECASE),
    re.compile(r"[【\[]?(?:待补|待核|待确认|来源待补|数据待补)[】\]]?"),
)

PIVOT_PATTERNS = (
    re.compile(r"(?:并)?不是[^。！？\n]{0,90}而是"),
    re.compile(r"并非[^。！？\n]{0,90}而是"),
    re.compile(r"不在于[^。！？\n]{0,90}而在于"),
    re.compile(r"与其说[^。！？\n]{0,90}(?:不如|倒不如|毋宁)"),
    re.compile(r"你以为[^。！？\n]{0,90}(?:其实|实际|后来)"),
    re.compile(r"看似[^。！？\n]{0,90}(?:其实|实际|实则)"),
)

STRONG_WORDS = (
    "真正", "本质", "其实", "关键", "分水岭", "护城河",
    "根本", "恰恰", "必然", "唯一", "从来",
)

JARGON = (
    "赋能", "抓手", "降本增效", "底层逻辑", "顶层设计",
    "认知跃迁", "价值释放", "能力沉淀", "拉通", "组合拳",
    "打开想象空间", "结构性机会", "关键命题", "深层逻辑",
    "方法论", "闭环", "链路", "颗粒度", "对齐",
)

PERSONAL_CLAIM_PATTERNS = (
    re.compile(r"我(?:曾经|之前|过去)?(?:负责|主导|参与|做过|上线|带领|服务过)"),
    re.compile(r"我们(?:团队|公司|项目)(?:曾经|之前|过去)?(?:负责|发现|验证|上线|做过|遇到)"),
)

METAPHOR_FIELDS = {
    "建筑": ("地基", "底座", "楼层", "支柱", "坍塌", "砖头"),
    "战争": ("战场", "弹药", "开火", "攻防", "阵地", "杀死"),
    "道路": ("赛道", "岔路", "路口", "终点", "门票", "高速路"),
    "海洋": ("蓝海", "浪潮", "潮水", "航船", "灯塔", "彼岸"),
    "身体机器": ("骨架", "血肉", "心脏", "血管", "引擎", "齿轮"),
}

NON_BODY_H2 = {
    "目录", "关键来源", "参考资料", "资料来源", "排版建议", "配图建议",
}

CHINESE_HEADING_NUMBERS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

NUMBERED_H2_PATTERN = re.compile(
    r"^(?P<number>[一二三四五六七八九十]+)、\s*\S"
)

NUMBERED_H3_PATTERN = re.compile(r"^\d+\.\d+\s+\S")


def han_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def excerpt(value: str, width: int = 54) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= width else value[: width - 1] + "…"


def clean_heading(value: str) -> str:
    """移除常见的 Markdown 强调符号，便于比较目录与正文标题。"""
    return re.sub(r"[*_`]", "", value).strip()


def mask_non_prose(text: str) -> str:
    def mask(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group())

    tick = chr(96)
    patterns = (
        re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.DOTALL),
        re.compile(re.escape(tick * 3) + r".*?" + re.escape(tick * 3), re.DOTALL),
        re.compile(re.escape(tick) + r"[^" + re.escape(tick) + r"\n]*" + re.escape(tick)),
        re.compile(r"https?://[^\s)>]+"),
    )
    masked = text
    for pattern in patterns:
        masked = pattern.sub(mask, masked)
    return masked


def all_matches(text: str, patterns):
    matches = []
    for pattern in patterns:
        matches.extend(pattern.finditer(text))
    return sorted(matches, key=lambda match: match.start())


def term_matches(text: str, terms):
    matches = []
    for term in terms:
        matches.extend((match.start(), term) for match in re.finditer(re.escape(term), text))
    return sorted(matches)


def metaphor_cluster(text: str, distance: int = 800):
    hits = []
    for field, words in METAPHOR_FIELDS.items():
        for word in words:
            hits.extend((match.start(), field, word) for match in re.finditer(word, text))
    hits.sort()
    for index, (start, _, _) in enumerate(hits):
        window = [hit for hit in hits[index:] if hit[0] - start <= distance]
        fields = {hit[1] for hit in window}
        if len(fields) >= 3:
            return window, fields
    return None


def paragraph_stats(text: str):
    paragraphs = []
    cursor = 0
    for block in re.split(r"\n\s*\n", text):
        position = text.find(block, cursor)
        cursor = max(cursor, position + len(block))
        clean = re.sub(r"[>*_]", "", block).strip()
        if not clean or clean.startswith(("#", "http", "![", "~~~")):
            continue
        if re.match(r"^(?:[-+*]|\d+[.、])\s", clean):
            continue
        count = han_count(clean)
        if count < 4:
            continue
        sentences = max(1, len(re.findall(r"[。！？!?]", clean)))
        paragraphs.append((position, count, sentences))
    return paragraphs


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 AI 产品经理中文文章")
    parser.add_argument("path", help="Markdown 或文本文件路径，使用 - 从标准输入读取")
    args = parser.parse_args()

    try:
        raw = sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        print(f"无法读取稿件。{error}", file=sys.stderr)
        return 2

    prose = mask_non_prose(raw)
    total_han = han_count(prose)
    if total_han == 0:
        print("没有检测到汉字。", file=sys.stderr)
        return 2

    failures = []
    warnings = []

    h2_matches = list(re.finditer(r"^##(?!#)\s+(.+?)\s*$", prose, re.MULTILINE))
    h2_headings = [
        (match.start(), clean_heading(match.group(1))) for match in h2_matches
    ]
    body_h2 = [
        (position, heading)
        for position, heading in h2_headings
        if heading not in NON_BODY_H2
    ]
    toc_matches = [
        match
        for match in h2_matches
        if clean_heading(match.group(1)) == "目录"
    ]
    has_toc = bool(toc_matches)
    structured_long = total_han >= 1200 or len(body_h2) >= 3
    needs_toc = total_han >= 1800 or len(body_h2) >= 4

    if structured_long:
        if len(body_h2) < 3:
            warnings.append(
                f"长文只检测到 {len(body_h2)} 个正文一级标题。"
                "确认总—分—总的中间展开是否完整；不要为了数量硬拆内容。"
            )

        unnumbered_h2 = [
            (position, heading)
            for position, heading in body_h2
            if not NUMBERED_H2_PATTERN.match(heading)
        ]
        if unnumbered_h2:
            samples = "、".join(
                f"第 {line_number(raw, position)} 行“{excerpt(heading)}”"
                for position, heading in unnumbered_h2[:6]
            )
            warnings.append(
                "长文正文一级标题应统一使用“一、二、三”的中文序号。"
                f"未编号标题有 {samples}。"
            )
        elif body_h2:
            heading_numbers = [
                CHINESE_HEADING_NUMBERS.get(
                    NUMBERED_H2_PATTERN.match(heading).group("number")
                )
                for _, heading in body_h2
            ]
            expected_numbers = list(range(1, len(body_h2) + 1))
            if heading_numbers != expected_numbers:
                warnings.append(
                    "正文一级标题序号不连续。"
                    f"检测到 {heading_numbers}，预期为 {expected_numbers}。"
                )

        h3_matches = []
        for match in re.finditer(
            r"^###(?!#)\s+(.+?)\s*$", prose, re.MULTILINE
        ):
            preceding_h2 = [
                (position, heading)
                for position, heading in h2_headings
                if position < match.start()
            ]
            if preceding_h2 and preceding_h2[-1][1] not in NON_BODY_H2:
                h3_matches.append(match)
        unnumbered_h3 = [
            (match.start(), clean_heading(match.group(1)))
            for match in h3_matches
            if not NUMBERED_H3_PATTERN.match(clean_heading(match.group(1)))
        ]
        if unnumbered_h3:
            samples = "、".join(
                f"第 {line_number(raw, position)} 行“{excerpt(heading)}”"
                for position, heading in unnumbered_h3[:6]
            )
            warnings.append(
                "长文二级标题应统一使用“1.1、1.2”的阿拉伯数字。"
                f"未按格式编号的标题有 {samples}。"
            )

    if needs_toc and not has_toc:
        warnings.append(
            "文章达到一千八百字或四个正文一级部分，但没有检测到“## 目录”。"
            "在开头核心判断之后、正文第一个一级标题之前增加目录。"
        )

    if has_toc:
        toc_match = toc_matches[0]
        if len(toc_matches) > 1:
            warnings.append("检测到多个“## 目录”，只保留一个目录。")

        if body_h2 and toc_match.start() > body_h2[0][0]:
            warnings.append(
                "目录出现在正文一级标题之后。"
                "把它移到开头核心判断之后、正文第一个一级标题之前。"
            )

        next_h2 = re.search(
            r"^##(?!#)\s+", prose[toc_match.end():], re.MULTILINE
        )
        toc_end = (
            toc_match.end() + next_h2.start()
            if next_h2
            else len(prose)
        )
        toc_block = prose[toc_match.end():toc_end]
        missing_from_toc = [
            heading for _, heading in body_h2 if heading not in toc_block
        ]
        if missing_from_toc:
            samples = "、".join(
                f"“{excerpt(heading)}”" for heading in missing_from_toc[:6]
            )
            warnings.append(
                "目录与正文一级标题不完全一致。"
                f"目录中没有找到 {samples}。"
            )

    placeholders = all_matches(prose, PLACEHOLDER_PATTERNS)
    for match in placeholders:
        failures.append(
            f"未完成占位符，第 {line_number(raw, match.start())} 行，"
            f"“{excerpt(match.group())}”"
        )

    pivots = all_matches(prose, PIVOT_PATTERNS)
    pivot_limit = max(2, total_han // 700)
    if len(pivots) > pivot_limit:
        lines = "、".join(str(line_number(raw, match.start())) for match in pivots[:8])
        warnings.append(
            f"翻案结构共 {len(pivots)} 处，建议线为 {pivot_limit} 处，第 {lines} 行。"
            "保留有事实差异的主要反转，删掉只负责抬高语气的部分。"
        )

    strong = term_matches(prose, STRONG_WORDS)
    strong_limit = max(4, total_han // 350)
    if len(strong) > strong_limit:
        counts = collections.Counter(term for _, term in strong)
        samples = "、".join(f"{term} {count} 次" for term, count in counts.most_common(6))
        warnings.append(
            f"强判断词共 {len(strong)} 处，建议线为 {strong_limit} 处。{samples}。"
            "确认每一处都有材料，并避免同一词反复承担气势。"
        )

    questions = list(re.finditer(r"[？?]", prose))
    question_limit = max(4, total_han // 450)
    if len(questions) > question_limit:
        warnings.append(
            f"问句共 {len(questions)} 处，建议线为 {question_limit} 处。"
            "只保留能暴露理解断口或推动结构的问题。"
        )

    jargon = term_matches(prose, JARGON)
    if jargon:
        counts = collections.Counter(term for _, term in jargon)
        samples = "、".join(f"{term} {count} 次" for term, count in counts.most_common(8))
        warnings.append(
            f"发现需要结合语境判断的产品黑话或术语 {len(jargon)} 处。{samples}。"
            "含义准确且读者需要时保留，能换成动作、指标和结果时改写。"
        )

    personal_claims = all_matches(prose, PERSONAL_CLAIM_PATTERNS)
    if personal_claims:
        lines = "、".join(
            str(line_number(raw, match.start())) for match in personal_claims[:8]
        )
        warnings.append(
            f"发现 {len(personal_claims)} 处第一人称项目经历，第 {lines} 行。"
            "逐项确认来自用户真实材料。"
        )

    numbers = list(
        re.finditer(r"\d+(?:\.\d+)?\s*(?:%|％|亿|万|倍|百分点)", prose)
    )
    has_source_signal = any(
        signal in raw
        for signal in ("http://", "https://", "关键来源", "参考资料", "资料来源")
    )
    if numbers and not has_source_signal:
        samples = "、".join(match.group() for match in numbers[:6])
        warnings.append(
            f"发现 {len(numbers)} 个精确比例或规模数字，但没有检测到来源区域。"
            f"例子有 {samples}。确认时间、口径和原始出处。"
        )

    acronyms = sorted(
        {
            match.group()
            for match in re.finditer(
                r"(?<![A-Za-z])[A-Z][A-Z0-9-]{1,10}(?![A-Za-z])", prose
            )
        }
    )
    if acronyms:
        warnings.append(
            "发现英文缩写 "
            + "、".join(acronyms[:12])
            + "。确认第一次出现时已经解释中文含义、英文全称或产品位置。"
        )

    paragraphs = paragraph_stats(prose)
    if len(paragraphs) >= 10:
        one_sentence = sum(sentences <= 1 for _, _, sentences in paragraphs)
        if one_sentence / len(paragraphs) >= 0.75:
            warnings.append(
                f"可识别段落中有 {one_sentence / len(paragraphs):.0%} 只有一句话。"
                "检查是否形成统一的短段鼓点。"
            )

    streak = []
    for position, count, sentences in paragraphs:
        if count <= 28 and sentences <= 1:
            streak.append((position, count, sentences))
            if len(streak) >= 4:
                warnings.append(
                    f"从第 {line_number(raw, streak[0][0])} 行起连续出现"
                    f" {len(streak)} 个短促单句段。合并不需要强调的段落。"
                )
                break
        else:
            streak = []

    colon_count = prose.count("：") + prose.count(":")
    colon_limit = max(8, total_han // 150)
    if colon_count > colon_limit:
        warnings.append(
            f"冒号共 {colon_count} 处，建议线为 {colon_limit} 处。"
            "框架和定义中可以使用，避免每段都用提示语领起。"
        )

    dash_count = prose.count("—") + prose.count("–")
    dash_limit = max(5, total_han // 300)
    if dash_count > dash_limit:
        warnings.append(
            f"破折号或连接号式破折号共 {dash_count} 处，建议线为 {dash_limit} 处。"
            "只保留确实需要插入说明或拉开停顿的位置。"
        )

    metaphors = metaphor_cluster(prose)
    if metaphors:
        window, fields = metaphors
        samples = "、".join(dict.fromkeys(hit[2] for hit in window))
        warnings.append(
            f"八百字内出现 {len(fields)} 套比喻领域。{'、'.join(sorted(fields))}。"
            f"例词有 {samples}。保留一套主要类比并回到真实概念。"
        )

    print(
        f"汉字数 {total_han}，正文一级标题 {len(body_h2)}，"
        f"目录 {'有' if has_toc else '无'}"
    )
    print(
        f"占位符 {len(placeholders)}，翻案结构 {len(pivots)}，"
        f"强判断词 {len(strong)}，问句 {len(questions)}，"
        f"黑话或术语 {len(jargon)}，第一人称经历 {len(personal_claims)}，"
        f"精确数字 {len(numbers)}"
    )

    if failures:
        print("\n需要修改")
        for item in failures:
            print(f"- {item}")

    if warnings:
        print("\n需要人工判断")
        for item in warnings:
            print(f"- {item}")

    if not failures and not warnings:
        print("\n未发现这份检查器覆盖的问题。")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
