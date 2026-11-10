#!/usr/bin/env bash
# dsh-rag 一键接入脚本（Linux / macOS）
#
# 与 scripts/install.ps1 等价：写 MCP 配置、挂面板插件、用 DSH 自带的 pnpm 安装。
# 全部幂等，改动前备份。
#
# 用法：
#   bash scripts/install.sh
#   bash scripts/install.sh --profile web
#   bash scripts/install.sh --dry-run
#   bash scripts/install.sh --skip-panel     # 只配 MCP
#   bash scripts/install.sh --uninstall
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PROFILE="${DSH_PROFILE:-desktop}"
PROFILES_ROOT="${DSH_PROFILES_ROOT:-$HOME/.dsh/profiles}"
SKIP_PANEL=0
DRY_RUN=0
UNINSTALL=0

usage() {
  cat <<'EOF'
dsh-rag 接入脚本（POSIX）

  --profile NAME       要配置的 DSH profile（默认 desktop）
  --profiles-root DIR  自定义 profiles 目录（默认 ~/.dsh/profiles）
  --skip-panel         只配 MCP，不装面板插件
  --dry-run            只显示计划，不改文件
  --uninstall          撤销写入的 MCP 段与插件配置
  -h, --help           显示帮助
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --profile)        PROFILE="${2:?--profile 需要参数}"; shift 2 ;;
    --profiles-root)  PROFILES_ROOT="${2:?--profiles-root 需要参数}"; shift 2 ;;
    --skip-panel)     SKIP_PANEL=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    --uninstall)      UNINSTALL=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage; exit 2 ;;
  esac
done

profile_dir="$PROFILES_ROOT/$PROFILE"
patch="$profile_dir/cordis.patch.yml"
pkg="$profile_dir/package.json"

