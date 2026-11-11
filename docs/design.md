# 设计与数据结构


## 目录结构

```
dsh-rag/
├─ src/
│  ├─ server.py       # MCP stdio server（JSON-RPC 2.0，手写，无 SDK 依赖）
│  ├─ store.py        # 领域层：写入 / 检索 / 会话台账 / 上下文包
│  ├─ schema.py       # v6 表结构与增量迁移（唯一真相，迁移前自动快照）
│  ├─ card.py         # 交接卡渲染 + 上下文包分层（缓存前缀在这里定序）
│  ├─ archive.py      # 会话 zstd JSONL → 摘要 / 接力度量 / 状态草稿
│  ├─ insight.py      # 缓存命中率、压缩代价、接力效率（只读会话日志）
│  ├─ doctor.py       # 体检：索引一致性 / 会话格式漂移 / 孤儿 / 草稿积压
│  ├─ cleanup.py      # 信息生命周期：软归档过期低价值信息
│  ├─ projects.py     # 项目实体：目录权威 + 归一 + 交接卡 md 导出
│  ├─ paths.py        # 路径解析（PROJECTS / DATA / HANDOFFS 都可用环境变量改）
│  ├─ embed.py        # 智谱 embedding-3 / 本地 fastembed / 关闭
│  └─ cli.py          # 面板 Host 调用的 JSON CLI
├─ scripts/
│  ├─ watch_archive.py       # 归档守护（常驻，每 5 分钟扫一次，单实例锁）
│  ├─ backfill.py            # 手动回溯历史会话（默认只补漏，--force 才重抽）
│  ├─ install-autostart.ps1  # 装/卸登录自启（vbs，不用计划任务）
│  ├─ reembed.py             # 换 embedding 后端后重建全部向量
│  ├─ backfill_vectors.py    # 只补缺向量的记忆（日常运维用，不重建全部）
│  ├─ rebuild_fts.py         # 重建全文索引（doctor 报 FTS 不一致时用）
│  ├─ ingest_changelog.py    # 兼容入口（实际逻辑在 src/changelog.py）
│  ├─ dsh-rag-archive.vbs
│  └─ selftest_*.py / selftest_plugin.mjs
├─ docs/
│  └─ 审查-2026-11-06.md     # 一次完整代码审视的记录（问题 + 修复 + 验证）
├─ data/
│  ├─ memory.db       # 唯一的真相源，备份它就是备份全部记忆
│  ├─ backup/         # 迁移 / 清理 / 删项目前的整库快照
│  ├─ models/         # ONNX 模型缓存（只有 local 后端用）
│  └─ watch-state.json# 归档基线时间
├─ logs/archive.log · logs/mcp.log
└─ requirements.txt
```

## 数据模型（v6）

六张表，每张只做一件事：

| 表 | 一行是什么 | 关键字段 |
|---|---|---|
| `meta` | 库自身元信息 | `schema_version`、`created_at` |
| `projects` | 项目登记簿 | `name`、`dir_path`、`archived_at` |
| `memories` | **唯一的事实表**：一条记忆 | `project`、`kind`、`content`、`meta_json`、`status`、`superseded_by`、`source`、`use_count`、`last_used_at` |
| `sessions` | 一条 DSH 会话 | `session_id`、`project`、`compact_count`、`memory_id`、`archived_at`、`detail_json`（含接力度量） |
| `vectors` | 向量（按模型隔离） | `memory_id`、`model`、`dim`、`vec` |
| `memories_fts` | 全文索引 | `rowid` = `memories.id` |
| `pages` | **长文档**的一行（目录层） | `project`、`path`、`title`、`summary`、`stale_days`、`status`、`mtime` |
| `chunks` | 页面切块（检索层） | `page_id`、`idx`、`text`、`vec` |

外加两个只读视图：`v_project_stats`（每项目条数/最近更新）、`v_handoff`（当前生效的交接卡）。

`status` 有四个值，语义别混：`active`（生效）、`superseded`（被新版本取代）、
`draft`（待确认的草稿，不进检索）、`archived`（清理掉的，可 restore）。

### 项目（目录是权威，表是登记簿）

`src/projects.py` 是项目身份的唯一来源：

