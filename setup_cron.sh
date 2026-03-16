#!/bin/bash
# setup_cron.sh - launchd (macOS) による定期実行設定
#
# 使い方: bash setup_cron.sh
# 解除:   bash setup_cron.sh --uninstall

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
LABEL="com.urbandevtracker.fetch"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$SCRIPT_DIR"
INTERVAL=21600  # 6時間（秒）

install() {
  cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${SCRIPT_DIR}/main.py</string>
    <string>fetch</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${SCRIPT_DIR}</string>

  <key>StartInterval</key>
  <integer>${INTERVAL}</integer>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/cron_stdout.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/cron_stderr.log</string>
</dict>
</plist>
EOF

  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  launchctl load "$PLIST_PATH"
  echo "✅ 定期実行を登録しました（6時間ごと）"
  echo "   設定ファイル: $PLIST_PATH"
  echo ""
  echo "確認コマンド: launchctl list | grep urbandevtracker"
  echo "ログ確認:     tail -f $LOG_DIR/urban-dev-tracker.log"
  echo "停止:         bash setup_cron.sh --uninstall"
}

uninstall() {
  if [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm "$PLIST_PATH"
    echo "✅ 定期実行を停止・削除しました"
  else
    echo "設定ファイルが見つかりません: $PLIST_PATH"
  fi
}

if [ "$1" = "--uninstall" ]; then
  uninstall
else
  install
fi
