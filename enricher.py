"""
enricher.py - 会員制記事の補足情報を無料ソースから自動取得

kensetsunews (建設通信新聞) 等の会員制記事はタイトル・URLのみ保存されるため、
同じキーワードで補足情報を取得する。

取得戦略:
  1. DB 内の他ソース記事でクロスリファレンス（即時・確実）
  2. Google News RSS でメディア情報を取得（タイトル・ソース名・リンク）
"""

import logging
import re
import time
from urllib.parse import quote_plus, urlparse

import requests

logger = logging.getLogger(__name__)

# 補足情報が必要なソースID プレフィックス
_PAYWALLED_PREFIXES = ("kensetsunews",)

# タイトルのノイズを除去するパターン
_NOISE_RE = re.compile(
    r'\s*最終更新\s*[|｜]\s*\d{4}/\d{2}/\d{2}.*$'
    r'|【[^】]*】'
    r'|\s*/\s*[^\s/]{1,15}$',
    re.MULTILINE,
)


def needs_enrichment(article: dict) -> bool:
    """補足情報の取得が必要かどうか判定する。"""
    sid = article.get("source_id") or ""
    if not any(sid.startswith(p) for p in _PAYWALLED_PREFIXES):
        return False
    if len(article.get("content") or "") > 50:
        return False
    if article.get("enrich_content"):
        return False
    return True


def _clean_title(title: str) -> str:
    """検索用にタイトルのノイズを除去して返す。"""
    t = _NOISE_RE.sub("", title).strip()
    parts = [p.strip() for p in re.split(r"[/／]", t) if p.strip()]
    return parts[0] if parts else t


_CONSTR_RE = re.compile(
    r'建設|工事|着工|竣工|施工|開発|整備|改修|建替|新築|建築|ゼネコン|再開発|事業'
)


def extract_keywords(title: str) -> str:
    """タイトルから検索クエリ文字列を生成する。"""
    cleaned = _clean_title(title)
    main = re.split(r"[、,]", cleaned)[0].strip()
    if len(main) < 8:
        main = cleaned
    query = main[:55]
    # 建設関連キーワードが含まれない場合は「建設」を補足して精度向上
    if not _CONSTR_RE.search(query):
        query += " 建設"
    return query


def _search_db(article: dict, db: dict) -> str | None:
    """DB 内の他ソース記事でクロスリファレンス検索する。

    同じプロジェクト名が含まれる他ソース（非kensetsunews）の記事を探し、
    そのコンテンツを返す。
    """
    title = article.get("title", "")
    # スラッシュ区切りのすべてのフレーズをキーワード候補に
    cleaned = _clean_title(title)
    parts = [p.strip() for p in re.split(r"[/／、,]", cleaned) if len(p.strip()) >= 6]
    if not parts:
        return None

    target_id = article.get("id", "")
    for aid, a in db.items():
        if aid == target_id:
            continue
        if (a.get("source_id") or "").startswith("kensetsunews"):
            continue
        a_title = a.get("title", "")
        a_content = (a.get("content") or a.get("summary") or "")
        a_text = a_title + " " + a_content
        # いずれかのフレーズが一致すれば関連記事とみなす
        if any(kw in a_text for kw in parts):
            content = (a.get("content") or a.get("summary") or "").strip()
            if content:
                src = a.get("url", "")
                logger.info(f"  → DB内で関連記事を発見: {a_title[:50]}")
                return content[:600], src

    return None


def _search_google_news_rss(query: str, session: requests.Session) -> list[dict]:
    """Google News RSS で検索し、記事メタデータリストを返す。

    Returns: [{"title": ..., "source": ..., "link": ..., "date": ...}]
    """
    import feedparser as _fp

    feed_url = (
        "https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=ja&gl=JP&ceid=JP:ja"
    )
    try:
        resp = session.get(feed_url, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Google News RSS 取得失敗: {e}")
        return []

    feed = _fp.parse(resp.content)
    results = []
    for entry in feed.entries[:5]:
        source_info = entry.get("source", {})
        source_name = source_info.get("title", "") if isinstance(source_info, dict) else ""
        raw_title = entry.get("title", "")
        # "タイトル - ソース名" 形式のタイトルからソース名を除去
        if source_name and raw_title.endswith(f" - {source_name}"):
            clean_title = raw_title[: -(len(source_name) + 3)].strip()
        else:
            clean_title = raw_title.strip()
        published = entry.get("published", "")[:16]
        results.append({
            "title": clean_title,
            "source": source_name,
            "link": entry.get("link", ""),
            "date": published,
        })

    return results


def enrich_article(article: dict, db: dict, session: requests.Session) -> dict | None:
    """1件の記事を補完する。

    戦略:
      1. DB 内の他ソース記事を検索
      2. Google News RSS でメディア情報を取得

    成功した場合は enrich_content / enrich_source を追加した新 dict を返す。
    失敗した場合は None を返す。
    """
    title = article.get("title", "")
    query = extract_keywords(title)
    if not query:
        return None

    logger.info(f"補足検索: {query!r}")

    # --- 戦略1: DB 内クロスリファレンス ---
    db_result = _search_db(article, db)
    if db_result:
        content, src = db_result
        return {
            **article,
            "enrich_content": content,
            "enrich_source": src,
        }

    # --- 戦略2: Google News RSS でメタデータ取得 ---
    news_items = _search_google_news_rss(query, session)
    # 建設関連キーワードを含む他ソースの記事のみ採用
    _RELEVANT_RE = re.compile(
        r'建設|工事|着工|竣工|施工|開発|整備|改修|建替|新築|建築|ゼネコン|再開発|'
        r'マンション.*(?:着工|建設|竣工|計画)|不動産|人事|機構改革|業務代行|入札|落札'
    )
    other_media = [
        n for n in news_items
        if "kensetsunews" not in n.get("source", "").lower()
        and n.get("source")
        and _RELEVANT_RE.search(n.get("title", ""))
    ]

    if other_media:
        # 関連報道一覧をテキストとして構築
        lines = ["【関連報道】"]
        for n in other_media[:3]:
            date_str = f"（{n['date']}）" if n.get("date") else ""
            lines.append(f"・{n['title']} [{n['source']}]{date_str}")
        content = "\n".join(lines)
        # Google News の検索 URL をソースとして保存
        gnews_url = (
            "https://news.google.com/search"
            f"?q={quote_plus(query)}&hl=ja&gl=JP&ceid=JP:ja"
        )
        logger.info(f"  → Google News で {len(other_media)} 件のメディア情報を取得")
        return {
            **article,
            "enrich_content": content,
            "enrich_source": gnews_url,
        }

    logger.info("  → 補足情報なし")
    return None


def enrich_all(days: int = 1) -> tuple[int, int]:
    """直近 days 日の未補完記事を一括処理する。

    Returns:
        (enriched_count, failed_count)
    """
    from datetime import datetime, timezone, timedelta
    from storage import load_db, save_db

    db = load_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    targets = [
        a for a in db.values()
        if needs_enrichment(a) and (a.get("fetched_at") or "") >= cutoff
    ]

    if not targets:
        logger.info("補足対象記事なし")
        return 0, 0

    logger.info(f"補足対象: {len(targets)} 件")

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    })

    enriched = 0
    failed = 0
    for i, article in enumerate(targets):
        if i > 0:
            time.sleep(1.5)
        result = enrich_article(article, db, session)
        if result:
            db[article["id"]] = result
            enriched += 1
        else:
            failed += 1

    if enriched > 0:
        save_db(db)

    logger.info(f"補足完了: {enriched} 件, 失敗: {failed} 件")
    return enriched, failed
