#!/bin/bash
# daily_update.sh - 毎朝9時の自動実行スクリプト
#
# 動作:
#   1. クロール（前日9時以降の新規記事を収集）
#   2. GitHub Pages にデプロイ（2025-11-01 以降全件）
#   3. ChatWork に本日分を通知
#
# 自動実行: cron "0 9 * * * /Users/sawakiayaka/urban-dev-tracker/daily_update.sh"
# 手動実行: bash /Users/sawakiayaka/urban-dev-tracker/daily_update.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOG="$SCRIPT_DIR/daily_update.log"

echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') 日次更新開始" >> "$LOG"
echo "========================================" >> "$LOG"

cd "$SCRIPT_DIR"

# 1. クロール
echo "$(date '+%H:%M:%S') [1/3] クロール開始" >> "$LOG"
if "$PYTHON" main.py crawl >> "$LOG" 2>&1; then
    echo "$(date '+%H:%M:%S') [1/3] クロール完了" >> "$LOG"
else
    echo "$(date '+%H:%M:%S') [1/3] クロールエラー（続行）" >> "$LOG"
fi

# 2. GitHub Pages デプロイ（2025-11-01 以降全件）
echo "$(date '+%H:%M:%S') [2/3] デプロイ開始" >> "$LOG"
if "$PYTHON" main.py deploy >> "$LOG" 2>&1; then
    echo "$(date '+%H:%M:%S') [2/3] デプロイ完了" >> "$LOG"
else
    echo "$(date '+%H:%M:%S') [2/3] デプロイエラー（続行）" >> "$LOG"
fi

# 3. ChatWork 通知（本日分のみ）
echo "$(date '+%H:%M:%S') [3/3] ChatWork 通知開始" >> "$LOG"
if "$PYTHON" main.py notify >> "$LOG" 2>&1; then
    echo "$(date '+%H:%M:%S') [3/3] ChatWork 通知完了" >> "$LOG"
else
    echo "$(date '+%H:%M:%S') [3/3] ChatWork 通知エラー" >> "$LOG"
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') 日次更新終了" >> "$LOG"
