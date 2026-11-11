# 日常运维


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

## 回滚（整体）

1. **只停记忆库**：把 `cordis.patch.yml` 里的 `dsh-rag` 段删掉，重启 DSH。
   原文件备份在同目录 `cordis.patch.yml.bak-20261101-1245`。
2. **停归档守护**：
   `powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall`
3. 记忆数据不动，随时可重新接回。
