"""
scraper.py - Web スクレイパー & RSS フィードリーダー

RSSフィード読み込みとHTMLページ変更監視の両方に対応。
変更検知はコンテンツのSHA256ハッシュで行う。
"""

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

# 記事URLと判定する正規表現
_ARTICLE_URL_RE = re.compile(
    r'(/news(?:/|$)|/press(?:/|$)|/topics(?:/|$)|/hodo(?:/|$)|'
    r'/info(?:/|$)|/report(?:/|$)|/detail(?:/|$)|/article(?:/|$)|'
    r'/pickup(?:/|$)|/release(?:/|$)|/whatsnew(?:/|$)|/what_new(?:/|$)|'
    r'/oshirase(?:/|$)|/osirase(?:/|$)|/koho(?:/|$)|'
    r'/archives(?:/|$)|'   # kensetsunews 等の /archives/1234567 形式
    r'/\d{4}/\d{2}/|\d{8}|\?id=\d+|&id=\d+|'
    r'/\d{4,8}/index\.html$|/\d{4,8}\.html$|'  # 市区町村HP: /1234567/index.html, /1234567.html
    r'/[a-z]\d{4,8}\.html$|'  # さいたま市等: /p125622.html 形式
    r'/\d{4,8}/?$)',
    re.IGNORECASE,
)

_EXCLUDE_URL_RE = re.compile(
    r'(javascript:|mailto:|^#|'
    r'twitter\.com|facebook\.com|instagram\.com|youtube\.com|'
    r'linkedin\.com|line\.me|smartnews\.com|'
    r'/search|/sitemap|/login|/logout)',
    re.IGNORECASE,
)

_EXCLUDE_EXT = frozenset([
    '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg',
    '.ico', '.mp4', '.mp3', '.zip', '.xlsx', '.xls', '.doc', '.docx', '.ppt',
])

_DATE_RE = re.compile(
    r'(\d{4}[年/\-]\s*\d{1,2}[月/\-]\s*\d{1,2}日?|\d{4}-\d{2}-\d{2})'
)

# JavaScript 使用案内の段落を除外（コンテンツとして意味がない）
_JS_WARN_RE = re.compile(
    r'^(?:(?:この|当)(?:サイト|ホームページ)では?javascript|'
    r'javascript(?:の使用)?を有効|'
    r'ブラウザの設定でjavascript|'
    r'お手数ですがjavascriptの使用)',
    re.IGNORECASE,
)

# kensetsunews等の h1 タグが「記事タイトル 最終更新 | 2026/03/12 16:46 【速報】次の記事...」
# という形式になっているため、末尾のゴミを除去する
_TITLE_TRAILING_JUNK_RE = re.compile(
    r'\s+(?:最終更新|更新日時)\s*[|｜]\s*\d{4}.*',
    re.DOTALL,
)

# 開発・建設関連キーワード（これを含まない記事は収集しない）
_RELEVANCE_KEYWORDS = frozenset([
    # 再開発・都市計画系
    "再開発", "都市計画", "市街地再開発", "都市再生", "再整備",
    "開発計画", "市街地", "開発事業", "整備事業", "整備計画",
    "事業計画", "地区計画", "特定街区", "権利変換", "組合設立",
    "高度利用", "容積率", "用途地域", "施行認可", "組合認可",
    "準備組合", "地権者", "権利床", "保留床",
    # 建設・工事系（建設通信新聞に多い）
    "着工", "竣工", "開業", "新築", "改築", "建設", "建替",
    "解体", "解体工事", "工事着手", "工事開始", "取り壊し",
    "施工", "設計", "工事契約",
    # スペック系（建設通信記事の特徴：延べ面積・階数で識別）
    "延べ", "延床", "総延べ", "㎡", "地上", "地下", "階建",
    "敷地", "街区", "ha", "ヘクタール",
    # 建物・施設系
    "タワー", "超高層", "複合施設", "複合ビル", "複合開発",
    "大規模", "ビルディング", "商業施設", "オフィス", "ホテル",
    "マンション", "住宅棟", "物流施設", "アリーナ", "スタジアム",
    # 事業スキーム
    "事業化", "共同開発", "基本協定", "基本計画", "基本設計",
    "まちづくり", "駅前整備", "駅周辺", "プロジェクト",
])

