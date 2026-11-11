#!/usr/bin/env bash
# dsh-rag 归档守护的自启安装（Linux systemd / macOS launchd）
#
# 为什么需要它：归档守护（watch_archive.py）是常驻进程。不装自启也能工作——
# MCP server 每次启动会补一次归档，只是延迟到下次开 DSH。装自启才不会漏。
#
# 用法：
#   bash scripts/install-autostart.sh
#   bash scripts/install-autostart.sh --uninstall
#   bash scripts/install-autostart.sh --status
#
# Windows 用 scripts/install-autostart.ps1。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$ROOT/.venv/bin/python"
WATCHER="$ROOT/scripts/watch_archive.py"
LOG_DIR="$ROOT/logs"

say()  { printf '\033[36m· %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

ACTION="install"
[ "${1:-}" = "--uninstall" ] && ACTION="uninstall"
[ "${1:-}" = "--status" ] && ACTION="status"

OS="$(uname -s)"
mkdir -p "$LOG_DIR"

[ -x "$PY" ] || warn "没找到 $PY，服务会起不来（先建 .venv 并装依赖）"

# ---------------------------------------------------------------- Linux systemd
if [ "$OS" = "Linux" ]; then
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT="$UNIT_DIR/dsh-rag-archive.service"
  case "$ACTION" in
    install)
      command -v systemctl >/dev/null || die "没有 systemctl，这台机器不支持 systemd user unit"
      mkdir -p "$UNIT_DIR"
      cat > "$UNIT" <<EOF
[Unit]
Description=dsh-rag session archive watcher
After=default.target

[Service]
Type=simple
WorkingDirectory=$ROOT
ExecStart=$PY $WATCHER
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
EOF
      systemctl --user daemon-reload
      systemctl --user enable --now dsh-rag-archive.service
      say "已启用 systemd user service: dsh-rag-archive.service"
      warn "要让它在你没登录时也跑，执行一次：loginctl enable-linger $USER"
      say "看状态：systemctl --user status dsh-rag-archive；日志：journalctl --user -u dsh-rag-archive -f"
      ;;
    uninstall)
      if [ -f "$UNIT" ]; then
        systemctl --user disable --now dsh-rag-archive.service || true
        rm -f "$UNIT"
        systemctl --user daemon-reload
        say "已移除 dsh-rag-archive.service"
      else
        say "未安装，无需移除"
      fi
      ;;
    status)
      systemctl --user status dsh-rag-archive.service --no-pager || true
      ;;
  esac
  exit 0
fi

# ---------------------------------------------------------------- macOS launchd
if [ "$OS" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.dsh.rag.archive.plist"
  case "$ACTION" in
    install)
      mkdir -p "$HOME/Library/LaunchAgents"
      cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.dsh.rag.archive</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$WATCHER</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchd.err.log</string>
</dict>
</plist>
EOF
      launchctl unload "$PLIST" 2>/dev/null || true
      launchctl load -w "$PLIST"
      say "已加载 launchd: com.dsh.rag.archive"
      say "日志：$LOG_DIR/launchd.err.log"
      ;;
    uninstall)
      if [ -f "$PLIST" ]; then
        launchctl unload -w "$PLIST" 2>/dev/null || true
        rm -f "$PLIST"
        say "已移除 launchd 任务"
      else
        say "未安装，无需移除"
      fi
      ;;
    status)
      launchctl list | grep -F "com.dsh.rag.archive" || say "未运行"
      ;;
  esac
  exit 0
fi

die "不认识的操作系统：$OS（Windows 用 install-autostart.ps1）"
