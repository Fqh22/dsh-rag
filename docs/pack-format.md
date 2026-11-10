# 上下文包格式（pack-format v1）

> 状态：P5 产物。`card.build_pack()` 是参考实现，任何消费方都应能按本文复现同样的字节。
> 目标不是"好看的 Markdown"，而是**可预测、可缓存、可校验**。

## 1. 它解决什么

LLM 的上下文缓存是**逐 token 前缀精确匹配**：只有从第一个字开始完全一样，后面的 token 才算命中。
所以「把最新状态写在最前面」等于每次开场都把整段前缀改写一遍。

pack 的分层顺序就是为这个设计的：**越稳定的越靠前，越易变的越靠后**。

## 2. 段序（不可调换）

| # | 段 | 内容 | 变化频率 |
|---|---|---|---|
| 0 | `baseline` | 技术栈、目录、红线 | 几乎不变 |
| 1 | `facts` | decision / pitfall / fact / todo，**按 id 升序** | 只追加 |
| 2 | `card` | 交接卡（当前目标 / 已完成 / 进行中 / 文件 / 未决问题） | 每次状态更新 |
| 3 | `next` | 下一步（**只在没有 card 时单独出现**） | 每次状态更新 |

文件头固定两行：

```
# 项目上下文包 · {namespace}
<!-- 由 dsh-rag 生成；基线不变则这一段字节不变，可跨会话复用前缀缓存 -->
```

## 3. 不写进前缀的东西

- 时间戳（`created_at` / `updated_at`）
- 记忆 id、检索分数
- 读取提示（续接建议、pack 版本号）

这些**每次都会变**，写进前缀等于自毁缓存。需要时用 `with_meta=true` 显式开启，且只对 `facts` 行生效
（格式变成 `[kind #id] title`）。

## 4. 裁剪策略（预算不够时）

预算换算：`limit_chars = budget_tokens × 1.6`（中文约 1.6 字符/token，只用于预算，不用于计费）。

收缩顺序**固定**：

1. **先丢 `next`** —— 有 card 时它本来就是重复信息；
2. **再裁 `facts`** —— 从**最旧**的开始丢，保留最新的；在 `\n\n` 条目边界切，不切半句；
   裁过之后 header 换成：
   `## 1 · 长期事实（按时间顺序，不改写；更早的被省略，用 recall 查）`
3. **最后才动 `card`** —— 它是「现在在干什么」，比任何一条历史事实重要；
   截断时保留开头、追加 `…（已截断，完整状态用 checkpoint_get）`。

> 这条顺序是 2026-11-06 修的：旧实现先砍 card 再砍 facts，且 facts 保留最旧的，
> 导致两个最活跃项目的 pack 里当前状态整段为空。

`baseline` **永远不裁**——它是缓存的地基。

## 5. 确定性

同一输入（同样的 baseline、同样的事实集合、同样的 card、同样的预算）必须产出**逐字节相同**的 text。

- 事实按 id 升序，只追加；
- 渲染不做时间格式化；
- `version` = 全文 sha256 的前 8 位（`_hash(text)[:8]`），可用作缓存键或变更检测。

## 6. 消费方约定

- **只读**：pack 是快照，不是状态存储；要改状态用 `checkpoint` / `remember`。
- **不要改写**：下游要补充信息，请追加在自己的 prompt 里，不要拼接进 pack 文本（会破坏版本哈希）。
- **缓存友好**：把 pack 放在请求的**最前面**，易变的指令/问题放后面。
- **预算**：默认 3000 token；给不足 600 会让 `card` 也被裁。

## 7. 机器可读摘要

`card.describe_pack_header()` 产出一行元信息（放在 pack 之外，**绝不进前缀**）：

```
pack v{version} · 约 {estTokens} token · 基线 v{baselineVersion} · 已裁剪 {next,card,facts}
```

## 8. 与其它实现的兼容

`engine.contracts.PackFormat` 是这个格式的协议：

```python
class PackFormat(Protocol):
    def render(self, namespace, *, budget_tokens=3000, with_meta=False) -> Pack: ...
```

只要实现满足「段序 + 裁剪顺序 + 确定性」三条，产出的包就能和 dsh-rag 互换使用。
