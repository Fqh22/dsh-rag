# dsh-rag-panel

DSH 面板插件：浏览 / 检索 / 编辑 [dsh-rag](https://github.com/Fqh22/dsh-rag) 的项目记忆库，
并在会话第一句后注入一条「去读 handoff_pack」的指针。

**这个包只是面板和自动注入。真正的记忆库（MCP server + SQLite）在 Python 侧**，
必须另外 clone 主仓库并装依赖 —— 见 [主仓库 README](https://github.com/Fqh22/dsh-rag#readme) 的「接入 DSH」。

## 完整安装（推荐）

这个包只是面板，记忆库本体在同仓库的 Python 侧。最短路径是**先 clone 主仓库，跑它的安装脚本**：

```powershell
git clone https://github.com/Fqh22/dsh-rag.git dsh-rag
cd dsh-rag
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

脚本会把 MCP 配置和本插件一起装好（默认用本地链接，改 `client.js` 刷新即生效），
改动前自动备份，`-DryRun` 可先看计划。

## 只装面板（MCP 已经配好）

```powershell
# 在 DSH 的 profile 目录执行（如 %USERPROFILE%\.dsh\profiles\desktop）
pnpm add dsh-rag-panel
```

然后把 `dsh-rag-panel` 加进该 profile `package.json` 的 `dsh.profile.bundles`，重启 DSH。
如果面板报「找不到 dsh-rag 仓库」，说明插件没找到 Python 侧——按下一节设 `DSH_RAG_ROOT`。

插件需要知道 Python 仓库在哪，按顺序找：

1. 环境变量 `DSH_RAG_ROOT`（指向 clone 的 dsh-rag 目录，里面应有 `src/cli.py`）
2. 插件上一级目录（本地 clone 时自然成立）
3. `~/Documents/Projects/dsh-rag`、`~/dsh-rag`

都找不到时，面板接口会返回一条明确的错误提示，而不是静默空白。

Python 解释器同理：`DSH_RAG_PYTHON` → 项目 `.venv`（Windows/POSIX 两种布局）→ PATH 里的 `python`。

## 关闭自动注入

设 `DSH_RAG_AUTO_INJECT=off`。面板本身仍然可用。

## License

MIT