# タイトルに含まれる場合にのみ確実に通過させる「強い」キーワード
_STRONG_TITLE_KEYWORDS = frozenset([
    # 再開発系
    "再開発", "市街地再開発", "再開発組合", "準備組合",
    "都市計画決定", "都市計画変更", "開発事業", "開発計画",
    "整備事業", "整備計画", "地区計画", "特定街区", "区画整理",
    "権利変換", "組合設立", "施行認可",
    # 建設・工事系
    "着工", "竣工", "開業", "供用開始", "新築", "建替", "建設",
    "解体", "取り壊し", "工事着手",
    # 施設・規模系
    "タワー", "複合施設", "大規模開発", "事業化", "超高層",
    "マンション", "ビルディング", "アリーナ", "スタジアム",
    # 事業スキーム
    "共同開発", "基本計画", "TOWER", "GATE", "CROSS", "Ave.",
    "プロジェクト",
])

logger = logging.getLogger(__name__)


@dataclass
class Article:
    """収集した記事・情報の単位"""
    id: str
    source_id: str
    source_name: str
    title: str
    url: str
    area: str
    tags: list[str]
    published_at: Optional[str]
    fetched_at: str
    summary: str = ""
    content: str = ""          # 記事本文（全文）
    content_hash: str = ""
    priority: str = "normal"   # high / medium / normal
    is_new: bool = True


def _make_id(source_id: str, url: str, title: str = "") -> str:
    # タイトルは ID に含めない（title が clean/dirty で揺れても同一記事として扱うため）
    raw = f"{source_id}::{url}"
    return hashlib.md5(raw.encode()).hexdigest()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify_priority(text: str, keywords: dict) -> str:
    t = text.lower()
    for kw in keywords.get("high_priority", []):
        if kw in t:
            return "high"
    for kw in keywords.get("medium_priority", []):
        if kw in t:
            return "medium"
    return "normal"


class FeedReader:
    """RSS / Atom フィードを読み込む"""

    def __init__(self, source: dict, keywords: dict, timeout: int = 15):
        self.source = source
        self.keywords = keywords
        self.timeout = timeout

    def fetch(self) -> list[Article]:
        url = self.source["url"]
        logger.info(f"[RSS] {self.source['name']} → {url}")
        try:
            feed = feedparser.parse(url, request_headers={
                "User-Agent": "urban-dev-tracker/1.0"
            })
        except Exception as e:
            logger.warning(f"  フィード取得エラー: {e}")
            return []

        articles = []
        for entry in feed.entries[:30]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", "")).strip()
            published = entry.get("published", entry.get("updated", ""))

            # BeautifulSoupでHTMLタグを除去
            clean_summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)[:300]

            text_for_priority = f"{title} {clean_summary}"
            priority = _classify_priority(text_for_priority, self.keywords)

            article = Article(
                id=_make_id(self.source["id"], link, title),
                source_id=self.source["id"],
                source_name=self.source["name"],
                title=title,
                url=link,
                area=self.source.get("area", ""),
                tags=self.source.get("tags", []),
                published_at=published,
                fetched_at=_now_iso(),
                summary=clean_summary,
                content_hash=_content_hash(title + clean_summary),
                priority=priority,
            )
            articles.append(article)

        logger.info(f"  → {len(articles)} 件取得")
        return articles


