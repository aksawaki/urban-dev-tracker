"""
既存 DB の汚染タイトルを一括修正するワンショットスクリプト
- kensetsunews 等の「最終更新 | yyyy/mm/dd ...」末尾ゴミを除去
"""
import json
import re

DB_FILE = "data/processed/articles.json"

_TITLE_TRAILING_JUNK_RE = re.compile(
    r"\s+(?:最終更新|更新日時)\s*[|｜]\s*\d{4}.*",
    re.DOTALL,
)

with open(DB_FILE, encoding="utf-8") as f:
    db = json.load(f)

updated = 0
for aid, a in db.items():
    changed = False

    old_title = a.get("title", "")
    new_title = _TITLE_TRAILING_JUNK_RE.sub("", old_title).strip()
    if new_title != old_title:
        a["title"] = new_title
        changed = True

    old_summary = a.get("summary", "")
    if "最終更新" in old_summary:
        new_summary = _TITLE_TRAILING_JUNK_RE.sub("", old_summary).strip()
        a["summary"] = new_summary

    if changed:
        print(f"FIXED: {repr(new_title[:80])}")
        updated += 1

with open(DB_FILE, "w", encoding="utf-8") as f:
    json.dump(db, f, ensure_ascii=False, indent=2)

print(f"\n合計 {updated} 件のタイトルを修正しました")
