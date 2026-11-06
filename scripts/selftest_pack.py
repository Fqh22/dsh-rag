"""pack 裁剪的回归测试。

2026-11-06 之前 build_pack 的收缩顺序是「先砍 card，再砍 facts」，而 facts
截断取开头（＝最旧的一批）。结果 dsh-rag / companion-bot 两个最活跃项目的
pack 里「当前状态」整段是空的，最新的事实也全丢了。

这个脚本锁住正确行为：
  1. 预算充足时逐字节不动；
  2. 预算只够 baseline + card 时，先丢最旧的事实，card 必须保留；
  3. 事实按条目边界裁，不出现半句话；
  4. 极小预算不崩，基线永远保留。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_ISO = Path(tempfile.mkdtemp(prefix="dshrag-pack-"))
os.environ["DSH_RAG_PROJECTS_ROOT"] = str(_ISO / "projects")
os.environ["DSH_RAG_HANDOFFS_ROOT"] = str(_ISO / "handoffs")
os.environ["DSH_RAG_EMBED"] = "off"      # 纯文本测试，不联网

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  ✓", name)
    else:
        FAIL += 1
        print("  ✗", name, "|", detail)


def main() -> int:
    from card import build_pack
    from store import Store

    s = Store(_ISO / "t.db")
    for i in range(1, 41):
        s.remember("p", "fact", f"事实{i:02d}", f"第{i}条内容" * 40)

    facts = [m.as_dict() for m in s.facts_for_pack("p")]
    cp = {"baseline": "基线", "goal": "把裁剪顺序修正正确"}

    print("== 1 预算充足 ==")
    full = build_pack("p", card="### 当前目标\n干活", checkpoint=cp,
                      facts=facts, budget_tokens=100000, with_meta=True)
    check("不裁剪", full["truncated"] == [], str(full["truncated"]))
    check("card 在", "干活" in full["text"])
    check("40 条事实都在", len(re.findall(r"\[fact #\d+\]", full["text"])) == 40)

    print("\n== 2 紧预算：只够 baseline + card ==")
    tight = build_pack("p", card="### 当前目标\n这是最关键的一段", checkpoint=cp,
                       facts=facts, budget_tokens=300, with_meta=True)
    check("card 保留（不再被砍空）", "这是最关键的一段" in tight["text"], str(tight["sections"]))
    check("facts 被裁", "facts" in tight["truncated"], str(tight["truncated"]))
    ids = [int(x) for x in re.findall(r"#(\d+)\]", tight["text"])]
    check("保留的是最新的事实", bool(ids) and ids == sorted(ids) and ids[-1] == 40, str(ids))
    check("被裁掉的是最旧的", 1 not in ids, str(ids[:5]))
    check("提示说明更早的被省略", "更早的被省略" in tight["text"])

    body = tight["text"].split("## 1 ·", 1)[1]
    items = [x.strip() for x in body.split("\n\n") if x.strip().startswith("[fact")]
    check("每条事实完整（不截半句）", all(x.endswith("内容") for x in items),
          str([x[-8:] for x in items]))

    print("\n== 3 极小预算 ==")
    tiny = build_pack("p", card="卡", checkpoint=cp, facts=facts, budget_tokens=20)
    check("不崩", isinstance(tiny["text"], str) and tiny["text"].startswith("# 项目上下文包"))
    check("基线永远保留", "基线" in tiny["text"], tiny["text"][:120])

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
