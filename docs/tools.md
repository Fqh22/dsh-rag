# 工具与页面层


## MCP 工具

| 工具 | 用途 |
|---|---|
| `handoff_pack(project, budget_tokens?, with_meta?)` | **新会话开场首选**：稳定分层上下文包 + 续接建议 |
| `handoff_get(project)` | 只读交接卡正文（旧习惯，仍可用） |
| `checkpoint_put(project, baseline?, goal?, done?, doing?, next?, files?, open_questions?)` | 写结构化状态，自动重渲染交接卡 |
| `checkpoint_get(project)` | 读结构化字段（含归档生成的**待确认草稿**） |
| `checkpoint_promote(project)` | 把草稿并入生效状态 |
| `recall(query, project?, limit?, kinds?)` | 混合检索（关键词 + 向量，RRF 融合），带**类型重排** |
| `remember(project, kind, title, content)` | 写入一条记忆，内容重复自动去重 |
| `memory_update(id, title?, content?)` | **改**一条已有记忆（纠错，不留矛盾记录） |
| `memory_recent(project?, limit?)` | 看最近写入 |
| `memory_stats()` | 统计、结构版本、向量后端 |
| `cache_stats(project?, limit?)` | 缓存命中率、压缩代价、接力度量 |
| `doctor()` | 体检：版本、索引一致性、会话格式、孤儿、草稿积压 |
| `cleanup(apply?)` | 低价值信息的清理计划（默认只出计划，执行是软归档） |
| `archive_sessions(project?, limit?)` | 手动触发会话归档 |
| `pages_index(project?, limit?)` | 长文档目录：标题 + 摘要 + 是否过期 |
| `pages_search(query, project?, limit?)` | 长文档段落级检索（记忆查不到细节时用） |

`kind` 约定：`baseline` / `checkpoint` / `handoff` / `decision` / `fact` / `todo` / `pitfall` / `session` / `note`。

> `memory_update` 会同时重算 FTS 索引和向量。只改其中一样的话，会出现
> 「改完关键词搜不到」或「改了还按旧内容语义召回」——这两个坑都踩过。

## 页面层：长文档（v6 增量）

`memories` 的粒度是「一条事实」。长文档（设计稿、运维手册、变更记录）塞进去
只有两个结果：整篇太贵，切碎丢上下文。所以单独一层：

```
pages    标题 + 摘要 + 保质期  →  当目录注入，几十篇也只要几百 token
chunks   400 字一块 + 向量      →  要细节时按段检索
```

**和 memories 的关系**：读写都不碰 memories，纯增量。建表在 `Store` 初始化时自动做。

```bash
# 灌一个目录（增量：sha 没变就跳过）
python src/cli.py pages --project qq-cloud-bot --dir path/to/docs
python src/cli.py pages --project qq-cloud-bot --dir path/to/docs --reindex   # 强制重算

# 看目录
python src/cli.py pages --project qq-cloud-bot --limit 20

# 段落检索（向量优先，没向量退回关键词）
python src/cli.py pages --project qq-cloud-bot --query "部署前要跑什么验证"
```

**时效**：`stale_days` 按内容判（价格 14 天 / 版本·迁移 30 天 / 政策 180 天 / 其余 180 天），
过期的在目录里带「可能过时」标记，检索结果也带 `stale: true`。

**摘要怎么来**：规则取引言区（第一个二级标题之前），允许引用块，跳过表格和列表。
试过直接取第一段——拿到全是表格头，当目录没法看。