class PageWatcher:
    """HTMLページを監視し、変更があれば記事として返す"""

    def __init__(self, source: dict, keywords: dict,
                 timeout: int = 15, snapshot_dir: str = "data/snapshots"):
        self.source = source
        self.keywords = keywords
        self.timeout = timeout
        self.snapshot_dir = snapshot_dir

    def _snapshot_path(self) -> str:
        import os
        os.makedirs(self.snapshot_dir, exist_ok=True)
        return f"{self.snapshot_dir}/{self.source['id']}.hash"

    def _load_prev_hash(self) -> Optional[str]:
        path = self._snapshot_path()
        try:
            with open(path) as f:
                return f.read().strip()
        except FileNotFoundError:
            return None

    def _save_hash(self, h: str):
        with open(self._snapshot_path(), "w") as f:
            f.write(h)

    def fetch(self, session: requests.Session) -> list[Article]:
        url = self.source["url"]
        logger.info(f"[PAGE] {self.source['name']} → {url}")

        ssl_verify = self.source.get("ssl_verify", True)
        try:
            resp = session.get(url, timeout=self.timeout, verify=ssl_verify)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
        except Exception as e:
            logger.warning(f"  ページ取得エラー: {e}")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        # CSS セレクタで対象要素を絞る
        selector = self.source.get("css_selector", "main, article, .content")
        target = soup.select(selector)
        if not target:
            target = [soup.body] if soup.body else [soup]

        text = " ".join(el.get_text(" ", strip=True) for el in target)
        current_hash = _content_hash(text)
        prev_hash = self._load_prev_hash()

        if current_hash == prev_hash:
            logger.info("  → 変更なし")
            return []

        self._save_hash(current_hash)

        # ページタイトル取得
        page_title = soup.title.string.strip() if soup.title else self.source["name"]
        summary = text[:300]
        priority = _classify_priority(text, self.keywords)
        is_new = prev_hash is None  # 初回取得かどうか

        article = Article(
            id=_make_id(self.source["id"], url, current_hash),
            source_id=self.source["id"],
            source_name=self.source["name"],
            title=f"【更新検知】{page_title}",
            url=url,
            area=self.source.get("area", ""),
            tags=self.source.get("tags", []),
            published_at=None,
            fetched_at=_now_iso(),
            summary=summary,
            content_hash=current_hash,
            priority=priority,
            is_new=is_new,
        )
        logger.info(f"  → 変更検知 priority={priority}")
        return [article]


