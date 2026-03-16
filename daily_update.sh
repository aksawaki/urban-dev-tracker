#!/bin/bash
# daily_update.sh - 毎朝の情報収集 & ChatWork 投稿
#
# 動作:
#   - 毎朝10時に launchd から自動実行
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

# 3. 新着記事を ChatWork に投稿
echo "$(date '+%H:%M:%S') [3/3] ChatWork投稿" >> "$LOG"
"$PYTHON" - << 'PYEOF' >> "$LOG" 2>&1
import json, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '.')

STAMP_FILE = Path('.last_notified_at')

# 前回通知時刻を読み込む（なければ epoch）
if STAMP_FILE.exists():
    try:
        last_ts = datetime.fromisoformat(STAMP_FILE.read_text().strip())
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
    except ValueError:
        last_ts = datetime.fromtimestamp(0, tz=timezone.utc)
else:
    last_ts = datetime.fromtimestamp(0, tz=timezone.utc)

# 全記事を読み込み、HTMLと同じフィルタ（重複統合・完了済み除外）を適用
with open('data/processed/articles.json') as f:
    raw = json.load(f)
import viewer
active = viewer.get_active_articles(list(raw.values()))

# fetched_at が前回通知より新しいものだけ抽出
new_articles = []
for a in active:
    ft = a.get('fetched_at', '')
    if not ft:
        continue
    try:
        art_ts = datetime.fromisoformat(ft)
        if art_ts.tzinfo is None:
            art_ts = art_ts.replace(tzinfo=timezone.utc)
    except ValueError:
        continue
    if art_ts > last_ts:
        new_articles.append(a)

if not new_articles:
    print(f"ChatWork: 新着なし（前回通知: {last_ts.strftime('%Y-%m-%d %H:%M')} UTC）")
else:
    import yaml
    with open('config.yaml') as f:
        config = yaml.safe_load(f)
    from notifier import ChatWorkNotifier
    notifier = ChatWorkNotifier.from_config(config)
    if notifier is None:
        print("ChatWork: 設定なし、スキップ")
    else:
        ok = notifier.send(new_articles, datetime.now().strftime('%Y年%m月%d日'))
        if ok:
            STAMP_FILE.write_text(max(a.get('fetched_at','') for a in new_articles))
            print(f"ChatWork: {len(new_articles)}件 投稿完了")
        else:
            print("ChatWork: 投稿エラー")
PYEOF
echo "$(date '+%H:%M:%S') [3/3] ChatWork投稿完了" >> "$LOG"

echo "$(date '+%Y-%m-%d %H:%M:%S') 日次更新終了" >> "$LOG"
