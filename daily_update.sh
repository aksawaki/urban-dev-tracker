#!/bin/bash
# daily_update.sh - 毎朝の情報収集 & HTML更新
#
# 動作:
#   - 毎朝9時に launchd から自動実行
#   - クローリング → HTML再生成 → GitHub Pagesデプロイ
#   - ChatWork配信は手動（send_chatwork.sh）で行う
#   - 手動実行: bash daily_update.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"
LOG="$SCRIPT_DIR/daily_update.log"
MAX_ARTICLES=10

# ── ログ開始 ──────────────────────────────────────────────────
echo "" >> "$LOG"
echo "========================================" >> "$LOG"
echo "$(date '+%Y-%m-%d %H:%M:%S') 日次更新開始" >> "$LOG"
echo "========================================" >> "$LOG"

cd "$SCRIPT_DIR"

# 1. 個別記事クロール
echo "$(date '+%H:%M:%S') [1/3] クロール開始 (max=$MAX_ARTICLES)" >> "$LOG"
if "$PYTHON" main.py crawl --max "$MAX_ARTICLES" --export >> "$LOG" 2>&1; then
    echo "$(date '+%H:%M:%S') [1/3] クロール完了" >> "$LOG"
else
    echo "$(date '+%H:%M:%S') [1/3] クロールエラー (続行)" >> "$LOG"
fi

# 2. HTML を最新状態に再生成
echo "$(date '+%H:%M:%S') [2/3] HTML再生成" >> "$LOG"
if "$PYTHON" - << 'PYEOF' >> "$LOG" 2>&1
import json, sys, re
sys.path.insert(0, '.')
import viewer
with open('data/processed/articles.json') as f:
    raw = json.load(f)
articles = list(raw.values())
html = viewer.generate_area_timeline_html(articles)
out = 'reports/timeline_latest.html'
with open(out, 'w') as f:
    f.write(html)
cards = len(re.findall(r'class="plan-card"', html))
areas = len(re.findall(r'id="ab-', html))
print(f"HTML生成完了: {cards}件 / {areas}エリア → {out}")
PYEOF
then
    echo "$(date '+%H:%M:%S') [2/3] HTML生成完了" >> "$LOG"
else
    echo "$(date '+%H:%M:%S') [2/3] HTML生成エラー" >> "$LOG"
fi

# 3. GitHub Pages にデプロイ
echo "$(date '+%H:%M:%S') [3/3] GitHub Pages デプロイ" >> "$LOG"
cp "$SCRIPT_DIR/reports/timeline_latest.html" "$SCRIPT_DIR/docs/index.html"
cd "$SCRIPT_DIR"
git add docs/index.html
git commit -m "chore: update timeline $(date '+%Y-%m-%d')" >> "$LOG" 2>&1
git push origin main >> "$LOG" 2>&1
echo "$(date '+%H:%M:%S') [3/3] デプロイ完了" >> "$LOG"

echo "$(date '+%Y-%m-%d %H:%M:%S') 日次更新終了（ChatWork配信は send_chatwork.sh で手動実行）" >> "$LOG"