class ArticleCrawler:
    """ニュースインデックスページから個別記事リンクを辿り、本文を収集する"""

    def __init__(self, source: dict, keywords: dict,
                 timeout: int = 15, max_articles: int = 8):
        self.source = source
        self.keywords = keywords
        self.timeout = timeout
        self.max_articles = max_articles

    def fetch(self, session: requests.Session) -> list[Article]:
        url = self.source["url"]
        logger.info(f"[CRAWL] {self.source['name']} → {url}")

        ssl_verify = self.source.get("ssl_verify", True)
        try:
            resp = session.get(url, timeout=self.timeout, verify=ssl_verify)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
        except Exception as e:
            logger.warning(f"  インデックスページ取得エラー: {e}")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        base_domain = urlparse(url).netloc

        # ナビゲーション・フッターを除去してからリンク抽出
        for tag in soup.find_all(["nav", "header", "footer", "aside"]):
            tag.decompose()

        selector = self.source.get("css_selector", "main, article, .content, #content, body")
        target_els = soup.select(selector) or ([soup.body] if soup.body else [soup])

        article_urls = self._extract_article_links(target_els, url, base_domain)
        if not article_urls:
            logger.info(f"  → 記事リンクが見つかりません")
            return []

        logger.info(f"  → {len(article_urls)} 件のリンクを発見")
        articles = []
        for article_url, link_text in article_urls[: self.max_articles]:
            article = self._fetch_article(article_url, session, link_text=link_text)
            if article:
                articles.append(article)
            time.sleep(0.8)

        logger.info(f"  → {len(articles)} 件の記事を収集")
        return articles

    def _extract_article_links(
        self, target_els: list, base_url: str, base_domain: str
    ) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []

        for el in target_els:
            for a_tag in el.find_all("a", href=True):
                href = (a_tag.get("href") or "").strip()
                if not href:
                    continue

                full_url = urljoin(base_url, href)
                parsed = urlparse(full_url)

                # 同ドメインのみ
                if parsed.netloc and parsed.netloc != base_domain:
                    continue
                # 除外パターン
                if _EXCLUDE_URL_RE.search(full_url):
                    continue
                # 拡張子チェック
                if any(parsed.path.lower().endswith(ext) for ext in _EXCLUDE_EXT):
                    continue
                # インデックスページ自身は除外
                if full_url.rstrip("/") == base_url.rstrip("/"):
                    continue

                # 記事URLパターン or リンクテキストに日付
                if not _ARTICLE_URL_RE.search(full_url):
                    text = a_tag.get_text(strip=True)
                    if not _DATE_RE.search(text):
                        continue

                if full_url not in seen:
                    seen.add(full_url)
                    # リンクのアンカーテキストをタイトルヒントとして保存
                    link_text = a_tag.get_text(" ", strip=True)[:120]
                    result.append((full_url, link_text))

        return result

    def _fetch_article(self, url: str, session: requests.Session,
                       link_text: str = "") -> Optional[Article]:
        ssl_verify = self.source.get("ssl_verify", True)
        try:
            resp = session.get(url, timeout=self.timeout, verify=ssl_verify)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
        except Exception as e:
            logger.warning(f"  記事取得エラー {url}: {e}")
            return None

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            logger.warning(f"  HTMLパースエラー {url}: {e}")
            return None

        # ナビ・サイドバー・ウィジェット除去（積極的に）
        for tag in soup.find_all(["nav", "header", "footer", "aside"]):
            tag.decompose()
        # class/id にナビ系の文字列を含む要素を除去
        _NAV_PATTERNS = re.compile(
            r'(nav|sidebar|side-bar|gnav|snav|global-nav|local-nav|'
            r'breadcrumb|crumb|menu|widget|banner|related|recommend|'
            r'pager|pagination|tool|share|social|footer|header)',
            re.IGNORECASE,
        )
        from bs4 import Tag as _BS4Tag
        # まずリストに収集してからdecompose（イテレート中のdecomposeを避ける）
        _nav_tags = []
        for tag in soup.find_all(True):
            if not isinstance(tag, _BS4Tag) or tag.attrs is None:
                continue
            cls = " ".join(tag.get("class") or [])
            tid = tag.get("id") or ""
            if _NAV_PATTERNS.search(cls) or _NAV_PATTERNS.search(tid):
                _nav_tags.append(tag)
        for tag in _nav_tags:
            try:
                tag.decompose()
            except Exception:
                pass

        # 汎用セクション見出しリスト（これが検出されたらアンカーテキストを優先）
        _GENERIC_TITLES = {
            "ニュースリリース", "プレスリリース", "ニュース", "お知らせ",
            "新着情報", "報道発表", "トピックス", "最新情報", "information",
            "news release", "press release", "news", "topics",
        }
        title = self._extract_title(soup)
        # kensetsunews 等: h1 末尾に「最終更新 | yyyy/mm/dd …」が混入するので除去
        title = _TITLE_TRAILING_JUNK_RE.sub("", title).strip()
        # 汎用タイトルだったらアンカーテキストを優先
        if not title or len(title) < 3 or title.strip().lower() in _GENERIC_TITLES:
            if link_text and len(link_text) > 5:
                title = link_text
            elif soup.title:
                title = soup.title.string.strip()
            else:
                title = self.source["name"]

        published_at = self._extract_date(soup)
        content = self._extract_content(soup)
        summary = content[:300] if content else title[:300]

        # ── 関連性チェック（2段階）──────────────────────────────
        # 1. タイトルに強いキーワード → 即通過
        title_has_strong = any(kw in title for kw in _STRONG_TITLE_KEYWORDS)
        # 2. 本文（ナビ除去後）にキーワードが含まれる → 通過
        body_text = content[:2000]  # 本文先頭2000字のみチェック
        body_has_kw = any(kw in body_text for kw in _RELEVANCE_KEYWORDS)

        if not (title_has_strong or body_has_kw):
            logger.debug(f"  スキップ（無関係）: {title[:50]}")
            return None

        priority = _classify_priority(f"{title} {content}", self.keywords)

        return Article(
            id=_make_id(self.source["id"], url, title),
            source_id=self.source["id"],
            source_name=self.source["name"],
            title=title,
            url=url,
            area=self.source.get("area", ""),
            tags=self.source.get("tags", []),
            published_at=published_at,
            fetched_at=_now_iso(),
            summary=summary,
            content=content,
            content_hash=_content_hash(title + content),
            priority=priority,
        )

    def _extract_title(self, soup: BeautifulSoup) -> str:
        for sel in [
            "h1", ".article-title", ".news-title", ".entry-title",
            ".post-title", ".ttl", ".page-title",
        ]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(" ", strip=True)
                if t and len(t) > 3:
                    return t
        return ""

    def _extract_date(self, soup: BeautifulSoup) -> Optional[str]:
        # <time> タグ
        time_el = soup.select_one("time")
        if time_el:
            dt = time_el.get("datetime") or time_el.get_text(strip=True)
            if dt:
                return dt.strip()
        # date 系クラス
        for cls_kw in ["date", "time", "day", "posted", "published", "update"]:
            for el in soup.find_all(class_=lambda c: c and cls_kw in str(c).lower()):
                m = _DATE_RE.search(el.get_text())
                if m:
                    return m.group(1)
        # 本文内パターン
        m = _DATE_RE.search(soup.get_text())
        return m.group(1) if m else None

    def _extract_content(self, soup: BeautifulSoup) -> str:
        for sel in [
            "article", ".article-body", ".article-content", ".article-detail",
            ".news-body", ".news-content", ".news-detail", ".press-content",
            ".entry-content", ".post-content", ".content-body", ".main-content",
            "main", "#main", "#content", ".content",
        ]:
            el = soup.select_one(sel)
            if el:
                paras = [
                    p.get_text(" ", strip=True)
                    for p in el.find_all(["p", "li", "dd"])
                ]
                text = " ".join(
                    p for p in paras
                    if len(p) > 15 and not _JS_WARN_RE.match(p)
                )
                if len(text) > 60:
                    return text[:800]

        # フォールバック: body テキスト
        if soup.body:
            words = soup.body.get_text(" ", strip=True).split()
            return " ".join(words)[:500]
        return ""


