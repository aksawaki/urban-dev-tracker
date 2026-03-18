#!/usr/bin/env python3
"""
main.py - 都市開発計画 情報収集ツール

使い方:
  python main.py fetch              # 収集 + レポート生成
  python main.py fetch --notify     # 収集 + ChatWork送信
  python main.py watch              # 定期実行モード
  python main.py notify             # 直近記事をChatWorkに送信
  python main.py notify --test      # ChatWork接続テスト
  python main.py show               # 直近7日の記事を表示
  python main.py show --days 30 --priority high
  python main.py stats              # DB統計
  python main.py add-area           # 監視エリアを対話式で追加
  python main.py add-area --url URL --name 名前 --area エリア --tags タグ1,タグ2
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent
os.chdir(BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("urban-dev-tracker.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = BASE_DIR / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(config: dict):
    config_path = BASE_DIR / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)


# ─── fetch ────────────────────────────────────────────────────────────────────

def cmd_fetch(args):
    from scraper import collect_all
    from storage import upsert_articles, save_raw, purge_old, get_recent
    from reporter import generate_report

    config = load_config()
    retention = config.get("settings", {}).get("data_retention_days", 365)
    do_notify = getattr(args, "notify", False)

    logger.info("=== 収集開始 ===")
    articles = collect_all(config, snapshot_dir=str(BASE_DIR / "data/snapshots"))
    logger.info(f"合計 {len(articles)} 件収集")

    new_count = 0
    if articles:
        save_raw(articles, label="fetch")
        new_count, skip_count = upsert_articles(articles)
        logger.info(f"新規: {new_count} 件 / スキップ: {skip_count} 件")

    purge_old(retention)

    # 建設通信新聞等の会員制記事を無料ソースで補完
    if new_count > 0:
        try:
            from enricher import enrich_all
            e_count, e_fail = enrich_all(days=1)
            if e_count > 0:
                logger.info(f"補足情報取得: {e_count} 件")
        except Exception as exc:
            logger.warning(f"補足情報取得をスキップ: {exc}")

    recent = get_recent(days=7)
    if recent:
        report_path = generate_report(recent)
        logger.info(f"レポート生成: {report_path}")
        print(f"\nレポートを生成しました: {report_path}")
    else:
        print("直近7日間に記事がありません")

    # ChatWork 通知（新規記事がある場合 or --notify 明示時）
    if do_notify or (new_count > 0 and _chatwork_enabled(config)):
        _do_notify(config, articles if not do_notify else recent)

    print(f"\n収集完了: {len(articles)} 件（新規 {new_count} 件）")


def _chatwork_enabled(config: dict) -> bool:
    from notifier import ChatWorkNotifier
    return ChatWorkNotifier.from_config(config) is not None


def _do_notify(config: dict, articles: list[dict]):
    from notifier import ChatWorkNotifier
    notifier = ChatWorkNotifier.from_config(config)
    if not notifier:
        print("ChatWork未設定。config.yaml の chatwork セクション or 環境変数を確認してください")
        return
    daily = config.get("chatwork", {}).get("daily_digest", True)
    if daily:
        ok = notifier.send_daily_digest(articles)
    else:
        ok = notifier.send(articles)
    if ok:
        print("ChatWork 送信完了")
    else:
        print("ChatWork 送信失敗。ログを確認してください")


# ─── notify ───────────────────────────────────────────────────────────────────

def cmd_notify(args):
    from storage import get_recent
    from notifier import ChatWorkNotifier, test_connection

    config = load_config()

    # 接続テスト
    if getattr(args, "test", False):
        notifier = ChatWorkNotifier.from_config(config)
        if not notifier:
            print("ChatWork未設定。以下を確認してください：")
            print("  config.yaml の chatwork.token / chatwork.room_id")
            print("  または環境変数 CHATWORK_TOKEN / CHATWORK_ROOM_ID")
            return
        ok = test_connection(notifier.token, notifier.room_id)
        print("接続テスト: " + ("✅ 成功" if ok else "❌ 失敗"))
        return

    days = getattr(args, "days", 1)
    priority = getattr(args, "priority", None)
    articles = get_recent(days=days, priority=priority or None)

    if not articles:
        print(f"直近 {days} 日間に送信対象の記事がありません")
        return

    _do_notify(config, articles)
    print(f"送信対象: {len(articles)} 件")


# ─── watch ────────────────────────────────────────────────────────────────────

def cmd_watch(args):
    from datetime import datetime

    config = load_config()
    interval_hours = config.get("settings", {}).get("check_interval_hours", 6)
    digest_hour = config.get("chatwork", {}).get("digest_hour", 8)
    interval_sec = interval_hours * 3600
    daily_digest = config.get("chatwork", {}).get("daily_digest", True)

    logger.info(f"定期実行モード: {interval_hours}時間ごとに収集")
    print(f"定期実行モード開始（{interval_hours}時間ごと）。Ctrl+C で停止。")
    if _chatwork_enabled(config):
        if daily_digest:
            print(f"ChatWork: 毎日 {digest_hour}:00 に日次ダイジェストを送信")
        else:
            print("ChatWork: 新規記事があるたびに送信")

    last_digest_date = None

    class FetchArgs:
        report = True
        notify = False

    while True:
        try:
            cmd_fetch(FetchArgs())

            # 日次ダイジェスト送信
            if daily_digest and _chatwork_enabled(config):
                now = datetime.now()
                today = now.date()
                if now.hour >= digest_hour and last_digest_date != today:
                    from storage import get_recent
                    from notifier import ChatWorkNotifier
                    config = load_config()
                    notifier = ChatWorkNotifier.from_config(config)
                    if notifier:
                        articles = get_recent(days=1)
                        if articles:
                            notifier.send_daily_digest(articles)
                            logger.info("ChatWork 日次ダイジェスト送信完了")
                    last_digest_date = today

            logger.info(f"次回実行まで {interval_hours} 時間待機...")
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            print("\n停止しました")
            break
        except Exception as e:
            logger.error(f"収集エラー: {e}", exc_info=True)
            logger.info("5分後にリトライ...")
            time.sleep(300)


# ─── show ─────────────────────────────────────────────────────────────────────

def cmd_show(args):
    from storage import get_recent, load_db

    priority = getattr(args, "priority", None)
    days = getattr(args, "days", 7)
    area = getattr(args, "area", None)
    keyword = getattr(args, "keyword", None)

    articles = get_recent(days=days, priority=priority or None)

    # エリアフィルタ
    if area:
        articles = [a for a in articles if area in a.get("area", "")]

    # キーワードフィルタ（タイトル・サマリー）
    if keyword:
        articles = [
            a for a in articles
            if keyword in a.get("title", "") or keyword in a.get("summary", "")
        ]

    if not articles:
        print(f"直近 {days} 日間に該当記事がありません")
        return

    print(f"\n=== 直近 {days} 日間の記事 ({len(articles)} 件) ===\n")
    PRIORITY_ICON = {"high": "🔴", "medium": "🟡", "normal": "⚪"}

    for a in articles:
        icon = PRIORITY_ICON.get(a.get("priority", "normal"), "")
        date = (a.get("fetched_at") or "")[:10]
        print(f"{icon} [{date}] {a['area']} - {a['title']}")
        print(f"   {a['url']}")
        if a.get("summary"):
            print(f"   {a['summary'][:120]}...")
        print()


# ─── stats ────────────────────────────────────────────────────────────────────

def cmd_stats(args):
    from storage import load_db

    db = load_db()
    print(f"\n=== DB 統計 ===")
    print(f"総記事数: {len(db)} 件\n")

    area_count: dict[str, int] = {}
    priority_count: dict[str, int] = {"high": 0, "medium": 0, "normal": 0}
    source_count: dict[str, int] = {}

    for a in db.values():
        area_count[a.get("area", "?")] = area_count.get(a.get("area", "?"), 0) + 1
        p = a.get("priority", "normal")
        priority_count[p] = priority_count.get(p, 0) + 1
        src = a.get("source_name", "?")
        source_count[src] = source_count.get(src, 0) + 1

    print("■ 優先度別")
    for p, c in priority_count.items():
        print(f"  {p}: {c} 件")

    print("\n■ エリア別 (上位15)")
    for area, c in sorted(area_count.items(), key=lambda x: -x[1])[:15]:
        print(f"  {area}: {c} 件")

    print("\n■ ソース別 (上位15)")
    for src, c in sorted(source_count.items(), key=lambda x: -x[1])[:15]:
        print(f"  {src}: {c} 件")

    config = load_config()
    page_count = len(config.get("sources", {}).get("pages", []))
    feed_count = len(config.get("sources", {}).get("feeds", []))
    custom_count = len(config.get("custom_areas", []))
    print(f"\n■ 設定")
    print(f"  監視ページ: {page_count} 件")
    print(f"  RSSフィード: {feed_count} 件")
    print(f"  カスタムエリア: {custom_count} 件")
    cw = config.get("chatwork", {})
    cw_enabled = bool(
        (os.environ.get("CHATWORK_TOKEN") or cw.get("token"))
        and (os.environ.get("CHATWORK_ROOM_ID") or cw.get("room_id"))
    )
    print(f"  ChatWork: {'✅ 設定済み' if cw_enabled else '❌ 未設定'}")


# ─── add-area ─────────────────────────────────────────────────────────────────

def cmd_add_area(args):
    """監視エリア（ページ）を対話式またはオプションで追加"""
    import re

    config = load_config()

    url = getattr(args, "url", None)
    name = getattr(args, "name", None)
    area = getattr(args, "area", None)
    tags_str = getattr(args, "tags", None)

    print("\n=== 監視エリア追加 ===")

    # 対話式入力
    if not url:
        url = input("監視URL: ").strip()
    if not url:
        print("URLが入力されていません")
        return

    if not name:
        name = input(f"表示名 [例: 新宿区 都市計画]: ").strip()
    if not name:
        # URLからデフォルト名を生成
        from urllib.parse import urlparse
        name = urlparse(url).netloc

    if not area:
        area = input(f"エリア名 [例: 新宿区 / 横浜市]: ").strip() or name

    if not tags_str:
        tags_str = input(f"タグ（カンマ区切り）[例: 再開発,都市計画]: ").strip()

    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else [area]

    css_selector = getattr(args, "selector", None)
    if not css_selector:
        css_selector = input("CSSセレクタ [デフォルト: main, article]: ").strip() or "main, article"

    # ID生成
    source_id = re.sub(r"[^a-z0-9]", "_", url.lower().split("//")[-1][:40])

    new_source = {
        "id": source_id,
        "name": name,
        "url": url,
        "area": area,
        "tags": tags,
        "css_selector": css_selector,
    }

    # 疎通確認
    print(f"\nURL疎通確認中: {url}")
    try:
        import requests
        ua = config.get("settings", {}).get("user_agent", "urban-dev-tracker/1.0")
        resp = requests.get(url, timeout=10, headers={"User-Agent": ua})
        if resp.status_code == 200:
            print(f"✅ OK (HTTP {resp.status_code})")
        else:
            print(f"⚠️  HTTP {resp.status_code} - URLを確認してください")
            confirm = input("このまま追加しますか？ (y/N): ").strip().lower()
            if confirm != "y":
                print("キャンセルしました")
                return
    except Exception as e:
        print(f"❌ 接続エラー: {e}")
        confirm = input("このまま追加しますか？ (y/N): ").strip().lower()
        if confirm != "y":
            print("キャンセルしました")
            return

    # config.yaml に追加
    if "sources" not in config:
        config["sources"] = {}
    if "pages" not in config["sources"]:
        config["sources"]["pages"] = []

    # 重複チェック
    existing_urls = [p.get("url") for p in config["sources"]["pages"]]
    if url in existing_urls:
        print(f"⚠️  このURLは既に登録されています")
        return

    config["sources"]["pages"].append(new_source)
    save_config(config)

    print(f"\n✅ 追加完了:")
    print(f"  名前: {name}")
    print(f"  URL:  {url}")
    print(f"  エリア: {area}")
    print(f"  タグ: {', '.join(tags)}")
    print(f"\n次回の fetch で収集が始まります。すぐに確認したい場合:")
    print(f"  python3 main.py fetch")


# ─── crawl ────────────────────────────────────────────────────────────────────

def cmd_crawl(args):
    """個別記事を収集してエリア別タイムラインを表示する"""
    from scraper import crawl_all
    from storage import upsert_articles, save_raw, get_recent
    from viewer import open_area_timeline, export_area_timeline

    config = load_config()
    max_per_source = getattr(args, "max", 8)

    logger.info("=== 記事クロール開始 ===")
    print(f"各ソースから最大 {max_per_source} 件の記事を収集します...")
    articles = crawl_all(config, max_articles_per_source=max_per_source)
    logger.info(f"合計 {len(articles)} 件収集")
    print(f"収集完了: {len(articles)} 件")

    if articles:
        save_raw(articles, label="crawl")
        new_count, skip_count = upsert_articles(articles)
        print(f"新規: {new_count} 件 / 重複スキップ: {skip_count} 件")

    # DB から直近90日のデータを取得してタイムライン表示
    recent = get_recent(days=90)

    export = getattr(args, "export", False)
    if export:
        out = export_area_timeline(recent)
        print(f"HTMLを保存しました: {out}")
        subprocess.run(["open", str(out)])
    else:
        open_area_timeline(recent)


def cmd_timeline(args):
    """収集済みデータをエリア別タイムラインで表示（再収集なし）"""
    from storage import get_recent
    from viewer import open_area_timeline, export_area_timeline

    days = getattr(args, "days", 90)
    area = getattr(args, "area", None)
    recent = get_recent(days=days)

    if area:
        recent = [a for a in recent if area in a.get("area", "")]

    if not recent:
        print("表示できる記事がありません。先に: python3 main.py crawl")
        return

    print(f"{len(recent)} 件のデータを表示します")

    export = getattr(args, "export", False)
    if export:
        out = export_area_timeline(recent)
        print(f"HTMLを保存しました: {out}")
        subprocess.run(["open", str(out)])
    else:
        open_area_timeline(recent)


# ─── view ─────────────────────────────────────────────────────────────────────

def cmd_view(args):
    """レポートをブラウザで表示"""
    from viewer import open_in_browser, export_html, open_rich_browser, export_rich_html
    from storage import get_recent
    from pathlib import Path

    export = getattr(args, "export", False)
    rich = getattr(args, "rich", False)

    if rich:
        days = getattr(args, "days", 30)
        recent = get_recent(days=days)
        if not recent:
            print("表示できる記事がありません。先に: python3 main.py crawl")
            return
        if export:
            out = export_rich_html(recent)
            print(f"HTMLを保存しました: {out}")
            subprocess.run(["open", str(out)])
        else:
            open_rich_browser(recent)
    else:
        md_path = None
        if export:
            out = export_html(md_path)
            print(f"HTMLを保存しました: {out}")
            subprocess.run(["open", str(out)])
        else:
            open_in_browser(md_path)


# ─── list-areas ───────────────────────────────────────────────────────────────

def cmd_list_areas(args):
    """登録済みの監視ページ一覧を表示"""
    config = load_config()
    pages = config.get("sources", {}).get("pages", [])
    feeds = config.get("sources", {}).get("feeds", [])

    print(f"\n=== 監視ページ一覧 ({len(pages)} 件) ===\n")
    for p in pages:
        print(f"  [{p.get('area', '?')}] {p.get('name', '?')}")
        print(f"    {p.get('url', '?')}")
        tags = ", ".join(p.get("tags", []))
        if tags:
            print(f"    tags: {tags}")
    if feeds:
        print(f"\n=== RSSフィード ({len(feeds)} 件) ===\n")
        for f in feeds:
            print(f"  [{f.get('area', '?')}] {f.get('name', '?')}")
            print(f"    {f.get('url', '?')}")


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="都市開発計画 情報収集ツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command")

    # fetch
    p_fetch = subparsers.add_parser("fetch", help="一回収集実行 + レポート生成")
    p_fetch.add_argument("--notify", action="store_true", help="収集後にChatWorkへ送信")

    # watch
    subparsers.add_parser("watch", help="定期収集モード（ChatWork日次ダイジェスト対応）")

    # notify
    p_notify = subparsers.add_parser("notify", help="収集済み記事をChatWorkに送信")
    p_notify.add_argument("--days", type=int, default=1, help="直近N日の記事（デフォルト: 1）")
    p_notify.add_argument("--priority", choices=["high", "medium", "normal"], help="優先度フィルタ")
    p_notify.add_argument("--test", action="store_true", help="ChatWork接続テスト")

    # show
    p_show = subparsers.add_parser("show", help="収集済み記事を表示")
    p_show.add_argument("--days", type=int, default=7, help="直近N日（デフォルト: 7）")
    p_show.add_argument("--priority", choices=["high", "medium", "normal"], help="優先度フィルタ")
    p_show.add_argument("--area", type=str, help="エリア名でフィルタ（部分一致）")
    p_show.add_argument("--keyword", type=str, help="キーワードでフィルタ")

    # stats
    subparsers.add_parser("stats", help="DB統計を表示")

    # add-area
    p_add = subparsers.add_parser("add-area", help="監視エリアを追加（対話式）")
    p_add.add_argument("--url", type=str, help="監視するURL")
    p_add.add_argument("--name", type=str, help="表示名")
    p_add.add_argument("--area", type=str, help="エリア名")
    p_add.add_argument("--tags", type=str, help="タグ（カンマ区切り）")
    p_add.add_argument("--selector", type=str, help="CSSセレクタ（デフォルト: main, article）")

    # enrich
    p_enrich = subparsers.add_parser("enrich", help="会員制記事を無料ソースで補完")
    p_enrich.add_argument("--days", type=int, default=1, help="直近N日の対象記事（デフォルト: 1）")

    # list-areas
    subparsers.add_parser("list-areas", help="登録済みの監視ページ一覧を表示")

    # crawl
    p_crawl = subparsers.add_parser("crawl", help="個別記事を収集してエリア別タイムラインで可視化")
    p_crawl.add_argument("--max", type=int, default=8, help="ソースあたりの最大取得記事数（デフォルト: 8）")
    p_crawl.add_argument("--export", action="store_true", help="HTMLファイルとして保存")

    # timeline
    p_tl = subparsers.add_parser("timeline", help="収集済みデータをエリア別タイムラインで表示（再収集なし）")
    p_tl.add_argument("--days", type=int, default=90, help="直近N日のデータ（デフォルト: 90）")
    p_tl.add_argument("--area", type=str, help="エリア名でフィルタ（部分一致）")
    p_tl.add_argument("--export", action="store_true", help="HTMLファイルとして保存")

    # view
    p_view = subparsers.add_parser("view", help="レポートをブラウザで表示")
    p_view.add_argument("--export", action="store_true", help="HTMLファイルとして保存")
    p_view.add_argument("--rich", action="store_true", help="カード形式のリッチビューで表示")
    p_view.add_argument("--days", type=int, default=30, help="直近N日の記事を表示（--rich 時）")

    args = parser.parse_args()

    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "notify":
        cmd_notify(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "add-area":
        cmd_add_area(args)
    elif args.command == "enrich":
        from enricher import enrich_all
        days = getattr(args, "days", 1)
        enriched, failed = enrich_all(days=days)
        print(f"補足完了: {enriched} 件 / 失敗: {failed} 件")
    elif args.command == "list-areas":
        cmd_list_areas(args)
    elif args.command == "crawl":
        cmd_crawl(args)
    elif args.command == "timeline":
        cmd_timeline(args)
    elif args.command == "view":
        cmd_view(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
