#!/bin/bash
# send_chatwork.sh - 本日分をChatWorkに手動配信
#
# 使い方: bash send_chatwork.sh
#   → 本日フェッチ済み・精査済みの記事をChatWorkに投稿する

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"

cd "$SCRIPT_DIR"

"$PYTHON" - << 'PYEOF'
import json, yaml, sys
from datetime import datetime
sys.path.insert(0, '.')

from viewer import _is_display_worthy
from notifier import ChatWorkNotifier, is_development_relevant

today = datetime.now().strftime('%Y-%m-%d')

with open('config.yaml') as f:
    config = yaml.safe_load(f)

with open('data/processed/articles.json') as f:
    raw = json.load(f)

# 本日フェッチ・表示対象・通知対象（30日以内の記事のみ）
articles = [
    a for a in raw.values()
    if (a.get('fetched_at') or '')[:10] == today
    and _is_display_worthy(a)
    and is_development_relevant(a)
]

if not articles:
    print(f"本日（{today}）の送信対象記事はありません。")
    sys.exit(0)

print(f"送信対象: {len(articles)}件")
for a in sorted(articles, key=lambda x: x.get('published_at') or '', reverse=True):
    pub = (a.get('published_at') or '日付不明')[:10]
    print(f"  {pub:10} [{a.get('area','?')}] {a.get('title','')[:52]}")

notifier = ChatWorkNotifier.from_config(config)
if notifier is None:
    print("\nエラー: ChatWork設定が見つかりません（config.yaml を確認）")
    sys.exit(1)

ok = notifier.send(articles, datetime.now().strftime('%Y年%m月%d日'))
print(f"\n{'✅ 送信完了' if ok else '❌ 送信失敗'} ({len(articles)}件)")
PYEOF