def build_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept-Language": "ja,en;q=0.9",
    })
    return session


def crawl_all(config: dict, max_articles_per_source: int = 8) -> list[Article]:
    """全ソースから個別記事リンクを辿って本文を収集する"""
    keywords = config.get("keywords", {})
    ua = config.get("settings", {}).get("user_agent", "urban-dev-tracker/1.0")
    session = build_session(ua)
    all_articles: list[Article] = []

    for src in config.get("sources", {}).get("pages", []):
        crawler = ArticleCrawler(src, keywords, max_articles=max_articles_per_source)
        all_articles.extend(crawler.fetch(session))
        time.sleep(2)

    return all_articles


def collect_all(config: dict, snapshot_dir: str = "data/snapshots") -> list[Article]:
    """設定ファイルの全ソースから記事を収集して返す"""
    keywords = config.get("keywords", {})
    ua = config.get("settings", {}).get("user_agent", "urban-dev-tracker/1.0")
    session = build_session(ua)
    all_articles: list[Article] = []

    # RSS フィード
    for src in config.get("sources", {}).get("feeds", []):
        reader = FeedReader(src, keywords)
        all_articles.extend(reader.fetch())
        time.sleep(1)

    # HTML ページ監視
    for src in config.get("sources", {}).get("pages", []):
        watcher = PageWatcher(src, keywords, snapshot_dir=snapshot_dir)
        all_articles.extend(watcher.fetch(session))
        time.sleep(1.5)

    return all_articles