say()  { printf '\033[36m· %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

backup() {
  [ -f "$1" ] || return 0
  local bak="$1.bak-$(date +%Y%m%d-%H%M%S)"
  cp "$1" "$bak"
  say "已备份 $(basename "$1") → $(basename "$bak")"
}

find_python() {
  if [ -x "$ROOT/.venv/bin/python" ]; then
    echo "$ROOT/.venv/bin/python"
  elif [ -x "$ROOT/.venv/bin/python3" ]; then
    echo "$ROOT/.venv/bin/python3"
  else
    command -v python3 || true
  fi
}

PY_BIN="$(find_python)"
if [ -z "$PY_BIN" ]; then
  warn "找不到 python3；--dry-run 仍可用，真正写入前需要先装 python3"
fi

echo
echo "dsh-rag 接入计划（$PROFILE profile）"
echo "  仓库：  $ROOT"
echo "  profile：$profile_dir"
echo "  Python：$PY_BIN"
if [ "$UNINSTALL" -eq 1 ]; then
  echo "  MCP：   移除"
  echo "  插件：  移除"
elif [ "$SKIP_PANEL" -eq 1 ]; then
  echo "  MCP：   写入 cordis.patch.yml"
  echo "  插件：  跳过"
else
  echo "  MCP：   写入 cordis.patch.yml"
  echo "  插件：  写入 package.json 并 pnpm install"
fi
echo

[ "$DRY_RUN" -eq 1 ] && { warn "DryRun：只显示计划，没有改动任何文件"; exit 0; }

[ -n "$PY_BIN" ] || die "找不到 python3（apt install python3 / brew install python）"
[ -d "$profile_dir" ] || die "找不到 profile 目录：$profile_dir（先用 DSH 打开一次，或确认 --profile）"

# ---------------------------------------------------------------- MCP 配置
write_mcp() {
  local py="$ROOT/.venv/bin/python"
  [ -x "$py" ] || py="$ROOT/.venv/bin/python3"
  [ -x "$py" ] || py="$PY_BIN"
  [ -n "$py" ] || { warn "找不到 Python，MCP 配置里的 command 会指向不存在的路径"; py="python3"; }
  {
    echo ""
    echo "# dsh-rag：本地项目记忆库（MCP stdio）。由 scripts/install.sh 写入；重复执行不会重复添加。"
    echo "# 回滚：bash scripts/install.sh --uninstall，或用同目录 *.bak-* 覆盖。"
    echo "- insert:"
    echo "    - id: dsh-rag"
    echo "      name: \"@deepseek-ai/dsh-mcp-client\""
    echo "      config:"
    echo "        serverName: rag"
    echo "        transport: stdio"
    echo "        command: '$py'"
    echo "        args: ['$ROOT/src/server.py']"
    echo "        cwd: '$ROOT/src'"
    echo "        env:"
    echo "          PYTHONIOENCODING: utf-8"
    echo "          PYTHONUTF8: '1'"
    echo "        toolCallTimeoutMs: 120000"
    echo "        failOnStartupError: false"
  } >> "$patch"
}

remove_mcp() {
  # 用临时文件而不是 stdin heredoc：某些 python 在 Windows/git-bash 下读 stdin 会失败，
  # 而标准做法在两边都成立。
  local script rc
  script="$(mktemp 2>/dev/null || echo "/tmp/dshrag-rm-$$.py")"
  cat > "$script" <<'PY'
import re, sys
p = sys.argv[1]
try:
    text = open(p, encoding='utf-8').read()
except OSError:
    sys.exit(0)
new = re.sub(r'(?ms)\n# dsh-rag：.*?failOnStartupError:\s*false\s*\n?', '\n', text)
if new == text:
    print('NOMATCH')
else:
    open(p, 'w', encoding='utf-8', newline='\n').write(new)
    print('OK')
PY
  "$PY_BIN" "$script" "$patch"
  rc=$?
  rm -f "$script"
  return $rc
}

if [ "$UNINSTALL" -eq 1 ]; then
  if grep -q 'id: dsh-rag' "$patch" 2>/dev/null; then
    backup "$patch"
    result="$(remove_mcp)"
    if [ "$result" = "OK" ]; then
      say "已从 cordis.patch.yml 移除 dsh-rag"
    else
      warn "没能精确定位写入段落，请手动删除 cordis.patch.yml 里的 dsh-rag 段"
    fi
  else
    say "cordis.patch.yml 里没有 dsh-rag，跳过"
  fi
elif [ -f "$patch" ] && grep -q 'id: dsh-rag' "$patch"; then
  say "cordis.patch.yml 里已有 dsh-rag，跳过（要更新路径请先 --uninstall）"
else
  [ -f "$patch" ] || { echo "[]" > "$patch"; }
  backup "$patch"
  write_mcp
  say "已写入 MCP 配置（- insert: 包裹，避免被当成覆盖行静默忽略）"
fi

# ---------------------------------------------------------------- 面板插件
update_pkg() {
  # $1 = add|remove。备份在 Python 里做（改动前那一瞬），bash 侧拿不到。
  local script rc
  script="$(mktemp 2>/dev/null || echo "/tmp/dshrag-pkg-$$.py")"
  cat > "$script" <<'PY'
import json, shutil, sys
from datetime import datetime

path, repo, mode = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(open(path, encoding='utf-8-sig'))
except (OSError, ValueError):
    print('NOFILE')
    sys.exit(0)

changed = False
deps = data.setdefault('dependencies', {})
profile = data.setdefault('dsh', {}).setdefault('profile', {})
bundles = profile.setdefault('bundles', [])

if mode == 'add':
    if 'dsh-rag-panel' not in deps:
        changed = True
    deps['dsh-rag-panel'] = f'link:{repo}'
    if 'dsh-rag-panel' not in bundles:
        bundles.append('dsh-rag-panel')
        changed = True
else:
    if 'dsh-rag-panel' in deps:
        del deps['dsh-rag-panel']
        changed = True
    if 'dsh-rag-panel' in bundles:
        profile['bundles'] = [b for b in bundles if b != 'dsh-rag-panel']
        changed = True

if changed:
    bak = f"{path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy(path, bak)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
print('CHANGED' if changed else 'SAME')
PY
  "$PY_BIN" "$script" "$pkg" "$ROOT/plugin" "$1"
  rc=$?
  rm -f "$script"
  return $rc
}

install_panel() {
  [ -f "$pkg" ] || { warn "profile 里没有 package.json，跳过插件"; return; }
  local r
  r="$(update_pkg add)"
  case "$r" in
    NOFILE) warn "package.json 读不出来，跳过插件"; return ;;
    SAME)   say "profile 里已配置 dsh-rag-panel，跳过" ;;
    CHANGED) say "已把 dsh-rag-panel 写入 profile package.json（已备份）" ;;
  esac

  local node pnpm
  node="$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node"
  pnpm="$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/pnpm/bin/pnpm.mjs"
  [ -x "$node" ] || node="$(command -v node || true)"
  if [ -n "$node" ] && [ -f "$pnpm" ]; then
    say "安装依赖（pnpm install，可能需要几十秒）…"
    ( cd "$profile_dir" && "$node" "$pnpm" install --silent ) \
      && say "依赖已安装" \
      || warn "pnpm install 失败，请手动在 $profile_dir 执行 pnpm install"
  else
    warn "没找到 DSH 自带的 node/pnpm，请手动在 $profile_dir 执行 pnpm install"
  fi
}

if [ "$UNINSTALL" -eq 1 ]; then
  if [ -f "$pkg" ]; then
    backup "$pkg"
    r="$(update_pkg remove)"
    [ "$r" = "CHANGED" ] && say "已从 profile package.json 移除 dsh-rag-panel" || say "package.json 里没有 dsh-rag-panel"
  fi
  echo
  say "卸载完成。重启 DSH 生效。"
  exit 0
fi

if [ "$SKIP_PANEL" -eq 0 ]; then
  install_panel
else
  say "按 --skip-panel 跳过插件"
fi

# ---------------------------------------------------------------- 自检
echo
say "自检"
if [ -x "$ROOT/.venv/bin/python" ]; then
  if "$ROOT/.venv/bin/python" "$ROOT/src/cli.py" state >/dev/null 2>&1; then
    say "Python 侧可运行（cli.py state 正常）"
  else
    warn "cli.py state 失败，检查 .venv 依赖是否装全"
  fi
else
  warn "没找到 .venv，先跑：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

echo
say "完成。重启 DSH 后生效。"
say "验证：工具表里出现 mcp__rag__*；界面上出现右下角记忆面板按钮。"
