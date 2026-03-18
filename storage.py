"""
storage.py - データ永続化

記事を JSON ファイルに保存。
重複排除（article.id で管理）、古いデータの自動削除に対応。
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scraper import Article

logger = logging.getLogger(__name__)

DB_FILE = "data/processed/articles.json"
RAW_DIR = "data/raw"

# kenbiya 記事番号のしきい値（これ未満 ≈ 2025年10月以前の古い記事）
_KENBIYA_MIN_ARTICLE_NUM = 9600

_PUB_DATE_RE = re.compile(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日')


def _parse_pub_date_str(s: str) -> str | None:
    """公開日文字列を 'YYYY-MM-DD' に正規化。解析不能なら None。"""
    if not s:
        return None
    m = _PUB_DATE_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if re.match(r'\d{4}-\d{2}-\d{2}', s):
        return s[:10]
    return None


def _is_too_old(article: Article) -> bool:
    """クロール時点で「本日より前の公開日」の記事かどうかを判定する。

    - published_at が判明していて本日より前 → True（除外）
    - kenbiya URL で記事番号 < _KENBIYA_MIN_ARTICLE_NUM → True（除外）
    - published_at が不明かつ非kenbiya → False（除外しない）
    """
    today = datetime.now().strftime('%Y-%m-%d')

    # 公開日が判明している場合: 本日より前なら除外
    pub = _parse_pub_date_str(article.published_at or "")
    if pub is not None and pub < today:
        return True

    # kenbiya 記事番号チェック（published_at が None でも古い記事を除外）
    url = article.url or ""
    if 'kenbiya.com' in url:
        m = re.search(r'/(\d+)(?:\.html|/?$)', url)
        if m and int(m.group(1)) < _KENBIYA_MIN_ARTICLE_NUM:
            return True

    return False


def _ensure_dirs():
    Path("data/processed").mkdir(parents=True, exist_ok=True)
    Path(RAW_DIR).mkdir(parents=True, exist_ok=True)


def load_db() -> dict[str, dict]:
    """既存のDBを読み込む。{article_id: article_dict}"""
    _ensure_dirs()
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_db(db: dict[str, dict]):
    _ensure_dirs()
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def upsert_articles(articles: list[Article]) -> tuple[int, int]:
    """記事をDBに追加/更新。(新規件数, スキップ件数) を返す"""
    db = load_db()
    new_count = 0
    skip_count = 0

    for article in articles:
        if article.id in db:
            existing = db[article.id]
            # published_at が未取得だった場合のみ更新（再クロールで日付を補完）
            if not existing.get("published_at") and article.published_at:
                existing["published_at"] = article.published_at
                db[article.id] = existing
                new_count += 1  # 更新も新規としてカウント
            else:
                skip_count += 1
            continue

        # 新規記事: 本日より前の公開日ならDBに保存しない
        if _is_too_old(article):
            logger.debug(f"スキップ（古い公開日）: {article.url}")
            skip_count += 1
            continue

        db[article.id] = _to_dict(article)
        new_count += 1

    save_db(db)
    logger.info(f"保存: 新規/更新 {new_count} 件, スキップ {skip_count} 件")
    return new_count, skip_count


def purge_old(retention_days: int):
    """retention_days より古いレコードを削除"""
    db = load_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    before = len(db)
    db = {
        aid: a for aid, a in db.items()
        if _parse_dt(a.get("fetched_at", "")) >= cutoff
    }
    after = len(db)
    if before != after:
        save_db(db)
        logger.info(f"古いデータを削除: {before - after} 件")


def save_raw(articles: list[Article], label: str = ""):
    """生データをタイムスタンプ付きJSONで保存"""
    _ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{RAW_DIR}/{ts}_{label or 'fetch'}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump([_to_dict(a) for a in articles], f, ensure_ascii=False, indent=2)
    logger.info(f"生データ保存: {fname}")


def get_recent(days: int = 7, priority: str = None) -> list[dict]:
    """直近 N 日のレコードを返す。priority でフィルタ可。"""
    db = load_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    results = [
        a for a in db.values()
        if _parse_dt(a.get("fetched_at", "")) >= cutoff
        and (priority is None or a.get("priority") == priority)
    ]
    return sorted(results, key=lambda a: a.get("fetched_at", ""), reverse=True)


def _to_dict(article: Article) -> dict:
    return {
        "id": article.id,
        "source_id": article.source_id,
        "source_name": article.source_name,
        "title": article.title,
        "url": article.url,
        "area": article.area,
        "tags": article.tags,
        "published_at": article.published_at,
        "fetched_at": article.fetched_at,
        "summary": article.summary,
        "content": getattr(article, "content", ""),
        "content_hash": article.content_hash,
        "priority": article.priority,
    }


def _parse_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)