- **权威来源** = `DSH_RAG_PROJECTS_ROOT`（默认 `~/Documents/Projects`）下的真实目录名（自动排除 `_cache`、`_tools` 这类 `_` 开头目录）
- **保留名** = `工作区`（没归到项目上的零散会话）、`未归类`（空值兜底），不对应目录
- **归一**：`trim` → 压缩空格 → 查别名表 → 大小写不敏感匹配（真实目录 ∪ 登记簿 ∪ 已有记忆）
- **收敛**：归档从文本里猜项目名时，`Projects\my-api\` 会被收敛到 `my-api-server`；
  带点的（`README.md`）直接判负。收敛不了就归到「工作区」，**宁可归错一处，也不要造一堆孤儿项目**。

别名表放在 `data/project-aliases.json`，可选：

```json
{ "记忆库": "dsh-rag", "别名": "my-project" }
```

### 记忆的 kind

| kind | 谁写 | 说明 |
|---|---|---|
| `baseline` | 人/模型 | 项目基线：技术栈、目录、红线。**几乎不变**，缓存前缀靠它 |
| `checkpoint` | `checkpoint_put` | 结构化状态（目标/已完成/进行中/下一步/文件/未决问题），存 `meta_json` |
| `handoff` | 由 checkpoint 渲染，或 `handoff_put` 手写 | 人读的交接卡正文，同时导出 md |
| `decision` / `fact` / `pitfall` / `todo` | `remember` | 长期事实，进 `pack` 的第 1 段 |
| `session` | `archive` | 会话归档摘要；含 DSH 压缩摘要时 `source='compact-checkpoint'` |
| `note` | 任意 | 兜底 |

### 交接卡 / 结构化状态 / 上下文包

> 完整格式规范（段序、裁剪顺序、确定性要求、消费方约定）见 [docs/pack-format.md](pack-format.md)。

三者是一条流水线，不是三个东西：

```
checkpoint（结构化，机器写）  ──render_card()──▶  handoff 卡（人读，同时导出 md）
                                                        │
                                              build_pack()│
                                                        ▼
                                   上下文包 pack（新会话开场读的那一坨）
```

- **一个项目同时只有一张生效的交接卡、一个生效的 checkpoint**；旧的置 `superseded` 并填 `superseded_by`
- **旧记录保留不清**，可以追溯历史（面板上能看到 id 递增的那串）
- **数据库是真相源**，md 只是导出物；写 md 失败不影响入库

### 为什么 pack 要分层（缓存前缀）

DeepSeek 的上下文缓存是**逐 token 前缀精确匹配**：从第一个字开始完全一样，后面的 token 才算命中。
所以「把最新状态写在最前面」= 每次开场都把整段前缀改写一遍，缓存全部失效。

pack 的固定顺序就是为这个设计的：

| 段 | 内容 | 变化频率 |
|---|---|---|
| 0 | 基线 | 几乎不变 → **跨会话一直命中** |
| 1 | 长期事实（`decision`/`pitfall`/`fact`/`todo`） | 只在**末尾追加**，按 id 升序 |
| 2 | 当前状态（交接卡，会变） | 每次收尾都变 |
| 3 | 下一步（只在没有卡时单独列） | 会变 |

**变化的一律排在后面**，新会话的请求前缀就能命中上一段的缓存。
另外 pack 默认**不写时间戳 / id / score**——这些字段每次都不同，写进前缀就是自毁缓存；
要元信息得显式 `with_meta=true`。

### 实时性：哪些会跟着变，哪些不会

| 你在磁盘上做的事 | 项目列表 | 记忆数据 |
|---|---|---|
| 新建 `Projects\<名>\` | ✅ 立刻出现（面板每 10 秒轮询，或下次请求） | ❌ 不自动建卡，面板上点「建卡」 |
| 删除目录，且该项目没有记忆 | ✅ 立刻消失 | — |
| 删除目录，但该项目有记忆 | ⚠️ 仍显示，标记「孤儿」 | ❌ 保留，面板上点「清理」显式删除 |

**设计理由**：目录是文件系统的事，记忆是数据的事。自动删数据风险太高——
一次误删目录就能把几个月的上下文清空。所以删除项目目录**永远不**连带删记忆，
只把它标成孤儿，删不删由人决定。

`prune` 命令也强制要求显式确认：

```powershell
.venv\Scripts\python.exe src\cli.py prune --project <名>           # 只报告，不删
.venv\Scripts\python.exe src\cli.py prune --project <名> --confirm  # 真删
```
