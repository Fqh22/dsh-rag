# dsh-rag — DSH 跨会话项目记忆库

[![ci](https://github.com/Fqh22/dsh-rag/actions/workflows/ci.yml/badge.svg)](https://github.com/Fqh22/dsh-rag/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/dsh-rag-panel)](https://www.npmjs.com/package/dsh-rag-panel)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

给 DeepSeek Harness 加一层**按项目分类的长期记忆**：会话变长归档、新开会话时，
不用再从头交接一遍。

形态：本地 MCP server（stdio）+ 会话归档守护。**数据全部留在本机**；只有向量化默认要联网（智谱 embedding），可切本地模型或完全关闭。

当前版本 **v0.7**：修掉「pack 丢当前状态」「baseline 改不动」「自测污染生产文件」三个硬伤，并补了迁移备份、单实例锁、以及一套更严的自测隔离。细节见 `docs/审查-2026-11-06.md`。

---

## 它解决什么

| 痛点 | 这里的对策 |
|---|---|
| 会话太长被归档，新会话什么都不记得 | 每项目一张**交接卡**（由结构化状态渲染），开场一条 `handoff_pack` 读完 |
| 新会话拿不回"上次干到哪"的现场 | `checkpoint_put/get` 存结构化状态；`handoff_pack` 末尾给续接建议（能恢复会话就别重开） |
| 每次开场都重算前缀，缓存命中上不去 | pack 按**稳定→易变**分层：基线在前、事实只在末尾追加，跨会话能复用前缀 |
| 长会话被 DSH 压缩后，最贵的那段上下文丢了 | 归档直接把 `compact-checkpoint` 的摘要整段收进记忆库（v0.4 只留了 300 字） |
| 收尾总是忘记写交接卡 | 归档自动生成**状态草稿**（改过的文件、目标、做过什么），模型只需审阅采纳 |
| 模型不一定会主动读 pack | 插件在**第一句用户消息后**自动注入一句指针（可关） |
| 想找"上次那个问题怎么解决的" | `recall` 混合检索历史记忆与会话归档 |
| 不同项目的信息混在一起 | 记忆按 `project` 隔离，检索默认只在本项目内 |
| 旧的错误方案被当成现行方案 | 记忆有 `status`，写新状态会让旧记录自动失效并填 `superseded_by` |
| 缓存到底有没有变好，只能靠感觉 | `cache_stats` 给出命中率与压缩代价；`relay` 给出"几步才第一次写" |
| 库越来越大、旧信息稀释检索 | `cleanup` 按生命周期**软归档**（可恢复），`doctor` 先体检再动手 |

**它不是一个通用文档问答系统。** 通用 RAG（切块 + 向量召回）回答的是"说过什么"，
而交接需要的是"现在是什么状态"——所以这里以**结构化记忆**为主，向量只做兜底召回。

---

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

---

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

> 完整格式规范（段序、裁剪顺序、确定性要求、消费方约定）见 [docs/pack-format.md](docs/pack-format.md)。

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

---

## 环境与安装

需要 Python 3.10+（用系统 Python，或 DSH 自带的运行时都行）。

```powershell
git clone https://github.com/Fqh22/dsh-rag.git dsh-rag
cd dsh-rag
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

> 国内网络建议用清华镜像；用官方源把 `-i` 去掉即可。

**环境变量**（都有合理默认值，通常只需要第一个）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `DSH_RAG_PROJECTS_ROOT` | 被记忆的项目放在哪个目录（一个子目录 = 一个项目） | `~/Documents/Projects` |
| `DSH_RAG_DATA` | 运行期数据目录（库、备份、状态文件） | `data/` |
| `DSH_RAG_DB` | 数据库路径（覆盖 `DSH_RAG_DATA` 里的默认值） | `data/memory.db` |
| `DSH_RAG_HANDOFFS_ROOT` | 没有项目目录时，交接卡 md 的导出位置 | `data/handoffs` |
| `DSH_RAG_EMBED` | 向量后端：`zhipu` / `local` / `off` | `zhipu` |
| `DSH_RAG_SCAN_SECONDS` | 归档守护的扫描间隔（秒） | `300` |
| `DSH_RAG_ROOT` | 面板插件去哪里找 Python 仓库（npm 安装时必设） | 自动探测 |
| `DSH_RAG_PYTHON` | 面板调 CLI 时用的 Python 解释器 | 项目的 `.venv`，找不到就用 `python` |

向量化默认走**智谱 embedding-3**（`https://open.bigmodel.cn/api/paas/v4/embeddings`，1024 维），
不需要下载任何模型，纯 HTTP 调用，单条约 150–400 ms。

**Key 的来源**（按优先级）：

1. 环境变量 `ZHIPUAI_API_KEY` / `ZHIPU_API_KEY`
2. 注册表 `HKCU\Environment`（DSH 桌面端登录时启动，未必继承后来设的变量，所以补这条路）
3. 项目内 `data/.zhipu_key` 文件（已在 `.gitignore` 里，最后的兜底）

想完全离线跑：`set DSH_RAG_EMBED=local`，装 fastembed，然后重建向量。

自测/离线也可以设 `DSH_RAG_EMBED=off`：完全不发网络请求，检索降级为纯关键词。
`scripts/selftest_*.py` 默认就用它——测试不该花 API 费用，也不该被网络拖慢。

| 后端 | 模型 | 维度 | 依赖 |
|---|---|---|---|
| `zhipu`（默认） | embedding-3 | 1024 | 只需联网 + key |
| `local` | bge-small-zh-v1.5 | 512 | 91MB ONNX 模型，纯 CPU |
| `off` | — | — | 纯关键词检索，不联网 |

### 数据与隐私

- **记忆全在一个文件里**：`data/memory.db`（SQLite）。备份 = 拷这一个文件；迁移 = 换机器后放回原位。
- **默认只有向量化会出网**：走智谱时，记忆的「标题 + 正文」会发给 `open.bigmodel.cn` 换向量。
  介意就把 `DSH_RAG_EMBED` 设成 `local`（本地模型）或 `off`（纯关键词，完全不出网）。
- **key 不进仓库**：依次读环境变量、Windows 注册表、`data/.zhipu_key`；后两者都在 `.gitignore` 里。
- **仓库里没有你的数据**：`data/`、`logs/`、`交接卡.md` 全部被忽略；导出的交接卡带项目名和文件路径，
  只适合放在你本机或私有仓库。
- **归档读取的是本机会话日志**：`~/.dsh/sessions/**/session.v4.jsonl.zstd`，只读，不复制。

> ⚠️ **换后端必须重建向量**：维度不同，新旧向量混在一起算余弦没有意义。
> 跑 `.venv\Scripts\python.exe scripts\reembed.py`，它会重建并清掉其它模型的旧向量。
>
> 只是「有些记忆没向量」（服务在跑旧代码、写库时抛过异常）时，别全量重建，用
> `.venv\Scripts\python.exe scripts\backfill_vectors.py`：只补缺的，省 API 调用。
> 看缺多少：`memory_stats` 里的「缺向量」，或 `store.stats(MODEL_NAME)`。

### 把项目的变更记录灌进记忆库

有些项目把「为什么这么改」写在变更记录里（`docs/99-*.md`、`CHANGELOG.md`、
`docs/CHANGELOG.md`、`变更记录.md` 都认）：跟着代码走、适合顺序读，但只能 grep。
记忆库能语义检索，却要人手抄一遍结论——两边各写一次必然漂移。
所以它是**库的一环**，不是某个项目的脚本：

```bat
.venv\Scripts\python.exe src\cli.py ingest --dry-run      :: 先看会灌哪些
.venv\Scripts\python.exe src\cli.py ingest                :: 所有项目，增量
.venv\Scripts\python.exe src\cli.py ingest --project qq-cloud-bot
.venv\Scripts\python.exe src\cli.py ingest --file 路径\CHANGELOG.md
```

- **自动跑**：归档守护（`watch_archive.py`）每天检查一次，新条目自动入库，
  单项目单次上限 10 条，防止某个项目一次灌几百条把记忆库冲掉。
- **只灌带结论的**：正文含 教训/结论/踩/值得/别再/回滚/代价/风险 之一才收；
  纯过程记录（改了几个文件、跑了几次测试）跳过——灌多了会把真正的决策淹掉。
  想全都灌用 `--all-entries`。
- **存储**：`kind=changelog`，标题 `99 · {原标题}`，正文取开头 400 字**原样**，
  末尾带 `[出处]`。试过两种"智能压缩"（按关键词抽结论行、首段+教训小节），
  抽出来都是半句话，不如原样截取 + 靠标题命中。
- **幂等**：`data/changelog_state.json` 记已灌条目，记忆库自身还有 content_hash 去重；
  重复跑新增为 0。
>
> **并发写的一个坑**：SQLite 默认 `busy_timeout=0`，多个进程（MCP 服务、归档守护、运维脚本）
> 同时写时后到的会直接报 `database is locked`。更隐蔽的是写失败后没有 `rollback`：
> 那条连接会一直挂着写事务，把其它进程全堵住。现在的对策：写失败即回滚、
> 所有连接 `PRAGMA busy_timeout=5000`、`doctor` 会报缺向量。
> **改完 `store.py` / `schema.py` 必须重启 DSH**，否则跑的还是旧代码。

---

## 接入 DSH

在 `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml` 末尾（注意必须包在 `- insert:` 里，见下方「两个必须知道的坑」）：

```yaml
- insert:
    - id: dsh-rag
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: rag              # 工具会变成 mcp__rag__recall 这种名字
        transport: stdio
        command: 'C:\path\to\dsh-rag\.venv\Scripts\python.exe'
        args: ['C:\path\to\dsh-rag\src\server.py']
        cwd: 'C:\path\to\dsh-rag\src'
        env:
          PYTHONIOENCODING: utf-8
        toolCallTimeoutMs: 120000
        failOnStartupError: false
```

改完**重启 DSH** 生效。验证：随便问一句，模型工具表里应出现 `mcp__rag__*`。

> ⚠️ **profile 别写错**：DSH Web GUI 读 `profiles/web/cordis.patch.yml`，桌面端读 `profiles/desktop/`。
> 两处现在都配了；只配桌面端时 Web 界面上是看不到 `mcp__rag__*` 的（踩过）。

### HTTP 模式（远程 / 多 host）

stdio 之外还有一个 **stateless HTTP** 通道，给不能起子进程的 host 用：

```powershell
.venv\Scripts\python.exe src\server.py --http 127.0.0.1:8765
```

| 端点 | 用途 |
|---|---|
| `POST /mcp` | JSON-RPC 2.0（单条或批量），无会话状态，每条请求独立 |
| `GET /healthz` | 探活：返回 `server` / `version` / `db` |

- **没有鉴权**：传输层只负责收发，默认绑回环。要暴露到局域网，请自己套反向代理并加认证。
- **不做 SSE**：会话管理与断线重连留给以后；现在只解决「远程/多 host 能连上」。
- stdio 与 HTTP 共用同一份 `dispatch()`，不存在「一条通道能跑、另一条少个分支」。

---

## 可视化面板（Web 插件）

界面上有个右下角悬浮按钮（带条目数角标），点开是记忆库面板。

> **插件市场里已经能搜到**：在 DSH 的 **Plugins → Discover**（或 dshmarket 面板）搜 `dsh-rag`，
> 条目是 `Fqh22/dsh-rag#plugin`，分类 *Memory*，收录于
> [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin)（[详情页](https://awesome-dsh-plugin.com/p/Fqh22/dsh-rag--plugin/)）。
> 市场里一键装的是面板插件；Python 侧的记忆服务仍需按上面「接入 DSH」配一次。

**能做什么**

| 操作 | 怎么用 |
|---|---|
| 项目总览 | 切到「全部」且搜索框为空时，先列出所有项目：有无交接卡、条目数、会话数、压缩次数；点一行进入该项目 |
| 缓存指标 | 总览顶部一行：最近 N 个会话的缓存命中率与压缩次数（来自 `/dsh-rag/api/cache`） |
| 查找 | 顶部搜索框，输入即检索（关键词 + 语义），命中词高亮；`Ctrl/Cmd+K` 聚焦，`Esc` 清空/退出 |
| 浏览 | 项目 chips 切换范围；点条目展开全文 |
| 改 | 条目右上角 `✎` 进入编辑态，改标题/正文，`Ctrl+Enter` 或点保存；`Esc` 取消 |
| 看交接卡 | 选中项目时，金色边框卡片就是该项目的交接卡 |

保存后会同时更新 FTS 索引和向量，所以改完立刻能按新内容搜到。

**位置**：按钮刻意避开右下角的桌宠挂件——这类挂件通常是
`position:fixed; right:0; bottom:0`、尺寸按短边百分比伸缩的正方形，
所以面板用同一个公式算 `bottom`，屏大字小都能自动让开。挂件尺寸因安装而异，
改 `client.js` 里的 `WHALE_SPACE` 即可。

```
plugin/
├─ package.json      # dsh.bundle.patch + dsh.client(platform: web)
├─ cordis.patch.yml  # insert 一行 dsh-rag-panel
├─ index.js          # Host：注册 /dsh-rag/api/*，转调 src/cli.py
├─ client.js         # Client：注册到 shell.overlay 的 React 面板
├─ locale/{zh,en}.json
└─ icon.svg
```

**装配方式**（以 desktop profile 为例）：

方式 A —— 从 npm 装（使用者）：

```powershell
cd $HOME\.dsh\profiles\desktop
pnpm add dsh-rag-panel
```

然后把 `dsh-rag-panel` 加进该 profile `package.json` 的 `dsh.profile.bundles`，重启 DSH。

插件会自己找 Python 仓库，顺序是：`DSH_RAG_ROOT` → 插件上级目录 → `~/Documents/Projects/dsh-rag` → `~/dsh-rag`。
都找不到时面板会显示明确报错（而不是空白），照提示设 `DSH_RAG_ROOT` 即可。

方式 B —— 本地目录联接（改插件代码立刻生效）：

```powershell
# 把 <repo> 换成本仓库的绝对路径
New-Item -ItemType Junction `
  -Path  "$HOME\.dsh\profiles\desktop\node_modules\dsh-rag-panel" `
  -Target "<repo>\plugin"

# profile 清单：dependencies 加 "dsh-rag-panel": "link:<repo>\plugin"，bundles 加 "dsh-rag-panel"
#    （改之前先备份 package.json）
```

`bundles` 数组里的包会自动应用它自己的 patch，所以不需要在 `cordis.patch.yml` 里再写 insert。

### 两个必须知道的坑

**1. 新增配置行必须用 `- insert:`**

```yaml
# ✗ 错：这是「按 id 覆盖已存在的行」，id 不存在时被静默忽略
- id: dsh-rag
  name: "@deepseek-ai/dsh-mcp-client"

# ✓ 对：新行包在 insert 里
- insert:
    - id: dsh-rag
      name: "@deepseek-ai/dsh-mcp-client"
      config: { ... }
```

表现是「MCP 完全不启动，且没有任何报错」。踩了两次才定位。

**2. 桌面端 index.html 不经过 tapIndex**

桌面壳的页面从安装包静态 dist 直出（`dsh-app://`），`dsh.client` 字段和 `tapIndex`
在这条路上都不生效。唯一通道是 `webserver/index-inject` 推结构化注入行：

```js
ctx.on('webserver/index-inject', (table) => {
  table.push({ kind: 'script', placement: 'body', text: '...' })
})
```

而且必须推**内联行**（`kind: 'script'` + `text`），不能用 `kind: 'script-src'`——
页面解释器对后者是「加载失败即 reject」，会把 `__DSH_BOOT_READY__` 一起弄挂，整个应用起不来。

**接口**（只允许回环访问，非本机 Host 一律 403）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/dsh-rag/api/state` | 统计 + 项目列表 + 结构版本 |
| GET | `/dsh-rag/api/overview` | 项目总览（含会话数/压缩次数） |
| GET | `/dsh-rag/api/handoff?project=` | 读交接卡 |
| POST | `/dsh-rag/api/handoff` | 写交接卡 |
| GET | `/dsh-rag/api/recent?project=&limit=` | 最近条目 |
| GET | `/dsh-rag/api/recall?q=&project=&limit=` | 检索 |
| GET | `/dsh-rag/api/cache?project=&limit=` | 缓存命中/压缩统计 |
| POST | `/dsh-rag/api/remember` | 写入记忆 |
| POST | `/dsh-rag/api/prune` | 删项目全部记忆（要 `confirm: true`） |

Host 侧只用 `execFile` 调 `src/cli.py`，不在 Node 里读 SQLite——Electron 内置 Node 的
`node:sqlite` 可用性不可控，且两套实现必然漂移。

自测：`node scripts/selftest_plugin.mjs`（mock 掉 DSH 宿主，验证路由注册、回环拦截、CLI 链路、slot 注册）。

**卸载**：删掉 junction + 用 `package.json.bak-20261101-1315` 覆盖 profile 清单，重启。

---

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

---

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

---

## 日常工作流

**新会话开场**（三句话就够让模型自己去做）：

> 先 `handoff_pack` 读一下当前项目的上下文包。

包里末尾会给续接建议：最近会话没被压缩过 → 去 DSH 里**恢复那个会话**（现场完整、前缀缓存全中）；
压缩过 → 按 pack 开新局。

**会话收尾**：

> 把这次的关键结论 `remember` 进去，再用 `checkpoint_put` 更新结构化状态。

**自动归档**：装了自启后，守护进程每 5 分钟扫一次
`~/.dsh/sessions/**/session.v4.jsonl.zstd`，把 5 分钟没写入过（即已结束）的会话抽成摘要入库。
归档按 `source.kind` 白名单过滤（`user` / `assistant` / `compact-checkpoint` / tool call），
环境注入（agent-instructions、runtime-context、skill-catalog、time-context…）一律跳过；
项目归属靠会话里的 `Projects\<名>` 路径与项目名关键词推断。

```powershell
# 安装登录自启（可选；不装的话 MCP 服务启动时也会补一次归档）
powershell -ExecutionPolicy Bypass -File "<repo>\scripts\install-autostart.ps1"

# 手动补录全部历史会话（force：已归档的也重新抽）
.venv\Scripts\python.exe scripts\backfill.py
```

**历史存量**：基线写在 `data/watch-state.json` 的 `archive_from`，守护默认不回溯。

**开场自动注入（v0.6）**：面板插件监听 `session/event`，在**第一句用户消息**到达时
调 `cli.py hook` 推断项目；命中就往 agent 的 inbox 里注入一句指针：

```
【dsh-rag】本轮命中项目「my-project」（上下文包 v642e69df，约 411 token）。
开工前请先调用 mcp__rag__handoff_pack(project="my-project") 读上下文包；
收尾时用 checkpoint_put 更新结构化状态。
```

- 只注入**指针**，不注入整包——整包由模型按需读，避免每轮都付 token
- 每个会话只尝试一次；推断不出项目就什么都不做
- 关闭：环境变量 `DSH_RAG_AUTO_INJECT=off`；每次决定记在 `logs/auto-inject.log`
- 为什么不在会话创建时注入：那一刻还不知道是哪个项目，得等用户开口

---

## 信息生命周期：什么时候该清理

记忆不是越多越好。**会话摘要是唯一快速贬值的东西**——它是"当时聊过什么"的记录，
一旦项目有了 checkpoint，它就不再支撑"现在是什么状态"。留着只会稀释检索、撑大 pack。

### 判断依据（按可信度排序）

| 依据 | 说明 | 是否自动执行 |
|---|---|---|
| 时效 | 会话摘要 30 天后、且项目已有 checkpoint → 降级 | ✅ 自动（`cleanup --apply`） |
| 时效（硬） | 任何会话摘要超过 90 天 | ✅ |
| 结构位置 | 失效交接卡每项目只留最近 3 张；失效 checkpoint 留 5 条 | ✅ |
| 过期草稿 | 7 天没人确认的草稿 | ✅ |
| 使用情况 | `use_count = 0` 且超过 90 天的普通记忆 | ⚠️ 只报告，不自动动 |
| 孤儿项目 | 目录已删、很久没更新 | ⚠️ 只报告 |

`use_count` 是 v0.6 加的：每次 `recall` 真的把某条返回给调用方就 +1。
它能区分"写了没人用"和"一直在用"，但**不足以单独构成删除理由**——所以只做提醒。

### 召回为什么要重排

`changelog` 和 `session` 是**日志型**记忆：又长（400 字起）、又什么都沾一点，
在 RRF 下会同时命中关键词和向量，把精炼的 `decision` / `pitfall` 挤出前几名。

实测（灌入 39 条 changelog 之后，8 条结果）：5 个典型查询里有 4 个是 **5–7 条 changelog**，
真正的结论排在后面。

`Store._rerank` 的规则：

| 类型 | 分数权重 | 单次最多占 |
|---|---|---|
| `changelog` | ×0.78 | `limit // 3` 条（至少 1） |
| `session` | ×0.88 | `limit // 3` 条（至少 1） |
| 其它 | ×1.0 | 不限 |

候选池同时从 `limit` 提到 `max(limit*3, 24)`——池子太小就没得挑，限流等于没做。
调用方显式传 `kinds` 时不重排（那是精确查询，别再自作主张）。

效果：同一组查询，changelog 从 5–7 条降到 2 条，`decision`/`pitfall` 回到前排。

### 怎么执行

```powershell
python src/cli.py cleanup                 # 只出计划（默认）
python src/cli.py cleanup --apply         # 软归档：status=archived，可 restore
python src/cli.py restore --id 123        # 反悔
python src/cli.py cleanup --purge         # 硬删 archived 超过 180 天的（先自动备份）
```

- **默认不删只归档**：archived 的记录检索、pack、recent 全部看不到，但行还在
- 每次 `--apply` 前自动整库备份到 `data/backup/cleanup-<时间>.db`
- 阈值都能改：`--session-days 14 --hard-days 60 --keep-handoffs 2`

### 当前该清什么（首次实跑）

首次清理：**几十条失效交接卡、约 27 KB**，全部是"结构位置"类；
会话摘要都还不到 30 天，一条没动。清理后第二次跑计划为空。

> 结论：这个项目现在**不需要激进清理**——数据才两天。真正会堆积的是半年后的
> 会话摘要，而规则已经就位，到时候一条命令的事。

---

## 运维

```powershell
# 自测（互相独立）
.venv\Scripts\python.exe scripts\selftest_store.py
.venv\Scripts\python.exe scripts\selftest_v5.py      # 迁移 / checkpoint / pack / 会话台账
.venv\Scripts\python.exe scripts\selftest_v6.py      # 使用计数 / 草稿 / 清理 / 体检 / 接力
.venv\Scripts\python.exe scripts\selftest_update.py
.venv\Scripts\python.exe scripts\selftest_projects.py
.venv\Scripts\python.exe scripts\selftest_mcp.py     # 需要能开子进程
node scripts\selftest_plugin.mjs

# 结构与缓存
# 注意：一定要用 .venv 里的 python。用别的解释器（比如 DSH 自带那个）跑时，
# 缺 zstandard 会让会话日志解析出 0 条——看起来像数据坏了，其实只是缺依赖。
# 现在的 doctor 会把这句单独报成 warn，不再算错误。
.venv\Scripts\python.exe src\cli.py doctor           # 体检（升级 DSH 后必跑）
.venv\Scripts\python.exe src\cli.py schema           # 版本、各表条数、是否有 _v4_* 备份表
.venv\Scripts\python.exe src\cli.py cache --limit 20 # 命中率、压缩代价
.venv\Scripts\python.exe src\cli.py relay            # 几步才第一次写、重复读几次
.venv\Scripts\python.exe src\cli.py cleanup          # 清理计划（--apply 才执行）
.venv\Scripts\python.exe src\cli.py draft --project dsh-rag   # 看待确认草稿
.venv\Scripts\python.exe src\cli.py pack --project dsh-rag
.venv\Scripts\python.exe src\cli.py sessions --limit 10

# 备份：一个文件就是全部记忆
Copy-Item data\memory.db data\backup\memory-$(Get-Date -f yyyyMMdd-HHmm).db
```

日志：`logs\archive.log`、`logs\mcp.log`、`logs\auto-inject.log`（开场注入的每次决定）。
守护进程若起不来，先手动跑 `python scripts\watch_archive.py` 看报错。

---

## 从 v0.4 / v0.5 升级

迁移是**自动**的：任何进程打开旧库时，`schema.ensure()` 会把结构升到当前版本。

- **v4 → v5**：建新表、搬数据、重建 FTS；旧表**不删**，重命名成
  `_v4_memories` / `_v4_vectors` / `_v4_archivals` 留在库里，确认无事后再手工 `DROP`
- **v5 → v6**：只加列（`use_count` / `last_used_at`）+ 一条索引，不搬数据、不重建
- v4 迁移会补上一直没填的 `superseded_by` 链
- 迁移前的整库快照在 `data/backup/`（sqlite backup API，WAL 已合并）
- **迁移后要重启 DSH**：MCP server 是常驻进程，旧的连接不会自己换表

回滚：停 DSH → 用 `data/backup/memory-v4-*.db` 覆盖 `data/memory.db`（连同 `-wal`/`-shm` 一起删）
→ 把 `src/` 恢复到对应版本 → 重启。

---

## 回滚（整体）

1. **只停记忆库**：把 `cordis.patch.yml` 里的 `dsh-rag` 段删掉，重启 DSH。
   原文件备份在同目录 `cordis.patch.yml.bak-20261101-1245`。
2. **停归档守护**：
   `powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall`
3. 记忆数据不动，随时可重新接回。

---

## 已知限制

- **中文检索用 bigram**：对 1–2 字的词召回偏松，靠 `project` + `kind` 过滤收敛。
- **归档摘要不调 LLM（除非 DSH 压缩过）**：规则抽取语义压缩有限；有 `compact-checkpoint` 时直接用它，质量最好。
- **项目归属是推断**：会话没提到项目名时会落到「工作区」。
- **pack 的缓存收益有条件**：基线不变才能跨会话命中；交接卡每写一次，卡之后的内容就要重算。
  预算不够时按 `next → facts → card` 收缩：先丢「下一步」，再从**最旧**的事实开始挤，
  「当前状态」最后才动；事实被裁剪时 header 会注明「更早的被省略，用 recall 查」。
- **单机单用户**：SQLite WAL，没有并发写优化，不适合多进程高频写。
- **向量化依赖网络**：智谱是 HTTP API，断网时向量检索降级为纯关键词（不报错，只是召回变差）。想彻底离线切 `DSH_RAG_EMBED=local`。
- **文本会出本机**：走智谱意味着记忆正文会发给智谱。介意的话用 local 后端。
- **跨会话首步命中不总是满的**：实测多数新会话能命中约 11 264 token（系统提示 + 工具表），
  但个别会话只有 512。原因未定位（请求头逐字节相同），`cache_stats` 会把它暴露出来。
- **开场自动注入依赖 DSH 内部 API**（`agent/created` / `session/event` / `agent.inject`）。
  这些不是公开契约，DSH 升级可能改；所以整条链路都是 try/catch + 默认失败即静默，
  排查看 `logs/auto-inject.log`。关了它 RAG 依然能用，只是回到"靠模型自觉"。
- **草稿质量取决于会话本身**：自动草稿只写"有证据"的字段（改过哪些文件、最后一句请求），
  它不会替你想清楚下一步；下一步仍然需要人或模型补。
- **接力度量需要样本**：要比较"读过 pack / 没读过"，得两种会话都攒够几个；新装时对照为空是正常的。
- **清理只认时效和结构位置**：`use_count=0` 只提醒不执行——"没人检索"不等于"没价值"。
  真正的取舍仍要人来定。

---

## 开发与测试

全部自测都用临时库 + 临时目录，不会碰 `data/memory.db`：

```powershell
# 单元 / 集成
.\.venv\Scripts\python.exe scripts\selftest_pack.py
.\.venv\Scripts\python.exe scripts\selftest_v6.py
.\.venv\Scripts\python.exe scripts\selftest_store.py
.\.venv\Scripts\python.exe scripts\selftest_archive.py

# MCP 端到端（起一个 server 子进程，走完整协议）
.\.venv\Scripts\python.exe scripts\selftest_mcp.py

# 插件静态自测（mock 宿主，不需要开 DSH）
node scripts\selftest_plugin.mjs
```

自测脚本在 import `store` 之前会设 `DSH_RAG_PROJECTS_ROOT` / `DSH_RAG_HANDOFFS_ROOT` /
`DSH_RAG_EMBED=off`——只隔离 `DSH_RAG_DB` 是不够的，`put_handoff` 会写磁盘。

改完 `src/*.py` 之后**必须重启 DSH**：MCP server 是常驻进程，盘上的新代码它不会自己加载。
CLI 和脚本没有这个限制。

## License

MIT，见 [LICENSE](LICENSE)。

## 贡献

欢迎 issue / PR。有两点约定：

1. **改库只走 `Store`**，不要在别处直接 `conn.execute`（页面层例外，它有独立事务）。
2. **自测必须临时库**；涉及文件系统副作用的，把三个路径变量都指到临时目录。

