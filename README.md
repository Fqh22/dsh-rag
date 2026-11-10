# dsh-rag — DSH 跨会话项目记忆库

[![ci](https://github.com/Fqh22/dsh-rag/actions/workflows/ci.yml/badge.svg)](https://github.com/Fqh22/dsh-rag/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/dsh-rag-panel)](https://www.npmjs.com/package/dsh-rag-panel)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

给 DeepSeek Harness 加一层**按项目分类的长期记忆**：会话变长归档、新开会话时，
不用再从头交接一遍。

形态：本地 MCP server（stdio / HTTP）+ 会话归档守护 + 面板插件。**数据全部留在本机**；
只有向量化默认要联网（智谱 embedding），可切本地模型或完全关闭。

---

## 快速开始（5 分钟）

```powershell
git clone https://github.com/Fqh22/dsh-rag.git dsh-rag
cd dsh-rag
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

Linux / macOS 把最后一行换成：

```bash
bash scripts/install.sh
```

脚本做三件事：把 MCP server 写进当前 profile 的 `cordis.patch.yml`（`- insert:` 包裹这个坑
已经替你处理）、把面板插件挂进 `package.json`、用 DSH 自带的 pnpm 装好。

- **幂等**：重复跑不会重复添加；改动前自动备份成 `*.bak-<时间戳>`
- `-DryRun` / `--dry-run` 只看计划不动文件；`-Uninstall` / `--uninstall` 撤销
- `-SkipPanel` 只配 MCP；`-Profile web` 配 Web 端

然后**重启 DSH**，验证两件事：

1. 工具表里出现 `mcp__rag__*`
2. 界面右下角出现记忆面板按钮

> 手动安装、HTTP 模式、插件市场、面板用法 → [docs/install.md](docs/install.md)

---

## 它解决什么

| 痛点 | 这里的对策 |
|---|---|
| 会话太长被归档，新会话什么都不记得 | 每项目一张**交接卡**（由结构化状态渲染），开场一条 `handoff_pack` 读完 |
| 新会话拿不回"上次干到哪"的现场 | `checkpoint_put/get` 存结构化状态；`handoff_pack` 末尾给续接建议（能恢复会话就别重开） |
| 每次开场都重算前缀，缓存命中上不去 | pack 按**稳定→易变**分层：基线在前、事实只在末尾追加，跨会话能复用前缀 |
| 长会话被 DSH 压缩后，最贵的那段上下文丢了 | 归档直接把 `compact-checkpoint` 的摘要整段收进记忆库 |
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

## 数据结构一览

六张表，每张只做一件事（完整说明见 [docs/design.md](docs/design.md)）：

| 表 | 一行是什么 |
|---|---|
| `projects` | 项目登记簿（目录是权威，表只是记录） |
| `memories` | **唯一的事实表**：一条记忆，`status` 管生命周期 |
| `sessions` | 一条 DSH 会话（压缩次数、对应记忆、接力度量） |
| `vectors` | 向量，按 embedding 模型隔离 |
| `memories_fts` | 全文索引（中文 bigram） |
| `pages` / `chunks` | 长文档的目录层与切块（可选，独立子系统） |

`status` 四个值：`active`（生效）、`superseded`（被取代）、`draft`（待确认，不进检索）、
`archived`（可恢复）。**删除永远不连带删记忆**——目录没了只标孤儿。

---

## 文档

| 想知道 | 去哪 |
|---|---|
| 怎么装、怎么接进 DSH、面板怎么用、HTTP 模式 | [docs/install.md](docs/install.md) |
| MCP 工具清单、长文档怎么灌、变更记录怎么进库 | [docs/tools.md](docs/tools.md) |
| 日常工作流、清理策略、运维、升级与回滚 | [docs/ops.md](docs/ops.md) |
| 表结构、目录结构、设计取舍 | [docs/design.md](docs/design.md) |
| 上下文包格式（给其它实现看） | [docs/pack-format.md](docs/pack-format.md) |
| 跑测试、贡献约定 | [docs/dev.md](docs/dev.md) |
| 一次完整代码审视的记录（问题 + 修复 + 验证） | [docs/审查-2026-11-06.md](docs/审查-2026-11-06.md) |

---

## 已知限制

- **中文检索用 bigram**：对 1–2 字的词召回偏松，靠 `project` + `kind` 过滤收敛。
- **归档摘要不调 LLM（除非 DSH 压缩过）**：规则抽取语义压缩有限；有 `compact-checkpoint` 时直接用它，质量最好。
- **项目归属是推断**：会话没提到项目名时会落到「工作区」。
- **pack 的缓存收益有条件**：基线不变才能跨会话命中；交接卡每写一次，卡之后的内容就要重算。
  预算不够时按 `next → facts → card` 收缩：先丢「下一步」，再从**最旧**的事实开始挤，
  「当前状态」最后才动。
- **单机单用户**：SQLite WAL，没有并发写优化，不适合多进程高频写。
- **向量化依赖网络**：智谱是 HTTP API，断网时向量检索降级为纯关键词（不报错，只是召回变差）。
  想彻底离线切 `DSH_RAG_EMBED=local` 或 `off`。
- **文本会出本机**：走智谱意味着记忆正文会发给智谱。介意的话用 local 后端。
- **跨会话首步命中不总是满的**：实测多数新会话能命中约 11 264 token（系统提示 + 工具表），
  但个别会话只有 512。原因未定位，`cache_stats` 会把它暴露出来。
- **开场自动注入依赖 DSH 内部 API**（`agent/created` / `session/event` / `agent.inject`）。
  这些不是公开契约，DSH 升级可能改；整条链路都是 try/catch + 失败即静默，
  排查看 `logs/auto-inject.log`。
- **草稿质量取决于会话本身**：自动草稿只写"有证据"的字段，它不会替你想清楚下一步。
- **接力度量需要样本**：要比较"读过 pack / 没读过"，得两种会话都攒够几个。
- **清理只认时效和结构位置**：`use_count=0` 只提醒不执行——"没人检索"不等于"没价值"。

---

## License

MIT，见 [LICENSE](LICENSE)。
