#!/usr/bin/env python3
"""
check_urls.py - config.yaml内のURLの疎通確認

エラーのURLを一覧表示し、修正が必要なものを特定する。

使い方:
  python3 check_urls.py
  python3 check_urls.py --fix   # エラーURLのみ表示
"""

import sys
import time
import requests
import yaml
from pathlib import Path

BASE_DIR = Path(__file__).parent


def check_urls(fix_mode: bool = False):
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ua = config.get("settings", {}).get("user_agent", "urban-dev-tracker/1.0")
    session = requests.Session()
    session.headers.update({"User-Agent": ua, "Accept-Language": "ja,en;q=0.9"})

    results = []

    for kind, sources in [
        ("RSS", config["sources"].get("feeds", [])),
        ("PAGE", config["sources"].get("pages", [])),
    ]:
        for src in sources:
            url = src["url"]
            try:
                resp = session.get(url, timeout=10, allow_redirects=True)
                status = resp.status_code
                ok = status == 200
            except Exception as e:
                status = f"ERROR: {e}"
                ok = False

            results.append({
                "kind": kind,
                "id": src["id"],
                "name": src["name"],
                "url": url,
                "status": status,
                "ok": ok,
            })
            marker = "✅" if ok else "❌"
            if not fix_mode or not ok:
                print(f"{marker} [{kind}] {src['name']}")
                print(f"   {url} → {status}")
            time.sleep(0.5)

    ok_count = sum(1 for r in results if r["ok"])
    ng_count = len(results) - ok_count
    print(f"\n--- 結果: {ok_count}/{len(results)} OK, {ng_count} エラー ---")

    if ng_count > 0:
        print("\n❌ エラーURL一覧（config.yamlで修正してください）:")
        for r in results:
            if not r["ok"]:
                print(f"  id: {r['id']}")
                print(f"  url: {r['url']}")
                print(f"  status: {r['status']}")
                print()


if __name__ == "__main__":
    fix_mode = "--fix" in sys.argv
    check_urls(fix_mode)
