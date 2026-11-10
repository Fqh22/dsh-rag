# 安装与接入


需要 Python 3.10+（用系统 Python，或 DSH 自带的运行时都行）。

```powershell
git clone https://github.com/Fqh22/dsh-rag.git dsh-rag
cd dsh-rag
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

> 国内网络建议用清华镜像；用官方源把 `-i` 去掉即可。

装完依赖后跑 `powershell -ExecutionPolicy Bypass -File scripts\install.ps1`，
一条命令完成 MCP 配置 + 面板插件（见上面的「快速开始」）。下面两节是它做的细节，想手动来可以对照。

**环境变量**（都有合理默认值，通常只需要第一个）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `DSH_RAG_PROJECTS_ROOT` | 被记忆的项目放在哪个目录（一个子目录 = 一个项目） | `~/Documents/Projects` |
| `DSH_RAG_DATA` | 运行期数据目录（库、备份、状态文件） | `data/` |
| `DSH_RAG_DB` | 数据库路径（覆盖 `DSH_RAG_DATA` 里的默认值） | `data/memory.db` |
| `DSH_RAG_HANDOFFS_ROOT` | 没有项目目录时，交接卡 md 的导出位置 | `data/handoffs` |
| `DSH_RAG_EMBED` | 向量后端：`zhipu` / `local` / `off` | `zhipu` |
| `DSH_RAG_SCAN_SECONDS` | 归档守护的扫描间隔（秒） | `300` |
| `DSH_RAG_ROOT` | 面板插件去哪里找 Python 仓库 | 自动探测：插件上级 → `~/Documents/Projects/dsh-rag` → `~/dsh-rag` |
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

> 跑过 `scripts/install.ps1` 的话，这一节可以跳过——它做的就是这个（默认走方式 B 的链接，改插件代码刷新即生效）。

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
