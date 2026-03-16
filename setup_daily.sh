#!/bin/bash
# setup_daily.sh - 毎朝10時の自動更新を macOS launchd に登録
#
# インストール: bash setup_daily.sh
# 解 除:       bash setup_daily.sh --uninstall
# 手動実行:    bash daily_update.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.urbandevtracker.daily"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"

install() {
  mkdir -p "$HOME/Library/LaunchAgents"

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
    <string>/bin/bash</string>
    <string>${SCRIPT_DIR}/daily_update.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${SCRIPT_DIR}</string>

  <!-- 毎朝10時に実行（スリープ中だった場合は起動後すぐに実行） -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>10</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>${SCRIPT_DIR}/daily_update.log</string>

  <key>StandardErrorPath</key>
  <string>${SCRIPT_DIR}/daily_update.log</string>
</dict>
</plist>
EOF

  launchctl unload "$PLIST_PATH" 2>/dev/null || true
  launchctl load "$PLIST_PATH"

  echo "✅ 自動更新を登録しました"
  echo ""
  echo "   動作: 毎朝10:00 に情報収集 → HTML更新 → ChatWork投稿"
  echo ""
  echo "   設定ファイル : $PLIST_PATH"
  echo "   更新ログ     : $SCRIPT_DIR/daily_update.log"
  echo "   HTML出力     : $SCRIPT_DIR/reports/timeline_latest.html"
  echo ""
  echo "手動実行: bash $SCRIPT_DIR/daily_update.sh"
  echo "ログ確認: tail -f $SCRIPT_DIR/daily_update.log"
  echo "停 止:    bash $SCRIPT_DIR/setup_daily.sh --uninstall"
}

uninstall() {
  if [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm "$PLIST_PATH"
    echo "✅ 自動実行を停止・削除しました"
  else
    echo "設定ファイルが見つかりません: $PLIST_PATH"
  fi
}

if [ "$1" = "--uninstall" ]; then
  uninstall
else
  install
fi
