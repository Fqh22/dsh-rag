"""交接卡与上下文包：把结构化状态渲染成文本。

三样东西，各司其职：

    结构化状态（checkpoint）  机器写、机器读，字段齐全，存在 memories.meta_json
        ↓ render_card()
    交接卡（handoff）          人读的一段文字，也落一份 md 到项目目录
        ↓ build_pack()
    上下文包（pack）           新会话开场要读的那一坨，**为缓存前缀优化过**

## 为什么 pack 要单独一层

DeepSeek 的上下文缓存是**逐 token 前缀精确匹配**：只有从第一个字开始完全一样，
后面的 token 才算命中。所以「把最新状态写在最前面」这件事，等于每次开场都
把整段前缀改写一遍，缓存全部失效。

这里的分层顺序就是为这个设计的：

    0 基线      几乎不变（技术栈、目录、红线）  ← 跨会话稳定，缓存能一直命中
    1 长期事实  决策/坑，按写入顺序升序排列      ← 只在末尾追加
    2 当前状态  交接卡（会变）
    3 下一步    会变

**变化的东西一律排在后面**，这样新会话的请求前缀能命中上一段的缓存，
只有末尾几十到几百 token 需要重算。

pack 里不写时间戳/id/score —— 这些字段每次都不同，写进前缀就是自毁缓存。
需要元信息时显式传 with_meta=True。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

# 粗略换算：中文约 1 token ≈ 1.6 字符；用于预算而不是计费
CHARS_PER_TOKEN = 1.6

SECTION_ORDER = ("baseline", "facts", "card", "next")


def est_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _bullets(items: Iterable[Any], prefix: str = "- ") -> str:
    lines: list[str] = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        # 一条里有多行时保持缩进，避免破坏 markdown 结构
        parts = s.splitlines()
        lines.append(prefix + parts[0])
        lines.extend("  " + p for p in parts[1:] if p.strip())
    return "\n".join(lines)


def render_card(project: str, cp: dict[str, Any]) -> str:
    """checkpoint → 交接卡正文（确定性输出：同样的输入永远同样的字节）。"""
    out: list[str] = []
    goal = str(cp.get("goal") or "").strip()
    if goal:
        out += ["### 当前目标", goal, ""]

    for key, title in (("done", "已完成"), ("doing", "进行中"), ("next", "下一步")):
        items = [str(x) for x in (cp.get(key) or []) if str(x).strip()]
        if items:
            out += [f"### {title}", _bullets(items), ""]

    files = [f for f in (cp.get("files") or []) if isinstance(f, dict) and f.get("path")]
    if files:
        out += ["### 现场文件"]
        out += [f"- `{f['path']}`" + (f" —— {f['why']}" if f.get("why") else "") for f in files]
        out += [""]

    open_q = [str(x) for x in (cp.get("open_questions") or []) if str(x).strip()]
    if open_q:
        out += ["### 未决问题", _bullets(open_q), ""]

    body = "\n".join(out).strip()
    return body or f"（{project} 还没有结构化状态）"


def build_pack(
    project: str,
    card: str | None,
    checkpoint: dict[str, Any] | None,
    facts: list[dict[str, Any]],
    budget_tokens: int = 3000,
    with_meta: bool = False,
) -> dict[str, Any]:
    """拼出上下文包。

    facts 由调用方按 **id 升序** 传入（旧 → 新），这样新增事实只会追加在末尾。
    返回 {text, version, baselineVersion, bytes, estTokens, truncated, sections}
    """
    cp = checkpoint or {}
    baseline = str(cp.get("baseline") or "").strip()
    next_items = [str(x) for x in (cp.get("next") or []) if str(x).strip()]

    baseline_section = ""
    if baseline:
        baseline_section = "## 0 · 基线（稳定）\n" + baseline.strip() + "\n\n"

    facts_section = ""
    fact_lines: list[str] = []
    for f in facts:
        title = str(f.get("title") or "").strip()
        content = str(f.get("content") or "").strip()
        if not title and not content:
            continue
        kind = str(f.get("kind") or "")
        if with_meta:
            fact_lines.append(f"[{kind} #{f.get('id')}] {title}\n{content}")
        else:
            fact_lines.append(f"[{kind}] {title}\n{content}")
    if fact_lines:
        facts_section = "## 1 · 长期事实（按时间顺序，不改写）\n" + "\n\n".join(fact_lines) + "\n\n"

    card_section = ""
    if card and card.strip():
        card_section = "## 2 · 当前状态\n" + card.strip() + "\n\n"

    # 「下一步」只在没有卡的时候单独列 —— 有卡时卡里已经有一段，重复一遍纯属浪费 token
    next_section = ""
    if next_items and not card_section:
        next_section = "## 3 · 下一步\n" + _bullets(next_items) + "\n\n"

    header = (
        f"# 项目上下文包 · {project}\n"
        f"<!-- 由 dsh-rag 生成；基线不变则这一段字节不变，可跨会话复用前缀缓存 -->\n"
    )

    sections = {
        "baseline": baseline_section,
        "facts": facts_section,
        "card": card_section,
        "next": next_section,
    }

    # 先估总量，超预算就按「从后往前丢」的顺序收缩：
    # next → card 尾部 → facts 尾部。基线永远不砍，它是缓存的地基。
    limit_chars = int(budget_tokens * CHARS_PER_TOKEN)
    truncated: list[str] = []

    def total() -> int:
        return len(header) + sum(len(sections[k]) for k in SECTION_ORDER)

    if total() > limit_chars and sections["next"]:
        sections["next"] = ""
        truncated.append("next")

    if total() > limit_chars and sections["card"]:
        keep = max(0, len(sections["card"]) - (total() - limit_chars))
        if keep > 200:
            sections["card"] = sections["card"][:keep].rstrip() + "\n…（已截断）\n"
        else:
            sections["card"] = ""
        truncated.append("card")

    if total() > limit_chars and sections["facts"]:
        keep = max(0, len(sections["facts"]) - (total() - limit_chars))
        sections["facts"] = (
            (sections["facts"][:keep].rstrip() + "\n…（更早的事实见 recall）\n") if keep > 200 else ""
        )
        truncated.append("facts")

    text = header + "".join(sections[k] for k in SECTION_ORDER)
    text = text.rstrip() + "\n"

    return {
        "text": text,
        "version": _hash(text),
        "baselineVersion": _hash(baseline_section) if baseline_section else None,
        "chars": len(text),
        "estTokens": est_tokens(text),
        "truncated": truncated,
        "sections": {k: len(v) for k, v in sections.items()},
    }


def describe_pack_header(pack: dict[str, Any]) -> str:
    """一行元信息（放在 pack 之外用，绝不进缓存前缀）。"""
    return (
        f"pack v{pack['version']} · 约 {pack['estTokens']} token · "
        f"基线 v{pack['baselineVersion'] or '无'}"
        + (f" · 已裁剪 {','.join(pack['truncated'])}" if pack["truncated"] else "")
    )
