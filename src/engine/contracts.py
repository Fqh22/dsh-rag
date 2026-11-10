"""引擎契约（P0）：只有类型和协议，没有实现。

这三个 Protocol 是「通用记忆引擎」的最小面：

    SessionSource  会话从哪来（现在只有 DSH 的 zstd JSONL）
    Repository     记忆存到哪（现在是 store.Store + SQLite）
    PackFormat     上下文包怎么渲染（现在是 card.build_pack）

设计原则：
- **命名与宿主解耦**：一律用 `namespace`，不用 `project`（project 只是 DSH 的一种取值）。
- **不碰 IO**：协议描述行为，不描述文件系统、网络、平台。
- **行为冻结**：本文件不改任何现有逻辑；P1/P2 逐步让现有实现对齐。

现有实现的对应关系（P2 时按这张表收编）：

    Repository.save       ← store.Store.remember
    Repository.get        ← store.Store.get_memory
    Repository.search     ← store.Store.recall
    Repository.supersede  ← store.Store.supersede_session_memories / set_status
    Repository.recent     ← store.Store.recent
    Repository.stats      ← store.Store.stats

    SessionSource.scan    ← archive.iter_session_files + run() 里的 mtime/cutoff 过滤
    SessionSource.read    ← archive.parse_session

    PackFormat.render     ← card.build_pack
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable

# ---------------------------------------------------------------- 会话源

@dataclass(frozen=True)
class SessionRef:
    """一个会话的引用（扫描阶段拿到的全部信息，不解析内容）。"""

    id: str
    path: str
    mtime: float
    namespace: str | None = None      # 扫描时能推断就填，不能就留给 read 之后


@dataclass(frozen=True)
class SessionTurn:
    """会话里的一轮事件。适配器负责把宿主格式压成这个形状。"""

    role: str                          # user | assistant | tool | compact
    text: str = ""
    kind: str = ""                     # 原始事件的细分类型（如 compact-checkpoint）
    ts: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)   # input/cache tokens 等


@runtime_checkable
class SessionSource(Protocol):
    """会话来源。DSH 是第一个实现，Markdown 是第二个（P3）。"""

    name: str

    def scan(self, since: float | None = None) -> list[SessionRef]:
        """列出候选会话（since 是 mtime 下限，None 表示不限）。"""

    def read(self, ref: SessionRef) -> Iterator[SessionTurn]:
        """按时间顺序产出事件。解析失败应抛出，不要静默返回空。"""


# ---------------------------------------------------------------- 存储

@dataclass
class MemoryRecord:
    """一条记忆的只读视图（与 store.Memory 字段对齐，去掉内部实现细节）。"""

    id: int
    namespace: str
    kind: str
    title: str
    content: str
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    session_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@runtime_checkable
class Repository(Protocol):
    """记忆的存取。6 个方法覆盖现有 store 的全部对外行为。"""

    def save(
        self,
        namespace: str,
        kind: str,
        title: str,
        content: str,
        *,
        meta: dict[str, Any] | None = None,
        session_id: str | None = None,
        ts: str | None = None,
        source: str = "manual",
        revive: bool = False,
    ) -> tuple[int, bool]:
        """写入一条记忆，返回 (id, 是否新建)。内容完全相同时幂等。"""

    def get(self, memory_id: int) -> MemoryRecord | None:
        """按 id 取一条。"""

    def search(
        self,
        namespace: str | None,
        query: str,
        *,
        limit: int = 8,
        kinds: list[str] | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryRecord]:
        """混合检索。namespace 为 None 表示跨命名空间。"""

    def supersede(
        self,
        namespace: str,
        kind: str,
        *,
        keep_id: int | None = None,
    ) -> int:
        """把某命名空间下该 kind 的记录置为 superseded（keep_id 指定的除外）。"""

    def recent(
        self,
        namespace: str | None = None,
        *,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        """最近写入的生效记忆。"""

    def stats(self) -> dict[str, Any]:
        """条数、向量覆盖、结构版本等。"""


# ---------------------------------------------------------------- 上下文包

@dataclass(frozen=True)
class Pack:
    """一次渲染的结果。text 是给模型读的，其余是元信息（不进前缀）。"""

    namespace: str
    text: str
    version: str
    est_tokens: int
    chars: int
    truncated: list[str] = field(default_factory=list)
    sections: dict[str, int] = field(default_factory=dict)
    has_handoff: bool = False
    has_checkpoint: bool = False


@runtime_checkable
class PackFormat(Protocol):
    """上下文包渲染器。分层顺序与裁剪策略是契约的一部分，不只是实现细节。"""

    def render(
        self,
        namespace: str,
        *,
        budget_tokens: int = 3000,
        with_meta: bool = False,
    ) -> Pack:
        """渲染上下文包。同一输入必须产出逐字节相同的 text。"""
