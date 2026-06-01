"""
viewer.py - レポートをブラウザで表示するHTMLビューア
"""

import html as _html
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import markdown

# ──────────────────────────────────────────────────────────
# エリア別タイムライン用 ユーティリティ
# ──────────────────────────────────────────────────────────

_PHASE_PATTERNS = {
    "completed":        ["竣工済", "開業済", "供用開始", "完成しました", "グランドオープン", "竣工しました", "完工", "竣工いたしました", "オープンしました"],
    "construction":     ["工事中", "施工中", "建設中", "整備中", "着工しました", "工事を開始", "工事が始まり", "工事に着手"],
    "pre_construction": ["着工予定", "工事着工予定", "着工を予定", "工事予定", "着工に向け", "工事開始予定"],
    "planning":         ["計画決定", "都市計画決定", "計画を策定", "事業認可", "認可を取得", "事業化", "計画中", "検討中", "基本計画", "事業計画", "都市計画変更"],
}

_PHASE_META = {
    "completed":        ("完成・供用中",  "#27ae60", "✅"),
    "construction":     ("工事中",        "#e67e22", "🔨"),
    "pre_construction": ("着工予定",      "#8e44ad", "📐"),
    "planning":         ("計画・検討中",  "#2980b9", "📋"),
    "info":             ("情報",          "#7f8c8d", "📄"),
}

# 着工関連日付抽出 (YEAR→ACTION 順)
_PERIOD_START_RE = re.compile(
    r'(?:\d{4}年(?:\d{1,2}月)?|\d{4}年度|令和\d+年(?:\d{1,2}月)?|令和\d+年度?)'
    r'[^\n。]{0,10}'
    r'(?:着工|工事着手|工事開始|整備着手|着手|工事に着手)',
)
# 着工 ACTION→YEAR 逆順 ("着工時期は2026年" 等)
_PERIOD_START_REV_RE = re.compile(
    r'(?:着工|工事着手|工事開始|整備着手|着手)[^\n。]{0,12}'
    r'(?:\d{4}年(?:\d{1,2}月)?|\d{4}年度|令和\d+年(?:\d{1,2}月)?|令和\d+年度?)',
)
# 竣工・完成・開業予定日抽出 (YEAR→ACTION 順)
_PERIOD_END_RE = re.compile(
    r'(?:\d{4}年(?:\d{1,2}月)?|\d{4}年度|令和\d+年(?:\d{1,2}月)?|令和\d+年度?)'
    r'[^\n。]{0,12}'
    r'(?:竣工|完成|開業|供用|オープン|完工|引渡)'
    r'(?:予定|見込み|を予定|する予定|いたします)?',
)
# 竣工 ACTION→YEAR 逆順 ("竣工・開館時期は2031年度" 等)
_PERIOD_END_REV_RE = re.compile(
    r'(?:竣工|完成|開業|供用|オープン|完工|引渡|開館|誕生|登場)'
    r'[^\n。]{0,15}'
    r'(?:\d{4}年(?:\d{1,2}月)?|\d{4}年度|令和\d+年(?:\d{1,2}月)?|令和\d+年度?)',
)
# 事業期間レンジ ("2026年1月5日～2040年3月31日" 等)
_PERIOD_RANGE_RE = re.compile(
    r'(\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?)\s*[〜～－–—]\s*'
    r'(\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?)',
)
# 「から...まで」形式の工期レンジ ("2025年10月19日から2026年3月31日まで" 等)
_PERIOD_RANGE_KARA_RE = re.compile(
    r'(\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?)\s*から\s*'
    r'(\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?)\s*まで',
)
# YEAR→開設/オープン（施設開設の日付。ギャップを30文字に拡張）
_PERIOD_END_OPEN_RE = re.compile(
    r'(?:\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?)'
    r'[^\n。]{0,30}'
    r'(?:開設|開店|開通|開館)',
)
# 月のみの着工記述（年は当該年度と推定）"5月着工", "8月着工" 等
_PERIOD_START_MONTH_RE = re.compile(
    r'(?<!\d{4}年)(\d{1,2})月(?:\d{1,2}日)?[^\n。]{0,5}(?:着工|工事開始|工事着手|工事に着手)',
)
# スケジュール文全体（着工・竣工どちらも含む）
_SCHED_RE = re.compile(
    r'[^\n。]{0,10}'
    r'(?:\d{4}年(?:\d{1,2}月)?|\d{4}年度|令和\d+年(?:\d{1,2}月)?|令和\d+年度?)'
    r'[^\n。]{0,20}'
    r'(?:竣工|開業|供用|完成|着工|オープン|整備完了|完工|予定|完了)'
    r'[^\n。]{0,20}',
)


def _detect_phase(content: str) -> str:
    for phase, keywords in _PHASE_PATTERNS.items():
        if any(kw in content for kw in keywords):
            return phase
    return "info"


def _is_recent_year(text: str, threshold: int = 2025) -> bool:
    """文字列中の最大年が threshold 以上かチェック（過去竣工の誤検出防止）
    ※ 令和年度表記（年なし）や月のみ表記は通す
    """
    years = re.findall(r'(\d{4})年', text)  # "N年" 形式の年をすべて抽出
    if not years:
        return True  # 年が見つからない場合は通す（令和年度表記等）
    return max(int(y) for y in years) >= threshold


def _extract_period(content: str) -> dict:
    """着工日・竣工予定日をコンテンツから抽出する（年単位でも抽出）"""
    start = ""
    end = ""

    # 事業期間レンジを最優先チェック（例: "2026年1月5日～2040年3月31日"）
    m = _PERIOD_RANGE_RE.search(content)
    if m:
        s, e = m.group(1), m.group(2)
        if _is_recent_year(s) or _is_recent_year(e):
            return {"start": s, "end": e}

    # 「から...まで」形式のレンジ
    m = _PERIOD_RANGE_KARA_RE.search(content)
    if m:
        s, e = m.group(1), m.group(2)
        if _is_recent_year(s) or _is_recent_year(e):
            return {"start": s, "end": e}

    # 着工: 順方向（YEAR→ACTION）→ 逆方向（ACTION→YEAR）→ 月のみ
    m = _PERIOD_START_RE.search(content)
    if m:
        cand = m.group(0)[:45].strip()
        if _is_recent_year(cand):
            start = cand
    if not start:
        m = _PERIOD_START_REV_RE.search(content)
        if m:
            cand = m.group(0)[:45].strip()
            if _is_recent_year(cand):
                start = cand
    if not start:
        m = _PERIOD_START_MONTH_RE.search(content)
        if m:
            start = f"2026年{m.group(1)}月（推定）着工"

    # 竣工: 順方向（YEAR→ACTION）→ 逆方向（ACTION→YEAR）→ 開設/開店系
    m = _PERIOD_END_RE.search(content)
    if m:
        cand = m.group(0)[:45].strip()
        if _is_recent_year(cand):
            end = cand
    if not end:
        m = _PERIOD_END_REV_RE.search(content)
        if m:
            cand = m.group(0)[:45].strip()
            if _is_recent_year(cand):
                end = cand
    if not end:
        m = _PERIOD_END_OPEN_RE.search(content)
        if m:
            cand = m.group(0)[:45].strip()
            if _is_recent_year(cand):
                end = cand

    return {"start": start, "end": end}


def _extract_schedule_sentences(content: str) -> list[str]:
    """「○○年○月 竣工予定」などのスケジュール文を抽出する"""
    results = []
    for m in _SCHED_RE.finditer(content):
        s = m.group(0).strip().rstrip("。").strip()
        if s and len(s) > 5 and s not in results:
            results.append(s)
    return results[:4]


# 地名キーワード（23区→主要市→隣接県の順で優先度高い順に並べる）
_LOCATION_KEYWORDS = [
    # 東京23区
    "千代田区", "中央区", "港区", "新宿区", "文京区", "台東区", "墨田区", "江東区",
    "品川区", "目黒区", "大田区", "世田谷区", "渋谷区", "中野区", "杉並区", "豊島区",
    "北区", "荒川区", "板橋区", "練馬区", "足立区", "葛飾区", "江戸川区",
    # 東京市部
    "立川市", "武蔵野市", "三鷹市", "八王子市", "町田市", "府中市", "調布市",
    "多摩市", "西東京市", "東村山市", "小平市",
    # 神奈川
    "横浜市", "川崎市", "相模原市", "横須賀市", "藤沢市", "平塚市",
    # 埼玉
    "さいたま市", "川口市", "越谷市", "所沢市", "草加市",
    # 千葉
    "千葉市", "船橋市", "松戸市", "市川市", "柏市", "浦安市",
    # 政令市・主要都市
    "大阪市", "名古屋市", "福岡市", "札幌市", "仙台市", "京都市", "神戸市", "北広島市", "広島市",
    # 都県
    "東京都", "神奈川県", "埼玉県", "千葉県", "茨城県", "栃木県", "群馬県",
    "大阪府", "愛知県", "福岡県",
]


# 地区名セット（これに含まれるキーワードは地区名として優先表示する）
# 区名へ変換せず、地区名のまま area として返す
_DISTRICT_NAMES: list[str] = [
    # ──── 千代田区エリア ────
    "大手町", "丸の内", "有楽町", "神保町", "九段南", "九段", "麹町",
    "永田町", "霞が関", "飯田橋", "常盤橋",
    # ──── 中央区エリア ────
    "日本橋", "銀座", "京橋", "築地", "月島", "勝どき", "晴海",
    # ──── 港区エリア ────
    "虎ノ門", "赤坂", "六本木", "麻布台", "麻布", "三田", "田町",
    "高輪", "泉岳寺", "高輪ゲートウェイ", "芝浦", "汐留", "新橋", "浜松町",
    "青山", "北青山", "南青山", "外苑", "神宮外苑", "秩父宮",
    # ──── 新宿区エリア ────
    "西新宿", "新宿西口", "歌舞伎町", "大久保", "東新宿",
    # ──── 渋谷区エリア ────
    "渋谷", "原宿", "代々木", "恵比寿", "表参道", "幡ヶ谷", "笹塚",
    # ──── 品川区・大田区エリア ────
    "大崎", "五反田", "大井町", "品川", "洗足池", "洗足", "蒲田", "羽田",
    # ──── 目黒区エリア ────
    "目黒", "中目黒", "自由が丘",
    # ──── 世田谷区エリア ────
    "二子玉川", "下北沢", "三軒茶屋",
    # ──── 台東区エリア ────
    "上野", "浅草", "秋葉原", "浅草橋",
    # ──── 墨田区エリア ────
    "錦糸町", "押上", "両国",
    # ──── 江東区エリア ────
    "豊洲", "有明", "辰巳",
    # ──── 豊島区エリア ────
    "池袋",
    # ──── 中野区エリア ────
    "中野",
    # ──── 文京区エリア ────
    "後楽園", "本郷", "水道橋",
    # ──── 荒川・足立・葛飾エリア ────
    "北千住", "千住",
    # ──── 神奈川：人口増加エリア ────
    "横浜", "川崎", "みなとみらい", "関内", "横須賀中央",
    "武蔵小杉", "元住吉", "新川崎", "川崎駅",
    # ──── 埼玉：大宮・さいたま新都心 ────
    "大宮", "さいたま新都心", "浦和", "武蔵浦和",
    # ──── 千葉：TX沿線・幕張（人口急増エリア）────
    "幕張", "幕張新都心", "柏の葉", "柏の葉キャンパス",
    "流山おおたかの森", "おおたかの森", "南流山", "流山",
    "船橋", "津田沼",
    # ──── 首都圏その他 ────
    "熱海", "柏",
]

# 区名・市名へのマッピング（外部スコープ判定用のみ：エリア表示には使わない）
_DISTRICT_TO_WARD: dict[str, str] = {d: d for d in _DISTRICT_NAMES}  # 自己マッピング（互換性維持）


def _extract_location(text: str) -> str:
    """テキスト中から地区名・区名・市名を優先度順に抽出する。
    地区名（赤坂・築地・外苑 etc）を最優先で返す。"""
    # 地区名を優先チェック（ユーザー指定の粒度）
    for district in _DISTRICT_NAMES:
        if district in text:
            return district
    # 区・市名
    for kw in _LOCATION_KEYWORDS:
        if kw in text:
            return kw
    return ""


_VAGUE_AREAS = frozenset({
    # 都道府県レベル（地区名抽出の代替としては粗すぎる）
    "全国", "東京都", "神奈川県", "埼玉県", "千葉県", "茨城県", "栃木県", "群馬県",
    "大阪府", "愛知県", "福岡県", "北海道", "宮城県", "広島県", "京都府", "兵庫県",
    # 市レベルは _VAGUE に含めない（横浜市・川崎市・さいたま市・流山市はそのまま表示）
})

# 都道府県レベル推定ヒント（「全国」より細かい分類のため）
# (テキスト中に含まれるキーワード, 返す都道府県名) の順序付きリスト
# — より具体的なキーワードを先に並べる
_PREF_HINTS: list[tuple[str, str]] = [
    # 東京
    ("東京都", "東京都"), ("東京", "東京都"), ("都内", "東京都"),
    # 神奈川
    ("神奈川県", "神奈川県"), ("神奈川", "神奈川県"),
    # 埼玉
    ("埼玉県", "埼玉県"), ("埼玉", "埼玉県"),
    # 千葉
    ("千葉県", "千葉県"), ("千葉", "千葉県"),
    # 関東その他
    ("茨城県", "茨城県"), ("茨城", "茨城県"),
    ("栃木県", "栃木県"), ("栃木", "栃木県"),
    ("群馬県", "群馬県"), ("群馬", "群馬県"),
    # 全国カバー都市（タイトルに明示されているとき設定エリアより優先）
    ("京都市", "京都市"), ("京都", "京都市"),
    ("大阪市", "大阪市"), ("大阪", "大阪市"),
    ("神戸市", "神戸市"), ("神戸", "神戸市"),
    ("福岡市", "福岡市"), ("福岡", "福岡市"),
    ("札幌市", "札幌市"), ("札幌", "札幌市"),
    ("仙台市", "仙台市"), ("仙台", "仙台市"),
    ("名古屋市", "名古屋市"), ("名古屋", "名古屋市"),
]


def _extract_pref(text: str) -> str:
    """テキストから都道府県名を推定する（地区名が取れない場合の fallback）"""
    for hint, pref in _PREF_HINTS:
        if hint in text:
            return pref
    return ""


# ──────────────────────────────────────────────────────────
# 地方 (region) ⇄ 都道府県 (prefecture) ⇄ エリア (area) マッピング
# UI のナビゲーション（地方→都道府県→記事）に使う
# ──────────────────────────────────────────────────────────
_PREFECTURE_TO_REGION: dict[str, str] = {
    "東京": "関東", "神奈川": "関東", "埼玉": "関東", "千葉": "関東",
    "茨城": "関東", "栃木": "関東", "群馬": "関東",
    "大阪": "関西", "京都": "関西", "兵庫": "関西", "奈良": "関西",
    "和歌山": "関西", "滋賀": "関西",
    "愛知": "中部", "静岡": "中部", "岐阜": "中部", "三重": "中部",
    "長野": "中部", "山梨": "中部", "新潟": "中部",
    "福井": "中部", "石川": "中部", "富山": "中部",
    "北海道": "北海道",
    "宮城": "東北", "福島": "東北", "山形": "東北",
    "岩手": "東北", "秋田": "東北", "青森": "東北",
    "福岡": "九州", "熊本": "九州", "大分": "九州",
    "鹿児島": "九州", "長崎": "九州", "佐賀": "九州",
    "宮崎": "九州", "沖縄": "九州",
    "広島": "中国", "岡山": "中国", "山口": "中国",
    "島根": "中国", "鳥取": "中国",
    "香川": "四国", "愛媛": "四国", "徳島": "四国", "高知": "四国",
}

# area 文字列 → 都道府県 への解決（区名・市名・街区名から逆引き）
_AREA_TO_PREFECTURE: dict[str, str] = {
    # 東京23区
    "千代田": "東京", "千代田区": "東京", "中央区": "東京", "港区": "東京",
    "新宿": "東京", "新宿区": "東京", "文京": "東京", "文京区": "東京",
    "台東": "東京", "台東区": "東京", "墨田": "東京", "墨田区": "東京",
    "江東": "東京", "江東区": "東京", "品川": "東京", "品川区": "東京",
    "目黒": "東京", "目黒区": "東京", "大田": "東京", "大田区": "東京",
    "世田谷": "東京", "世田谷区": "東京", "渋谷": "東京", "渋谷区": "東京",
    "中野": "東京", "中野区": "東京", "杉並": "東京", "杉並区": "東京",
    "豊島": "東京", "豊島区": "東京", "北区": "東京",
    "荒川": "東京", "荒川区": "東京", "板橋": "東京", "板橋区": "東京",
    "練馬": "東京", "練馬区": "東京", "足立": "東京", "足立区": "東京",
    "葛飾": "東京", "葛飾区": "東京", "江戸川": "東京", "江戸川区": "東京",
    # 多摩
    "立川": "東京", "立川市": "東京", "武蔵野": "東京", "武蔵野市": "東京",
    "三鷹": "東京", "三鷹市": "東京", "府中": "東京", "府中市": "東京",
    "調布": "東京", "調布市": "東京", "町田": "東京", "町田市": "東京",
    "八王子": "東京", "八王子市": "東京", "小平市": "東京", "東村山市": "東京",
    # 東京 街区・通称
    "丸の内": "東京", "日本橋": "東京", "六本木": "東京", "赤坂": "東京",
    "麻布": "東京", "虎ノ門": "東京", "麹町": "東京", "霞が関": "東京",
    "銀座": "東京", "新橋": "東京", "勝どき": "東京", "有明": "東京",
    "池袋": "東京", "高輪": "東京", "西新宿": "東京", "京橋": "東京",
    "浅草": "東京", "千住": "東京", "大井町": "東京",
    "東京都": "東京", "東京": "東京",
    # 神奈川
    "横浜": "神奈川", "横浜市": "神奈川", "川崎": "神奈川", "川崎市": "神奈川",
    "相模原": "神奈川", "相模原市": "神奈川", "藤沢": "神奈川", "鎌倉": "神奈川",
    "神奈川": "神奈川",
    # 埼玉
    "さいたま": "埼玉", "さいたま市": "埼玉", "川口": "埼玉", "所沢": "埼玉",
    "越谷": "埼玉", "草加": "埼玉", "川越市": "埼玉", "埼玉": "埼玉",
    # 千葉
    "千葉市": "千葉", "船橋": "千葉", "市川": "千葉", "流山": "千葉",
    "柏": "千葉", "浦安": "千葉", "幕張": "千葉", "千葉": "千葉",
    # 大阪・関西
    "大阪市": "大阪", "梅田": "大阪", "難波": "大阪", "天王寺": "大阪",
    "大阪": "大阪",
    "京都市": "京都", "京都": "京都",
    "神戸市": "兵庫", "兵庫": "兵庫",
    "奈良県": "奈良", "奈良": "奈良", "和歌山": "和歌山",
    "大津市": "滋賀", "滋賀": "滋賀",
    # 中部
    "名古屋市": "愛知", "栄": "愛知", "愛知県": "愛知", "愛知": "愛知",
    "静岡市": "静岡", "静岡": "静岡", "熱海": "静岡",
    "岐阜市": "岐阜", "岐阜": "岐阜",
    "三重県": "三重", "三重": "三重",
    "松本市": "長野", "長野": "長野",
    "甲府市": "山梨", "山梨": "山梨",
    "新潟市": "新潟", "新潟": "新潟",
    "金沢市": "石川", "石川": "石川",
    "富山市": "富山", "富山": "富山",
    "福井市": "福井", "福井": "福井",
    # 北海道・東北
    "札幌市": "北海道", "北海道": "北海道",
    "仙台市": "宮城", "宮城": "宮城",
    "盛岡市": "岩手", "岩手": "岩手",
    "山形市": "山形", "山形": "山形",
    "福島市": "福島", "福島": "福島",
    "青森市": "青森", "青森": "青森",
    "水戸市": "茨城", "茨城": "茨城",
    "宇都宮市": "栃木", "栃木": "栃木",
    "前橋市": "群馬", "群馬": "群馬",
    # 中国・四国
    "広島市": "広島", "広島県": "広島", "広島": "広島",
    "岡山市": "岡山", "岡山": "岡山",
    "山口市": "山口", "山口": "山口",
    "松江市": "島根", "島根": "島根",
    "鳥取市": "鳥取", "鳥取": "鳥取",
    "高松市": "香川", "香川": "香川",
    "松山市": "愛媛", "愛媛": "愛媛",
    "徳島市": "徳島", "徳島": "徳島",
    "高知市": "高知", "高知": "高知",
    # 九州・沖縄
    "福岡市": "福岡", "天神": "福岡", "博多": "福岡", "福岡": "福岡",
    "熊本市": "熊本", "熊本": "熊本",
    "大分市": "大分", "大分": "大分",
    "鹿児島市": "鹿児島", "鹿児島": "鹿児島",
    "長崎市": "長崎", "長崎": "長崎",
    "佐賀市": "佐賀", "佐賀": "佐賀",
    "宮崎市": "宮崎", "宮崎": "宮崎",
    "那覇市": "沖縄", "沖縄": "沖縄",
}


def _classify_region_pref(area: str) -> tuple[str, str]:
    """area 文字列から (地方, 都道府県) を返す。
    判定不能なら ('その他', 'その他')."""
    if not area:
        return ("その他", "その他")
    pref = _AREA_TO_PREFECTURE.get(area, "")
    if not pref:
        # 部分一致のフォールバック（例: "横浜駅西口" → "横浜" マッチ）
        for key, val in _AREA_TO_PREFECTURE.items():
            if key in area:
                pref = val
                break
    if not pref:
        return ("その他", "その他")
    region = _PREFECTURE_TO_REGION.get(pref, "その他")
    return (region, pref)


def _effective_area(a: dict) -> str:
    """記事の有効エリアを返す。
    優先順:
      ① タイトル内の地区名（駅・街区レベル）
      ② notifier.detect_area によるキーワードマッチング（設定エリアより詳細な場合に採用）
      ③ 設定エリア（区・市レベル、都道府県/全国でない場合）
      ④ 本文頭部600字の地区名
      ⑤ タイトル＋本文から都道府県レベルで特定
      ⑥ 設定値フォールバック（全国 / その他）
    """
    from notifier import detect_area as _detect_area

    title = (a.get("title") or "").strip()
    content = (a.get("content") or a.get("summary") or "")
    configured = (a.get("area") or "").strip()

    # ① タイトルから地区名抽出（最も信頼性が高い）
    loc = _extract_location(title)
    if loc and loc not in _VAGUE_AREAS:
        return loc

    # ② notifier.detect_area による詳細マッチング
    #    設定エリアと異なる（より具体的な）地名が見つかった場合に採用
    detected = _detect_area(title, content, fallback="")
    if detected and detected != configured:
        return detected

    # ③ 設定エリアが区・市レベル（都道府県/全国でない）ならそのまま使う
    if configured and configured not in _VAGUE_AREAS:
        return configured

    # ④ 本文の最初の600文字から地区名抽出
    loc = _extract_location(content[:600])
    if loc and loc not in _VAGUE_AREAS:
        return loc

    # ⑤ 都道府県レベルで特定（地区名が取れなかった場合。「全国」より細かい）
    pref = _extract_pref(title + " " + content[:400])
    if pref:
        return pref

    return configured or "その他"


# ── プロジェクトスペック抽出 ──────────────────────────────
_SPEC_FLOORS_RE = re.compile(r'地上(\d+)階(?:・地下(\d+)階)?')
_SPEC_HEIGHT_RE = re.compile(r'高さ\s*([\d.]+)\s*(?:m|ｍ|メートル)')
_SPEC_FLOOR_AREA_RE = re.compile(r'延床面積[：:\s]*([\d,，]+)\s*(?:㎡|m²|平方メートル)')
_SPEC_SITE_AREA_RE = re.compile(r'敷地面積[：:\s]*([\d,，]+)\s*(?:㎡|m²|平方メートル)')
_SPEC_UNITS_RE = re.compile(r'(?:総戸数|戸数)[：:\s]*([\d,]+)\s*戸')

_USE_KEYWORDS = [
    ("オフィス", "🏢"), ("事務所", "🏢"),
    ("商業", "🏪"), ("店舗", "🏪"),
    ("ホテル", "🏨"), ("宿泊", "🏨"),
    ("住宅", "🏠"), ("マンション", "🏠"), ("居住", "🏠"),
    ("ホール", "🎪"), ("コンファレンス", "🎪"), ("会議場", "🎪"),
    ("物流", "📦"),
    ("データセンター", "💻"),
    ("公園", "🌳"), ("広場", "🌳"),
    ("医療", "🏥"), ("病院", "🏥"),
]


_BOILERPLATE_RE = re.compile(
    r'^(?:トップページ|ホーム|お知らせ|ニュース(?:リリース)?|プレスリリース|メニュー|'
    r'サイトマップ|お問い合わせ|アクセス|プライバシーポリシー|サイトポリシー|'
    r'リロードする|ログイン|遅延証明書|Copyright|All Rights Reserved|'
    r'PDF(?:ファイル)?をご覧|Adobe\s*Reader|Acrobat|'
    r'NEWS RELEASE|ニュースリリース\s*\d{4}年|'
    r'お持ちでない方|下記よりダウンロード|'
    r'シェアする|このページ(?:を|の)|記事をシェア|SNSでシェア|'
    r'括弧内(?:の数字|の数値)|'          # 表の注釈（都立高校等）
    r'[）\)]\s*と(?:同居|在住|連絡)|'    # 文章の断片（閉じ括弧始まり）
    r'実施校一覧|追検査入学|入学手続|'   # 学校募集ボイラープレート
    r'[（\(]PDF[：:]\s*\d+(?:KB|MB)[）\)]|'  # PDFファイルサイズ表記
    # JavaScript 使用案内（city hall/JS-heavy サイトに多い）
    r'(?:この|当)(?:サイト|ホームページ)では?[Jj]ava[Ss]cript|'
    r'[Jj]ava[Ss]cript(?:の使用)?を有効|'
    r'ブラウザの設定で[Jj]ava[Ss]cript|'
    r'お手数ですが[Jj]ava[Ss]criptの使用|'
    r'一部の機能が正確に動作しない)',
    re.IGNORECASE,
)

# enrich_content (Google News RSS) の建設関連フィルタ
_ENRICH_RELEVANT_RE = re.compile(
    r'建設|工事|着工|竣工|施工|開発|整備|改修|建替|新築|建築|ゼネコン|再開発|'
    r'不動産|業務代行|入札|落札|タワー|'
    r'区画整理|土地区画|公共工事|橋梁|港湾|'
    r'人事異動|機構改革|代表取締役|専務|常務'
)


def _to_bullets(content: str) -> list[str]:
    """コンテンツ文字列を箇条書きリストに変換する（最大8件）"""
    if not content:
        return []
    # 文章を句点・改行・全角スペースで分割
    parts = re.split(r'[。\n]', content)
    bullets = []
    for p in parts:
        p = p.strip().strip('\u3000').rstrip('。').strip()
        if len(p) < 12:
            continue
        # 断片（閉じ括弧・※・注 で始まる行はゴミ）
        if p[0] in ('）', ')', '※', '＊'):
            continue
        if p.startswith('注\u3000') or p.startswith('注 '):
            continue
        if _BOILERPLATE_RE.match(p):
            continue
        # JavaScriptが含まれる行は除外（^アンカー非マッチの場合も）
        if re.search(r'[Jj]ava[Ss]cript', p):
            continue
        if p not in bullets:
            bullets.append(p)
    return bullets[:8]


def _project_end_year(content: str) -> int:
    """竣工・完成予定の西暦年を抽出。不明なら0"""
    period = _extract_period(content)
    text = period.get("end", "")
    m = re.search(r'(20\d{2})', text)
    if m:
        return int(m.group(1))
    # 全文から「○○年度完成」「○○年竣工」パターンを検索
    m2 = re.search(r'(20\d{2})年(?:度)?(?:[^\n。]{0,8})(?:竣工|完成|開業|供用|オープン)', content)
    if m2:
        return int(m2.group(1))
    return 0


def _is_active_or_future(content: str) -> bool:
    """終了年が2026年以降、または終了年不明のプロジェクトか"""
    year = _project_end_year(content)
    if year == 0:
        return True
    return year >= 2026


def _extract_specs(content: str) -> dict:
    """記事本文からプロジェクト仕様を抽出する"""
    specs = {}
    m = _SPEC_FLOORS_RE.search(content)
    if m:
        above = m.group(1)
        below = m.group(2)
        specs["規模"] = f"地上{above}階" + (f"・地下{below}階" if below else "")
    m = _SPEC_HEIGHT_RE.search(content)
    if m:
        specs["高さ"] = f"{m.group(1)}m"
    m = _SPEC_FLOOR_AREA_RE.search(content)
    if m:
        specs["延床面積"] = f"{m.group(1)}㎡"
    m = _SPEC_SITE_AREA_RE.search(content)
    if m:
        specs["敷地面積"] = f"{m.group(1)}㎡"
    m = _SPEC_UNITS_RE.search(content)
    if m:
        specs["総戸数"] = f"{m.group(1)}戸"
    # 用途
    uses = [f"{emoji}{kw}" for kw, emoji in _USE_KEYWORDS if kw in content]
    if uses:
        specs["用途"] = " ".join(uses[:5])
    return specs

BASE_DIR = Path(__file__).parent

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>都市開発計画レポート</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif;
    background: #f5f5f5;
    color: #333;
    line-height: 1.7;
  }}
  .header {{
    background: #1a3a5c;
    color: white;
    padding: 20px 30px;
    position: sticky;
    top: 0;
    z-index: 100;
    box-shadow: 0 2px 8px rgba(0,0,0,0.3);
    display: flex;
    align-items: center;
    gap: 16px;
  }}
  .header h1 {{ font-size: 18px; font-weight: 600; }}
  .header .generated {{ font-size: 12px; opacity: 0.7; margin-left: auto; }}
  .container {{ max-width: 1000px; margin: 0 auto; padding: 24px 20px; }}

  /* 優先度バッジ */
  .priority-high   {{ background: #fff0f0; border-left: 4px solid #e53e3e; }}
  .priority-medium {{ background: #fffbf0; border-left: 4px solid #dd6b20; }}
  .priority-normal {{ background: #f0f4ff; border-left: 4px solid #3182ce; }}

  /* 見出し */
  h1 {{ font-size: 26px; color: #1a3a5c; margin: 24px 0 8px; }}
  h2 {{ font-size: 20px; color: #2d6a9f; margin: 32px 0 12px; padding-bottom: 6px; border-bottom: 2px solid #2d6a9f; }}
  h3 {{ font-size: 16px; color: #444; margin: 20px 0 8px; }}
  h4 {{ font-size: 15px; margin: 0 0 8px; }}

  /* 記事カード */
  h4 a {{
    color: #1a56a0;
    text-decoration: none;
    font-weight: 600;
  }}
  h4 a:hover {{ text-decoration: underline; }}

  /* リスト */
  ul {{ padding-left: 20px; margin: 6px 0 10px; }}
  li {{ margin: 4px 0; font-size: 14px; }}

  /* 引用（概要） */
  blockquote {{
    background: rgba(0,0,0,0.04);
    border-left: 3px solid #ccc;
    padding: 8px 14px;
    margin: 8px 0;
    font-size: 13px;
    color: #555;
    border-radius: 0 4px 4px 0;
  }}

  /* テーブル */
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }}
  th {{ background: #1a3a5c; color: white; padding: 8px 12px; text-align: left; }}
  td {{ padding: 7px 12px; border-bottom: 1px solid #e0e0e0; }}
  tr:hover td {{ background: #f0f4ff; }}

  /* セクション区切り */
  hr {{ border: none; border-top: 1px solid #ddd; margin: 24px 0; }}

  /* フッター */
  em {{ color: #888; font-size: 12px; }}

  /* レスポンシブ */
  @media (max-width: 600px) {{
    .header {{ padding: 14px 16px; }}
    .container {{ padding: 16px 12px; }}
    h1 {{ font-size: 20px; }}
  }}

  /* 重要度セクションの背景 */
  .section-high   {{ background: #fff5f5; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .section-medium {{ background: #fffaf0; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
</style>
</head>
<body>
<div class="header">
  <span style="font-size:24px">🏙️</span>
  <h1>都市開発計画 情報レポート</h1>
  <span class="generated">生成: {generated}</span>
</div>
<div class="container">
{body}
</div>
</body>
</html>"""


def render_html(md_path: Path) -> str:
    """Markdownファイルを読み込んでHTML文字列を返す"""
    with open(md_path, encoding="utf-8") as f:
        md_text = f.read()

    body = markdown.markdown(
        md_text,
        extensions=["tables", "nl2br", "sane_lists"],
    )

    generated = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    return HTML_TEMPLATE.format(body=body, generated=generated)


def open_in_browser(md_path: Path = None):
    """レポートをHTMLに変換してブラウザで開く"""
    if md_path is None:
        md_path = BASE_DIR / "reports" / "latest.md"

    if not md_path.exists():
        print(f"レポートファイルが見つかりません: {md_path}")
        print("先に以下を実行してください:")
        print("  python3 main.py fetch")
        return

    html = render_html(md_path)

    # 一時HTMLファイルに書き出してブラウザで開く
    tmp = tempfile.NamedTemporaryFile(
        suffix=".html",
        delete=False,
        mode="w",
        encoding="utf-8",
        prefix="urban_dev_report_",
    )
    tmp.write(html)
    tmp.close()

    print(f"ブラウザで開いています: {tmp.name}")
    subprocess.run(["open", tmp.name])


RICH_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>都市開発情報</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🏗️</text></svg>">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", sans-serif;
    background: #eef2f7;
    color: #222;
    line-height: 1.7;
  }}
  .header {{
    background: linear-gradient(135deg, #1a3a5c, #2d6a9f);
    color: white;
    padding: 16px 24px;
    position: sticky;
    top: 0;
    z-index: 200;
    box-shadow: 0 3px 12px rgba(0,0,0,0.3);
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .header h1 {{ font-size: 18px; font-weight: 700; }}
  .header .meta {{ font-size: 11px; opacity: 0.75; margin-left: auto; }}
  /* ── フィルターバー ── */
  .filter-bar {{
    background: #fff;
    border-bottom: 1px solid #d0daea;
    padding: 8px 24px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    position: sticky;
    top: 53px;
    z-index: 190;
    font-size: 12px;
  }}
  .fb-label {{ color: #666; font-weight: 600; white-space: nowrap; }}
  .fb-sep {{ color: #ccc; padding: 0 4px; }}
  .fbtn {{
    background: #f4f7fc;
    border: 1px solid #c8d4e8;
    border-radius: 14px;
    padding: 3px 12px;
    font-size: 12px;
    cursor: pointer;
    color: #445;
    white-space: nowrap;
    transition: .15s;
  }}
  .fbtn:hover {{ background: #e2eaf6; }}
  .fbtn.active {{ background: #1e3a6e; color: #fff; border-color: #1e3a6e; }}
  .filter-count {{ margin-left: auto; font-size: 11px; color: #888; }}
  /* ── エリアナビ ── */
  .area-nav {{
    background: #f0f4fb;
    border-bottom: 1px solid #d0daea;
    padding: 6px 24px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    position: sticky;
    top: 102px;
    z-index: 180;
  }}
  .area-btn {{
    font-size: 11px;
    background: #fff;
    border: 1px solid #c8d4e8;
    border-radius: 12px;
    padding: 2px 10px;
    cursor: pointer;
    color: #334;
    white-space: nowrap;
    transition: .15s;
  }}
  .area-btn:hover {{ background: #e2eaf6; }}
  .area-btn.active {{ background: #1e3a6e; color: #fff; border-color: #1e3a6e; }}
  /* ── カードグリッド ── */
  .container {{ max-width: 1200px; margin: 0 auto; padding: 20px 20px 40px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px; }}
  .card {{
    background: white;
    border-radius: 10px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.07);
    padding: 14px 16px 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-left: 4px solid #3182ce;
    transition: box-shadow 0.2s;
  }}
  .card:hover {{ box-shadow: 0 5px 18px rgba(0,0,0,0.12); }}
  .card-top {{
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }}
  .chip {{
    font-size: 11px;
    background: #eef2f7;
    color: #1a3a5c;
    padding: 2px 8px;
    border-radius: 12px;
    font-weight: 600;
  }}
  .date {{ font-size: 11px; color: #999; margin-left: auto; }}
  .card-title {{
    font-size: 14px;
    font-weight: 700;
    line-height: 1.45;
  }}
  .card-title a {{ color: #1a3a5c; text-decoration: none; }}
  .card-title a:hover {{ color: #2d6a9f; text-decoration: underline; }}
  .card-source {{ font-size: 11px; color: #888; }}
  .card-content {{ flex-grow: 1; }}
  .card-bullets {{
    margin: 2px 0 0 0;
    padding-left: 14px;
    font-size: 12px;
    color: #444;
    line-height: 1.65;
    list-style: disc;
  }}
  .card-bullets li {{ margin-bottom: 1px; }}
  .enrich-note {{ font-size: 11px; color: #aaa; margin-top: 2px; }}
  .enrich-note a {{ color: #aaa; text-decoration: none; }}
  .enrich-note a:hover {{ text-decoration: underline; }}
  .card-footer {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-top: 2px;
  }}
  .btn-source {{
    display: inline-block;
    font-size: 11px;
    background: #1a3a5c;
    color: white;
    padding: 4px 12px;
    border-radius: 16px;
    text-decoration: none;
    font-weight: 600;
    transition: background 0.2s;
  }}
  .btn-source:hover {{ background: #2d6a9f; }}
  .btn-gnews {{
    display: inline-block;
    font-size: 11px;
    background: #1a6e3c;
    color: white;
    padding: 4px 12px;
    border-radius: 16px;
    text-decoration: none;
    font-weight: 600;
    transition: background 0.2s;
    margin-left: 6px;
  }}
  .btn-gnews:hover {{ background: #28a35a; }}
  .tags {{ display: flex; gap: 3px; flex-wrap: wrap; }}
  .tag {{
    font-size: 10px;
    background: #f0f4ff;
    color: #2d6a9f;
    padding: 1px 7px;
    border-radius: 8px;
  }}
  .card.hidden {{ display: none; }}
  .empty {{ color: #999; font-size: 14px; padding: 40px; text-align: center; }}
  footer {{ text-align: center; padding: 20px; font-size: 11px; color: #bbb; }}
  @media (max-width: 600px) {{
    .header {{ padding: 12px 14px; }}
    .cards {{ grid-template-columns: 1fr; }}
    .filter-bar, .area-nav {{ top: unset; position: static; }}
  }}
  /* ── パスワードゲート ── */
  #pw-overlay {{
    position: fixed; inset: 0; z-index: 9999;
    background: rgba(15,32,68,0.97);
    display: flex; align-items: center; justify-content: center;
  }}
  .pw-box {{
    background: #fff; border-radius: 14px;
    padding: 44px 40px 36px; max-width: 380px; width: 90%;
    text-align: center; box-shadow: 0 24px 60px rgba(0,0,0,0.5);
  }}
  .pw-logo {{ font-size: 44px; margin-bottom: 10px; }}
  .pw-box h2 {{ font-size: 20px; font-weight: 700; color: #1a3a5c; margin-bottom: 4px; }}
  .pw-box p {{ font-size: 13px; color: #888; margin-bottom: 28px; }}
  .pw-input {{
    width: 100%; padding: 11px 14px; box-sizing: border-box;
    border: 1.5px solid #c8d4e8; border-radius: 8px;
    font-size: 15px; outline: none; margin-bottom: 12px;
    transition: border-color .15s, box-shadow .15s;
  }}
  .pw-input:focus {{ border-color: #2d6a9f; box-shadow: 0 0 0 3px rgba(45,106,159,0.15); }}
  .pw-btn {{
    width: 100%; padding: 11px; background: #1a3a5c; color: white;
    border: none; border-radius: 8px; font-size: 15px;
    font-weight: 700; cursor: pointer; transition: background .2s;
  }}
  .pw-btn:hover {{ background: #2d6a9f; }}
  .pw-error {{ color: #e74c3c; font-size: 13px; margin-top: 10px; min-height: 18px; }}
  /* ── 新レイアウト: ピン留め (本日/昨日) ── */
  .section-title {{
    font-size: 13px; font-weight: 700; color: #1a3a5c;
    margin: 22px 4px 8px; display: flex; align-items: center; gap: 6px;
    letter-spacing: 0.02em;
  }}
  .section-title:first-of-type {{ margin-top: 8px; }}
  .pinned-tabs {{ display: flex; gap: 6px; margin: 0 4px 12px; flex-wrap: wrap; }}
  .ptab {{
    background: #fff; border: 1px solid #c8d4e8; border-radius: 18px;
    padding: 5px 16px; font-size: 13px; cursor: pointer; color: #1a3a5c;
    font-weight: 600; transition: .15s;
  }}
  .ptab:hover {{ background: #e2eaf6; }}
  .ptab.active {{ background: #1a3a5c; color: #fff; border-color: #1a3a5c; }}
  .ptab .pcount {{
    font-size: 10px; background: rgba(0,0,0,0.12); color: inherit;
    padding: 1px 7px; border-radius: 10px; margin-left: 5px;
  }}
  .ptab.active .pcount {{ background: rgba(255,255,255,0.25); }}
  .pinned-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 14px; margin-bottom: 8px;
  }}
  .pinned-grid.hidden {{ display: none; }}
  .pinned-empty {{ color: #888; font-size: 12px; padding: 16px; text-align: center; background: #f7f9fc; border-radius: 8px; }}
  /* ── 地方ボタン行 ── */
  .region-row {{
    display: flex; gap: 6px; flex-wrap: wrap; margin: 0 4px 8px;
  }}
  .region-btn {{
    background: #fff; border: 1.5px solid #c8d4e8; border-radius: 16px;
    padding: 5px 14px; font-size: 12px; cursor: pointer;
    color: #1a3a5c; font-weight: 600; transition: .15s;
  }}
  .region-btn:hover {{ background: #e2eaf6; }}
  .region-btn.active {{ background: #2d6a9f; color: #fff; border-color: #2d6a9f; }}
  .region-btn .rcount {{
    font-size: 10px; background: rgba(0,0,0,0.10); color: inherit;
    padding: 1px 6px; border-radius: 10px; margin-left: 4px;
  }}
  .region-btn.active .rcount {{ background: rgba(255,255,255,0.25); }}
  /* ── 都道府県サブタブ行 ── */
  .pref-row {{
    display: flex; gap: 5px; flex-wrap: wrap; margin: 0 4px 10px;
    padding: 6px 8px; background: #f4f7fc; border-radius: 8px;
    border: 1px dashed #c8d4e8;
  }}
  .pref-row.hidden {{ display: none; }}
  .pref-btn {{
    background: #fff; border: 1px solid #d0daea; border-radius: 12px;
    padding: 3px 11px; font-size: 11px; cursor: pointer; color: #334;
    transition: .15s;
  }}
  .pref-btn:hover {{ background: #e2eaf6; }}
  .pref-btn.active {{ background: #1e3a6e; color: #fff; border-color: #1e3a6e; }}
  .pref-btn .pfcount {{ font-size: 9px; opacity: 0.7; margin-left: 3px; }}
  /* ── period filter: place sticky right under header ── */
  .period-bar {{ top: 53px; }}
</style>
</head>
<body>
<!-- パスワードゲート -->
<div id="pw-overlay">
  <div class="pw-box">
    <div class="pw-logo">🏙️</div>
    <h2>都市開発情報</h2>
    <p>メンバー専用ページです。<br>パスワードを入力してください。</p>
    <input id="pw-input" class="pw-input" type="password"
           placeholder="パスワード" autocomplete="current-password"
           onkeydown="if(event.key==='Enter')pwCheck()">
    <button class="pw-btn" onclick="pwCheck()">ログイン</button>
    <div id="pw-error" class="pw-error"></div>
  </div>
</div>
<div class="header">
  <span style="font-size:24px">🏙️</span>
  <h1>都市開発情報</h1>
  <div class="meta">生成: {generated} &nbsp;|&nbsp; {total}件</div>
</div>
<!-- 期間フィルター -->
<div class="filter-bar period-bar" id="period-bar">
  <span class="fb-label">📅 期間</span>
  <button class="fbtn fbtn-days active" data-days="0">全期間</button>
  <button class="fbtn fbtn-days" data-days="7">1週間</button>
  <button class="fbtn fbtn-days" data-days="30">1ヶ月</button>
  <button class="fbtn fbtn-days" data-days="90">3ヶ月</button>
  <span class="filter-count" id="filter-count"></span>
</div>
<div class="container">
  <!-- 📰 本日・昨日の最新情報（常時TOP表示） -->
  <h2 class="section-title">📰 最新</h2>
  <div class="pinned-tabs">
    <button class="ptab active" data-target="today">本日<span class="pcount" id="today-count">0</span></button>
    <button class="ptab" data-target="yesterday">昨日<span class="pcount" id="yesterday-count">0</span></button>
  </div>
  <div class="pinned-grid" id="pinned-today"><div class="pinned-empty">本日の新着はまだありません</div></div>
  <div class="pinned-grid hidden" id="pinned-yesterday"><div class="pinned-empty">昨日の新着はありませんでした</div></div>

  <!-- 🗾 地方→都道府県 ナビゲーション -->
  <h2 class="section-title">🗾 エリア別</h2>
  <div class="region-row" id="region-row"><!-- JS で地方ボタン生成 --></div>
  <div class="pref-row hidden" id="pref-row"><!-- 地方選択時に都道府県サブタブを生成 --></div>

  <div class="cards" id="cards-grid">
{body}
  </div>
</div>
<footer>urban-dev-tracker — {generated}</footer>
<!-- AES暗号化ペイロード（password がセットされている場合のみ中身あり） -->
<script id="enc-data" type="application/json">{enc_blob}</script>
<script>
// ── パスワードゲート / AES復号 ──
// モード判定:
//   暗号化モード: enc-data に JSON あり → AES-GCM 復号成功で解錠
//   レガシーモード: password_hash のみ → SHA-256比較（カードは既に表示済み）
//   無設定: そのまま表示
var __PWHASH = '{password_hash}';

function __getEnc() {{
  var t = (document.getElementById('enc-data') || {{}}).textContent || '';
  t = t.trim();
  if (!t) return null;
  try {{ return JSON.parse(t); }} catch(e) {{ return null; }}
}}

function __b64ToBytes(s) {{
  var raw = atob(s);
  var bytes = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}}

async function __deriveKey(password, saltBytes, iters) {{
  var baseKey = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(password),
    {{name:'PBKDF2'}}, false, ['deriveKey']
  );
  return await crypto.subtle.deriveKey(
    {{name:'PBKDF2', salt:saltBytes, iterations:iters, hash:'SHA-256'}},
    baseKey, {{name:'AES-GCM', length:256}}, false, ['decrypt']
  );
}}

async function __tryDecrypt(password) {{
  var enc = __getEnc();
  if (!enc) return null;
  var salt = __b64ToBytes(enc.salt);
  var iv   = __b64ToBytes(enc.iv);
  var ct   = __b64ToBytes(enc.ct);
  var key  = await __deriveKey(password, salt, enc.iter);
  var plain = await crypto.subtle.decrypt({{name:'AES-GCM', iv:iv}}, key, ct);
  return JSON.parse(new TextDecoder().decode(plain));
}}

function __injectDecrypted(data) {{
  var grid = document.getElementById('cards-grid');
  if (grid && data.body) grid.innerHTML = data.body;
  // 新レイアウトでは area_btns は使わず、JS が card[data-region] から動的生成
  if (typeof initLayout === 'function') initLayout();
}}

(async function() {{
  var el = document.getElementById('pw-overlay');
  var enc = __getEnc();

  // 暗号化モードでもレガシーゲートでもない → そのまま表示
  if (!enc && !__PWHASH) {{ if (el) el.remove(); return; }}

  if (enc) {{
    // 暗号化モード: sessionStorage に直近の正解パスワードがあれば自動復号
    var cached = sessionStorage.getItem('udt_pw');
    if (cached) {{
      try {{
        var data = await __tryDecrypt(cached);
        __injectDecrypted(data);
        if (el) el.remove();
        document.body.style.overflow = '';
        return;
      }} catch(e) {{ sessionStorage.removeItem('udt_pw'); }}
    }}
  }} else if (__PWHASH) {{
    // レガシーモード: SHA-256キャッシュ確認
    var stored = localStorage.getItem('udt_' + __PWHASH.slice(0,8));
    if (stored === __PWHASH) {{ if (el) el.remove(); return; }}
  }}

  if (el) {{
    document.body.style.overflow = 'hidden';
    setTimeout(function() {{ document.getElementById('pw-input').focus(); }}, 100);
  }}
}})();

async function pwCheck() {{
  var pw = document.getElementById('pw-input').value;
  var enc = __getEnc();
  var errEl = document.getElementById('pw-error');
  var fail = function() {{
    errEl.textContent = 'パスワードが違います';
    document.getElementById('pw-input').value = '';
    document.getElementById('pw-input').focus();
  }};

  if (enc) {{
    // 暗号化モード: 復号が成功すれば正解
    try {{
      var data = await __tryDecrypt(pw);
      sessionStorage.setItem('udt_pw', pw);
      __injectDecrypted(data);
      document.getElementById('pw-overlay').remove();
      document.body.style.overflow = '';
    }} catch(e) {{ fail(); }}
    return;
  }}

  // レガシーモード: SHA-256 ハッシュ比較
  var buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(pw));
  var hex = Array.from(new Uint8Array(buf)).map(function(b) {{ return b.toString(16).padStart(2,'0'); }}).join('');
  if (hex === __PWHASH) {{
    localStorage.setItem('udt_' + __PWHASH.slice(0,8), __PWHASH);
    document.getElementById('pw-overlay').remove();
    document.body.style.overflow = '';
  }} else {{
    fail();
  }}
}}
// ── 新レイアウト: ピン留め + 地方/都道府県 + 期間フィルタ ──
var _days = 0;
var _region = '';
var _pref = '';

function __dateStr(t) {{
  var y = t.getFullYear();
  var m = String(t.getMonth()+1).padStart(2,'0');
  var d = String(t.getDate()).padStart(2,'0');
  return y + '-' + m + '-' + d;
}}

// 地方の表示順（記事0件のものは非表示）
var __REGION_ORDER = ['関東','関西','中部','東北','北海道','九州','中国','四国','その他'];

function buildPinned() {{
  var today = new Date();
  var yesterday = new Date(today.getTime() - 86400000);
  var todayStr = __dateStr(today);
  var yesterdayStr = __dateStr(yesterday);

  var allCards = document.querySelectorAll('#cards-grid .card');
  var todayCards = [], yestCards = [];
  allCards.forEach(function(c) {{
    var d = c.dataset.date || '';
    if (d === todayStr) todayCards.push(c);
    else if (d === yesterdayStr) yestCards.push(c);
  }});

  var tBox = document.getElementById('pinned-today');
  var yBox = document.getElementById('pinned-yesterday');
  tBox.innerHTML = ''; yBox.innerHTML = '';
  if (todayCards.length === 0) {{
    tBox.innerHTML = '<div class="pinned-empty">本日の新着はまだありません</div>';
  }} else {{
    todayCards.forEach(function(c) {{ tBox.appendChild(c.cloneNode(true)); }});
  }}
  if (yestCards.length === 0) {{
    yBox.innerHTML = '<div class="pinned-empty">昨日の新着はありませんでした</div>';
  }} else {{
    yestCards.forEach(function(c) {{ yBox.appendChild(c.cloneNode(true)); }});
  }}
  document.getElementById('today-count').textContent = todayCards.length;
  document.getElementById('yesterday-count').textContent = yestCards.length;
}}

function selectPinnedTab(target) {{
  document.querySelectorAll('.ptab').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.target === target);
  }});
  document.getElementById('pinned-today').classList.toggle('hidden', target !== 'today');
  document.getElementById('pinned-yesterday').classList.toggle('hidden', target !== 'yesterday');
}}

function buildRegionNav() {{
  // data-region 属性をもとに記事数を集計
  var regionStats = {{}};
  document.querySelectorAll('#cards-grid .card').forEach(function(c) {{
    var r = c.dataset.region || 'その他';
    var p = c.dataset.prefecture || 'その他';
    if (!regionStats[r]) regionStats[r] = {{ count: 0, prefs: {{}} }};
    regionStats[r].count++;
    regionStats[r].prefs[p] = (regionStats[r].prefs[p] || 0) + 1;
  }});

  var row = document.getElementById('region-row');
  row.innerHTML = '';
  __REGION_ORDER.forEach(function(r) {{
    if (!regionStats[r]) return;
    var btn = document.createElement('button');
    btn.className = 'region-btn';
    btn.dataset.region = r;
    btn.innerHTML = r + '<span class="rcount">' + regionStats[r].count + '</span>';
    btn.onclick = function() {{ selectRegion(r); }};
    row.appendChild(btn);
  }});
  // グローバルに保持（pref-row 構築時に再利用）
  window.__regionStats = regionStats;
}}

function buildPrefRow() {{
  var prefRow = document.getElementById('pref-row');
  prefRow.innerHTML = '';
  if (!_region || !window.__regionStats || !window.__regionStats[_region]) {{
    prefRow.classList.add('hidden');
    return;
  }}
  prefRow.classList.remove('hidden');
  var prefs = window.__regionStats[_region].prefs;
  var entries = Object.keys(prefs).map(function(k) {{ return [k, prefs[k]]; }});
  entries.sort(function(a, b) {{ return b[1] - a[1]; }});
  // 「全表示」ボタン
  var allBtn = document.createElement('button');
  allBtn.className = 'pref-btn' + (_pref === '' ? ' active' : '');
  allBtn.dataset.prefecture = '';
  allBtn.innerHTML = _region + 'すべて';
  allBtn.onclick = function() {{ selectPref(''); }};
  prefRow.appendChild(allBtn);
  entries.forEach(function(e) {{
    var btn = document.createElement('button');
    btn.className = 'pref-btn' + (_pref === e[0] ? ' active' : '');
    btn.dataset.prefecture = e[0];
    btn.innerHTML = e[0] + '<span class="pfcount">' + e[1] + '</span>';
    btn.onclick = function() {{ selectPref(e[0]); }};
    prefRow.appendChild(btn);
  }});
}}

function selectRegion(r) {{
  _region = (_region === r) ? '' : r;
  _pref = '';
  document.querySelectorAll('.region-btn').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.region === _region);
  }});
  buildPrefRow();
  applyFilter();
}}

function selectPref(p) {{
  _pref = p;
  document.querySelectorAll('.pref-btn').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.prefecture === _pref);
  }});
  applyFilter();
}}

function setDays(d) {{
  _days = d;
  document.querySelectorAll('#period-bar .fbtn-days').forEach(function(b) {{
    b.classList.toggle('active', Number(b.dataset.days) === d);
  }});
  applyFilter();
}}

function applyFilter() {{
  var visible = 0;
  var cutoff = '';
  if (_days > 0) {{
    var cd = new Date(Date.now() - _days * 86400000);
    cutoff = __dateStr(cd);
  }}
  document.querySelectorAll('#cards-grid .card').forEach(function(c) {{
    var d = c.dataset.date || '';
    var r = c.dataset.region || 'その他';
    var p = c.dataset.prefecture || 'その他';
    var show = true;
    if (cutoff && d < cutoff) show = false;
    if (_region && r !== _region) show = false;
    if (_pref && p !== _pref) show = false;
    c.classList.toggle('hidden', !show);
    if (show) visible++;
  }});
  var el = document.getElementById('filter-count');
  if (el) el.textContent = visible + '件表示中';
}}

function initLayout() {{
  buildPinned();
  buildRegionNav();
  applyFilter();
}}

// イベントハンドラ束ねて登録
document.querySelectorAll('.ptab').forEach(function(b) {{
  b.addEventListener('click', function() {{ selectPinnedTab(b.dataset.target); }});
}});
document.querySelectorAll('#period-bar .fbtn-days').forEach(function(b) {{
  b.addEventListener('click', function() {{ setDays(Number(b.dataset.days)); }});
}});

// 暗号化モードでない場合（cards がすでに DOM 内）は即初期化
// 暗号化モードでは __injectDecrypted() が呼ぶ
if (document.querySelectorAll('#cards-grid .card').length > 0) {{
  initLayout();
}}
</script>
</body>
</html>"""


def _card_html(a: dict) -> str:
    area = _effective_area(a)
    title = _clean_title(a.get("title", "（タイトルなし）").replace("【更新検知】", "").strip())
    url = a.get("url", "#")
    source_id = a.get("source_id", "")
    source = a.get("source_name", "")
    tags = a.get("tags", [])
    published = (
        _parse_pub_date(a.get("published_at") or "")
        or _pub_date_from_title(a.get("title") or "")
        or (a.get("fetched_at") or "")[:10]
    )
    content = (a.get("content") or a.get("summary") or "").strip()
    enrich_content = (a.get("enrich_content") or "").strip()
    enrich_source = (a.get("enrich_source") or "").strip()

    import html as _html

    # コンテンツを箇条書きに変換（最大3件・40字）
    _title_norm = re.sub(r"\s", "", _clean_title(title))
    if content:
        raw_bullets = _to_bullets(content)
        bullets = []
        for b in raw_bullets:
            b_norm = re.sub(r"\s", "", b)
            if b_norm in _title_norm or _title_norm in b_norm:
                continue
            bullets.append(b[:40] + ("…" if len(b) > 40 else ""))
            if len(bullets) >= 3:
                break
        if not bullets and enrich_content:
            content = ""
    if not content and enrich_content:
        is_gnews = enrich_content.startswith("【関連報道】")
        raw_lines = [l.strip().lstrip("・") for l in enrich_content.splitlines()
                     if l.strip() and l.strip() != "【関連報道】"]
        if is_gnews:
            # Google News RSS 由来: 建設関連のみ採用（無関係ニュースを除外）
            filtered = [l for l in raw_lines if _ENRICH_RELEVANT_RE.search(l)]
        else:
            # DB内クロスリファレンス由来: フィルターなし
            filtered = raw_lines

        def _alphanum(s: str) -> str:
            return re.sub(r'[^\w]', '', s, flags=re.UNICODE)

        _title_alnum = _alphanum(_title_norm)  # /・-等も除去してalphanumで比較
        deduped: list[str] = []
        for line in filtered:
            # タイトルと内容が類似する行は除外（kensetsunews記事自身がRSSに出る場合）
            line_norm = _alphanum(line)
            if _title_alnum and (
                line_norm[:20] in _title_alnum or _title_alnum[:20] in line_norm
            ):
                continue
            # 既存bulletと先頭15文字が一致する場合は重複とみなし除外
            prefix = _alphanum(line)[:15]
            if any(_alphanum(d)[:15] == prefix for d in deduped):
                continue
            deduped.append(line)
            if len(deduped) >= 3:
                break

        bullets = [l[:40] + ("…" if len(l) > 40 else "") for l in deduped]
    elif content:
        pass
    else:
        bullets = []

    if bullets:
        items = "".join(f"<li>{_html.escape(b)}</li>" for b in bullets)
        content_html = f'<ul class="card-bullets">{items}</ul>'
    else:
        content_html = ""

    title_safe = _html.escape(title)
    source_safe = _html.escape(source)
    area_safe = _html.escape(area)
    area_data = _html.escape(area)

    tags_html = "".join(f'<span class="tag">{_html.escape(t)}</span>' for t in tags)

    enrich_note = ""
    if enrich_content and enrich_source:
        enrich_safe = _html.escape(enrich_source)
        link_label = "Google Newsで関連報道を検索" if "news.google.com" in enrich_source else _html.escape(enrich_source[:40])
        enrich_note = (
            f'<div class="enrich-note">📎 '
            f'<a href="{enrich_safe}" target="_blank" rel="noopener">{link_label}</a></div>'
        )

    # kensetsunews は有料記事なのでGoogle Newsの関連検索リンクを追加
    gnews_btn = ""
    if source_id.startswith("kensetsunews"):
        import urllib.parse as _up
        gnews_q = _up.quote(title)
        gnews_url = f"https://news.google.com/search?q={gnews_q}&hl=ja&gl=JP&ceid=JP%3Aja"
        gnews_btn = f'<a class="btn-gnews" href="{gnews_url}" target="_blank" rel="noopener">Google News で関連記事 →</a>'

    # 地方・都道府県を判定（フロントのナビ用）
    _region, _pref = _classify_region_pref(area)
    region_data = _html.escape(_region, quote=True)
    pref_data = _html.escape(_pref, quote=True)

    return f"""<div class="card" data-area="{area_data}" data-region="{region_data}" data-prefecture="{pref_data}" data-date="{published}">
  <div class="card-top">
    <span class="chip">{area_safe}</span>
    <span class="date">{published}</span>
  </div>
  <div class="card-title"><a href="{url}" target="_blank" rel="noopener">{title_safe}</a></div>
  <div class="card-source">{source_safe}</div>
  <div class="card-content">{content_html}</div>
  {enrich_note}
  <div class="card-footer">
    <a class="btn-source" href="{url}" target="_blank" rel="noopener">元記事を読む →</a>
    {gnews_btn}
    <div class="tags">{tags_html}</div>
  </div>
</div>"""


def _encrypt_html_payload(plaintext: str, password: str, iterations: int = 200_000) -> dict:
    """AES-GCM-256 + PBKDF2-HMAC-SHA256 で plaintext を暗号化し、
    salt / iv / ct / iter を base64 で含む dict を返す。
    ブラウザの crypto.subtle と互換のパラメータを使用。"""
    import os as _os
    import base64 as _b64
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = _os.urandom(16)
    iv = _os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=salt, iterations=iterations)
    key = kdf.derive(password.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return {
        "salt": _b64.b64encode(salt).decode(),
        "iv":   _b64.b64encode(iv).decode(),
        "ct":   _b64.b64encode(ct).decode(),
        "iter": iterations,
    }


def generate_rich_html(articles: list[dict], password_hash: str = "", password: str = "") -> str:
    """記事リストからカード形式のリッチHTMLを生成する"""
    import html as _html
    from datetime import datetime

    # 関連性フィルタ: 都市開発無関係を除外
    from notifier import is_development_relevant as _is_dev_relevant, _BAD_TITLE_KEYWORDS, _BAD_TITLE_RE
    _CONSTR_SOURCE_RE = re.compile(r'^kensetsunews')

    def _is_relevant(a: dict) -> bool:
        # kensetsunews は建設業界専門誌なので DEV_KEYWORDS/コンテンツ品質チェックを緩和
        # 悪タイトルキーワード・パターンのみで除外判定
        if _CONSTR_SOURCE_RE.match(a.get("source_id") or ""):
            title = (a.get("title") or "").strip()
            if len(title) < 12:
                return False
            for kw in _BAD_TITLE_KEYWORDS:
                if kw in title:
                    return False
            if _BAD_TITLE_RE.search(title):
                return False
            # kensetsunews は悪タイトル以外はすべて表示（bullet不要）
            return True
        return _is_dev_relevant(a)

    articles = [a for a in articles if _is_relevant(a)]

    # タイトルが実質同一の記事を除去（同一ニュースの複数フィード掲載対策）
    import unicodedata as _ud

    def _tnorm(t: str) -> str:
        """NFKC正規化 + 英数字・漢字等のみ残す"""
        return re.sub(r'[^\w]', '', _ud.normalize('NFKC', t or ''), flags=re.UNICODE)

    def _is_title_dup(n1: str, n2: str, win: int = 9) -> bool:
        """どちらかが win 文字以上の共通部分文字列を持てば重複"""
        s, lo = (n1, n2) if len(n1) <= len(n2) else (n2, n1)
        if len(s) < win:
            return False
        for i in range(len(s) - win + 1):
            if s[i:i + win] in lo:
                return True
        return False

    deduped_articles: list[dict] = []
    seen_norms: list[str] = []
    for a in articles:
        norm = _tnorm(a.get('title', ''))
        if any(_is_title_dup(norm, n) for n in seen_norms):
            continue
        deduped_articles.append(a)
        seen_norms.append(norm)
    articles = deduped_articles

    # 日付の新しい順に並べる
    def _sort_date(a):
        return (
            _parse_pub_date(a.get("published_at") or "")
            or _pub_date_from_title(a.get("title") or "")
            or (a.get("fetched_at") or "")[:10]
            or ""
        )

    sorted_articles = sorted(articles, key=_sort_date, reverse=True)

    if not sorted_articles:
        body = '<p class="empty">表示できる記事がありません。先に <code>python3 main.py crawl</code> を実行してください。</p>'
        area_btns = ""
    else:
        body = "\n".join(_card_html(a) for a in sorted_articles)
        # ユニークエリアをボタン化（記事数降順、全国/その他は除外）
        from collections import Counter
        area_counts = Counter(
            _effective_area(a) for a in sorted_articles
            if _effective_area(a) not in ("全国", "その他", "")
        )
        area_btns = "".join(
            f'<button class="fbtn area-btn" data-area="{_html.escape(ar)}" onclick="setArea(\'{_html.escape(ar)}\')">'
            f'{_html.escape(ar)}</button>'
            for ar, _ in area_counts.most_common()
        )

    generated = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # ── 暗号化モード ──
    # password が与えられた場合、本文 (body) とエリアボタン (area_btns) を
    # AES-GCM で暗号化して埋め込む。HTMLソースを見ても暗号文しか露出しない。
    if password:
        import json as _json
        payload_obj = {"body": body, "area_btns": area_btns}
        enc = _encrypt_html_payload(
            _json.dumps(payload_obj, ensure_ascii=False),
            password,
        )
        enc_blob_json = _json.dumps(enc, ensure_ascii=False)
        # 本文プレースホルダは空にする
        body_out = '<!-- encrypted -->'
        area_btns_out = '<!-- encrypted -->'
        # 旧ハッシュは渡さない（暗号化モードでは復号成功=正解判定）
        password_hash_out = ""
    else:
        enc_blob_json = ""
        body_out = body
        area_btns_out = area_btns
        password_hash_out = password_hash

    return RICH_TEMPLATE.format(
        generated=generated,
        total=len(articles),
        area_btns=area_btns_out,
        body=body_out,
        password_hash=password_hash_out,
        enc_blob=enc_blob_json,
    )


def open_rich_browser(articles: list[dict]):
    """カード形式HTMLをブラウザで開く"""
    html = generate_rich_html(articles)
    tmp = tempfile.NamedTemporaryFile(
        suffix=".html",
        delete=False,
        mode="w",
        encoding="utf-8",
        prefix="urban_dev_rich_",
    )
    tmp.write(html)
    tmp.close()
    print(f"ブラウザで開いています: {tmp.name}")
    subprocess.run(["open", tmp.name])


AREA_TIMELINE_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>都市開発計画 エリア別一覧</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif;background:#eef2f7;color:#1a1a2e;line-height:1.7;font-size:14px}}
/* ヘッダー */
.hdr{{background:linear-gradient(135deg,#0f2044,#1e4d8c);color:#fff;padding:16px 28px;position:sticky;top:0;z-index:200;box-shadow:0 3px 14px rgba(0,0,0,.35);display:flex;align-items:center;gap:12px}}
.hdr h1{{font-size:18px;font-weight:700;letter-spacing:.03em}}
.hdr .meta{{margin-left:auto;font-size:11px;opacity:.75;text-align:right;line-height:1.6}}
/* 統計バー */
.stats{{background:#fff;border-bottom:1px solid #d0daea;padding:8px 28px;display:flex;flex-wrap:wrap;gap:18px;font-size:12px;color:#555;position:sticky;top:var(--stats-top,52px);z-index:195}}
.sn{{font-weight:700;font-size:13px;padding:0 2px}}
.sc{{color:#27ae60}} .sw{{color:#e67e22}} .sp{{color:#8e44ad}} .sl{{color:#2980b9}} .si{{color:#7f8c8d}}
/* エリアナビ：横スクロール1行固定 */
.anav{{background:#1e3a6e;padding:8px 16px;display:flex;flex-wrap:nowrap;gap:6px;overflow-x:auto;-webkit-overflow-scrolling:touch;position:sticky;top:var(--anav-top,148px);z-index:190}}
.anav::-webkit-scrollbar{{height:3px}}
.anav::-webkit-scrollbar-thumb{{background:rgba(255,255,255,.4);border-radius:2px}}
.anav a{{font-size:11px;color:rgba(255,255,255,.85);text-decoration:none;padding:3px 10px;border-radius:10px;border:1px solid rgba(255,255,255,.2);white-space:nowrap;flex-shrink:0;transition:.15s}}
.anav a:hover,.anav a.active{{background:rgba(255,255,255,.22);color:#fff}}
/* コンテナ */
.container{{max-width:960px;margin:0 auto;padding:24px 16px 60px}}
/* エリアブロック */
.ab{{margin-bottom:24px;border-radius:10px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,.08)}}
.ah{{background:linear-gradient(90deg,#1e3a6e,#2d5a8c);color:#fff;padding:12px 20px;display:flex;align-items:center;gap:10px}}
.ah-name{{font-size:16px;font-weight:700}}
.ah-cnt{{margin-left:auto;font-size:11px;background:rgba(255,255,255,.18);padding:2px 10px;border-radius:8px}}
/* 計画カード */
.plan-card{{background:#fff;border-bottom:1px solid #e8eef8}}
.plan-card:last-child{{border-bottom:none}}
.plan-card:hover{{background:#f7faff}}
/* カード内部: 定義リスト形式 */
dl.fields{{display:grid;grid-template-columns:120px 1fr;gap:0}}
dl.fields>div{{display:contents}}
dl.fields dt{{padding:9px 14px 9px 20px;font-size:11px;font-weight:700;color:#6b7280;background:#f9fafb;border-bottom:1px solid #edf2f7;border-right:1px solid #e8eef8;display:flex;align-items:flex-start;white-space:nowrap}}
dl.fields dd{{padding:9px 20px 9px 16px;font-size:13px;color:#2d3748;border-bottom:1px solid #edf2f7;display:flex;align-items:flex-start;flex-wrap:wrap;gap:6px;word-break:break-word}}
dl.fields .title-row dt{{background:#eef2fa;color:#1e3a6e;font-size:12px}}
dl.fields .title-row dd{{font-size:14px;font-weight:700}}
dl.fields .detail-row dd{{font-size:13px;color:#374151;line-height:1.7;display:block}}
/* フェーズバッジ */
.pb{{font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;color:#fff;white-space:nowrap}}
.pb-completed{{background:#27ae60}} .pb-construction{{background:#e67e22}} .pb-pre_construction{{background:#8e44ad}} .pb-planning{{background:#2980b9}} .pb-info{{background:#7f8c8d}}
/* 開発期間 */
.period-box{{display:flex;flex-wrap:wrap;align-items:center;gap:8px}}
.period-s{{color:#8e44ad;font-weight:600;font-size:12px}}
.period-e{{color:#27ae60;font-weight:600;font-size:12px}}
.period-arrow{{color:#aaa;font-size:14px}}
.no-period{{color:#bbb;font-size:12px;font-style:italic}}
/* 箇条書き詳細 */
.detail-list{{margin:0;padding-left:16px;list-style:disc}}
.detail-list li{{margin:3px 0;font-size:13px;color:#374151;line-height:1.65}}
/* スペックバー */
.specs-bar{{display:flex;flex-wrap:wrap;gap:6px}}
.spec-item{{display:inline-flex;align-items:center;background:#f0f4ff;border:1px solid #d0daea;border-radius:6px;overflow:hidden;font-size:11px}}
.spec-k{{background:#1e3a6e;color:#fff;padding:2px 7px;font-weight:700;white-space:nowrap}}
.spec-v{{padding:2px 8px;color:#1a1a2e;font-weight:600;white-space:nowrap}}
/* URL */
.src-link{{color:#1e4d8c;font-size:12px;text-decoration:none;word-break:break-all;border-bottom:1px dashed #a0aec0}}
.src-link:hover{{color:#2d6a9f;border-bottom-color:#2d6a9f}}
/* エリアチップ */
.area-chip{{background:#eef2fa;color:#1e3a6e;font-size:11px;font-weight:700;padding:2px 10px;border-radius:10px}}
/* 優先度 */
.pri-high{{background:#fef2f2;color:#c0392b;font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px}}
.pri-medium{{background:#fff8f0;color:#d35400;font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px}}
footer{{text-align:center;padding:18px;font-size:11px;color:#aaa}}
/* 日付フィルタバー */
.filter-bar{{background:#f4f7fc;border-bottom:1px solid #d0daea;padding:7px 28px;display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:12px;color:#555;position:sticky;top:var(--fb-top,100px);z-index:192}}
.filter-bar .fb-label{{font-weight:700;color:#1e3a6e;margin-right:2px}}
.fb-sep{{color:#c8d4e8;margin:0 4px;font-size:14px}}
.fbtn{{background:#fff;border:1px solid #c0cfe0;border-radius:14px;padding:3px 14px;font-size:12px;cursor:pointer;color:#445;transition:.15s;white-space:nowrap}}
.fbtn:hover{{background:#e8eef8;border-color:#8aaad0}}
.fbtn.active{{background:#1e3a6e;color:#fff;border-color:#1e3a6e}}
.fb-date{{border:1px solid #c0cfe0;border-radius:6px;padding:2px 8px;font-size:12px;color:#445;background:#fff;cursor:pointer;height:26px}}
.fb-date:focus{{outline:none;border-color:#1e3a6e;box-shadow:0 0 0 2px rgba(30,58,110,.15)}}
.fb-date-clear{{background:none;border:none;color:#aaa;cursor:pointer;font-size:14px;padding:0 2px;line-height:1;vertical-align:middle}}
.fb-date-clear:hover{{color:#e53e3e}}
.filter-count{{margin-left:auto;font-size:11px;color:#888}}
.fbtn.today-btn{{background:#e8f4ec;border-color:#4caf82;color:#1a6e42}}
.fbtn.today-btn.active{{background:#1a6e42;color:#fff;border-color:#1a6e42}}
.fb-toggle{{margin-left:4px;font-size:12px;color:#555;cursor:pointer;display:flex;align-items:center;gap:4px}}
.fb-toggle input{{cursor:pointer;accent-color:#1e3a6e}}
@media(max-width:600px){{
  dl.fields{{grid-template-columns:90px 1fr}}
}}
</style>
</head>
<body>
<div class="hdr">
  <span style="font-size:26px">🏙️</span>
  <h1>都市開発計画 エリア別一覧</h1>
  <div class="meta">更新: {generated}<br>全{total}件 / {cnt_areas}エリア</div>
</div>
<div class="stats">
  ✅ 完成・供用中 <span class="sn sc">{cnt_completed}</span> &nbsp;
  🔨 工事中 <span class="sn sw">{cnt_construction}</span> &nbsp;
  📐 着工予定 <span class="sn sp">{cnt_pre}</span> &nbsp;
  📋 計画中 <span class="sn sl">{cnt_planning}</span> &nbsp;
  📄 その他 <span class="sn si">{cnt_info}</span>
</div>
<div class="filter-bar">
  <span class="fb-label">📅 期間</span>
  <button class="fbtn today-btn" id="btn-today">本日分</button>
  <button class="fbtn" data-days="0">全期間</button>
  <button class="fbtn" data-days="30">1ヶ月</button>
  <button class="fbtn" data-days="90">3ヶ月</button>
  <button class="fbtn" data-days="180">6ヶ月</button>
  <button class="fbtn active" data-days="365">1年</button>
  <span class="fb-sep">|</span>
  <span class="fb-label">📆 日付指定</span>
  <input type="date" id="date-from" class="fb-date" title="開始日">
  <span style="color:#999">〜</span>
  <input type="date" id="date-to" class="fb-date" title="終了日">
  <button class="fb-date-clear" id="date-clear" title="日付指定をクリア">✕</button>
  <label class="fb-toggle"><input type="checkbox" id="show-completed"> 完了済みも表示</label>
  <span class="filter-count" id="filter-count"></span>
</div>
<div class="anav" id="area-nav">{nav_links}</div>
<div class="container">
{body}
</div>
<footer>urban-dev-tracker が自動生成 — {generated}</footer>
<script>
// ── フィルタ状態 ──────────────────────────────────────────────
let _currentDays = 365;
let _dateFrom = null;   // "YYYY-MM-DD" or null
let _dateTo   = null;   // "YYYY-MM-DD" or null

// ── メインフィルタ関数 ────────────────────────────────────────
function applyFilter() {{
  const showCompleted = document.getElementById('show-completed')?.checked ?? false;
  // 日付範囲 or 相対日数でカットオフを決定
  let cutoffFrom = null, cutoffTo = null;
  if (_dateFrom || _dateTo) {{
    if (_dateFrom) cutoffFrom = new Date(_dateFrom);
    if (_dateTo)   cutoffTo   = new Date(_dateTo + 'T23:59:59');
  }} else if (_currentDays > 0) {{
    cutoffFrom = new Date(Date.now() - _currentDays * 86400000);
  }}

  let total = 0;
  document.querySelectorAll('.ab').forEach(ab => {{
    let visible = 0;
    ab.querySelectorAll('.plan-card').forEach(card => {{
      const d        = card.dataset.date;
      const endYear  = parseInt(card.dataset.endYear  || '0');
      const endMonth = parseInt(card.dataset.endMonth || '0');
      // 取得日フィルタ
      let dateOk = true;
      if (d) {{
        const cd = new Date(d);
        if (cutoffFrom && cd < cutoffFrom) dateOk = false;
        if (cutoffTo   && cd > cutoffTo)   dateOk = false;
      }}
      // 完了済みフィルタ（2026年4月より前）
      let isOldCompleted = false;
      if (endYear > 0) {{
        if (endYear < 2026) isOldCompleted = true;
        else if (endYear === 2026 && endMonth > 0 && endMonth < 4) isOldCompleted = true;
      }}
      const show = dateOk && (!isOldCompleted || showCompleted);
      card.style.display = show ? '' : 'none';
      if (show) visible++;
    }});
    ab.style.display = visible > 0 ? '' : 'none';
    const cnt = ab.querySelector('.ah-cnt');
    if (cnt) cnt.textContent = visible + ' 件';
    total += visible;
  }});
  // ナビリンク同期
  document.querySelectorAll('.anav a').forEach(a => {{
    const id = a.getAttribute('href').slice(1);
    const ab = document.getElementById(id);
    a.style.display = (ab && ab.style.display !== 'none') ? '' : 'none';
  }});
  const fc = document.getElementById('filter-count');
  if (fc) fc.textContent = total + ' 件表示中';
}}

// ── 本日分ボタン ─────────────────────────────────────────────
document.getElementById('btn-today')?.addEventListener('click', function() {{
  const today = new Date().toISOString().slice(0, 10);
  _dateFrom = today; _dateTo = today;
  _currentDays = -1;
  document.getElementById('date-from').value = today;
  document.getElementById('date-to').value   = today;
  document.querySelectorAll('.fbtn[data-days]').forEach(b => b.classList.remove('active'));
  this.classList.add('active');
  applyFilter();
}});

// ── クイックボタン（1ヶ月/3ヶ月/…）────────────────────────────
document.querySelectorAll('.fbtn[data-days]').forEach(btn => {{
  btn.addEventListener('click', function() {{
    _currentDays = parseInt(this.dataset.days);
    _dateFrom = null; _dateTo = null;
    document.getElementById('date-from').value = '';
    document.getElementById('date-to').value   = '';
    document.querySelectorAll('.fbtn[data-days]').forEach(b => b.classList.remove('active'));
    document.getElementById('btn-today')?.classList.remove('active');
    this.classList.add('active');
    applyFilter();
  }});
}});

// ── 日付入力（開始/終了）────────────────────────────────────
function onDateInput() {{
  _dateFrom = document.getElementById('date-from').value || null;
  _dateTo   = document.getElementById('date-to').value   || null;
  // 日付指定中はクイックボタンの active を外す
  document.querySelectorAll('.fbtn[data-days]').forEach(b => b.classList.remove('active'));
  applyFilter();
}}
document.getElementById('date-from')?.addEventListener('change', onDateInput);
document.getElementById('date-to')?.addEventListener('change', onDateInput);

// ── 日付クリアボタン ─────────────────────────────────────────
document.getElementById('date-clear')?.addEventListener('click', function() {{
  _dateFrom = null; _dateTo = null;
  document.getElementById('date-from').value = '';
  document.getElementById('date-to').value   = '';
  _currentDays = 365;
  document.querySelectorAll('.fbtn[data-days]').forEach(b => b.classList.remove('active'));
  document.getElementById('btn-today')?.classList.remove('active');
  const btn = document.querySelector('.fbtn[data-days="365"]');
  if (btn) btn.classList.add('active');
  applyFilter();
}});

// ── 完了済みトグル ───────────────────────────────────────────
document.getElementById('show-completed')?.addEventListener('change', applyFilter);

// 初期化: 1年以内 + 2026-04以前完了案件は非表示
applyFilter();

// ── エリアナビ スクロール連動 ─────────────────────────────────
const obs = new IntersectionObserver(entries => {{
  entries.forEach(e => {{
    if (e.isIntersecting) {{
      document.querySelectorAll('.anav a').forEach(a => a.classList.remove('active'));
      const a = document.querySelector(`.anav a[href="#${{e.target.id}}"]`);
      if (a) a.classList.add('active');
    }}
  }});
}}, {{threshold: 0.05, rootMargin:'-60px 0px -60% 0px'}});
document.querySelectorAll('.ab').forEach(el => obs.observe(el));
// ── sticky top 値を動的に計算 ──────────────────────────────────
function updateStickyOffsets() {{
  const hdrH   = (document.querySelector('.hdr')        || {{}}).offsetHeight || 0;
  const statsH = (document.querySelector('.stats')      || {{}}).offsetHeight || 0;
  const fbH    = (document.querySelector('.filter-bar') || {{}}).offsetHeight || 0;
  const r = document.documentElement;
  r.style.setProperty('--stats-top', hdrH + 'px');
  r.style.setProperty('--fb-top',   (hdrH + statsH) + 'px');
  r.style.setProperty('--anav-top', (hdrH + statsH + fbH) + 'px');
}}
updateStickyOffsets();
window.addEventListener('resize', updateStickyOffsets);
</script>
<!-- パスワード認証オーバーレイ -->
<div id="auth-overlay" style="display:none;position:fixed;inset:0;z-index:9999;background:#1a1a2e;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:16px;">
  <div style="background:#fff;border-radius:12px;padding:40px 48px;box-shadow:0 8px 32px rgba(0,0,0,0.4);text-align:center;max-width:380px;width:90%;">
    <div style="font-size:36px;margin-bottom:8px;">🏙️</div>
    <h2 style="margin:0 0 4px;font-size:18px;color:#1a1a2e;">都市開発計画 情報レポート</h2>
    <p style="margin:0 0 24px;font-size:13px;color:#666;">アクセスにはパスワードが必要です</p>
    <input id="auth-pw" type="password" placeholder="パスワードを入力" style="width:100%;box-sizing:border-box;padding:10px 14px;border:1px solid #ccc;border-radius:6px;font-size:15px;outline:none;" onkeydown="if(event.key==='Enter')authCheck()">
    <div id="auth-err" style="color:#e53;font-size:13px;margin-top:8px;min-height:18px;"></div>
    <button onclick="authCheck()" style="margin-top:12px;width:100%;padding:11px;background:#2563eb;color:#fff;border:none;border-radius:6px;font-size:15px;cursor:pointer;">ログイン</button>
  </div>
</div>
<script>
(function(){{
  var PASS = 'salowin-tenpo';
  var KEY  = 'auth_ok';
  var overlay = document.getElementById('auth-overlay');
  if (sessionStorage.getItem(KEY) !== '1') {{
    overlay.style.display = 'flex';
    setTimeout(function(){{ document.getElementById('auth-pw').focus(); }}, 50);
  }} else {{
    overlay.style.display = 'none';
  }}
  window.authCheck = function() {{
    var pw = document.getElementById('auth-pw').value;
    if (pw === PASS) {{
      sessionStorage.setItem(KEY, '1');
      overlay.style.display = 'none';
    }} else {{
      document.getElementById('auth-err').textContent = 'パスワードが違います';
      document.getElementById('auth-pw').value = '';
      document.getElementById('auth-pw').focus();
    }}
  }};
}})();
</script>
</body>
</html>"""


def _plan_card_html(a: dict) -> str:
    """1件の開発計画を構造化カード形式でHTMLにする"""
    content = (a.get("content") or a.get("summary") or "").strip()
    phase = _detect_phase(content)
    phase_label, phase_color, phase_emoji = _PHASE_META[phase]
    period = _extract_period(content)

    title = _clean_title(a.get("title", "（タイトルなし）").replace("【更新検知】", "").strip())
    url = _html.escape(a.get("url", "#"))
    source = _html.escape(a.get("source_name", ""))
    area = _html.escape(_effective_area(a))
    priority = a.get("priority", "normal")

    # 情報取得日（YYYY-MM-DD に正規化して data-date に使用）
    fetched = (a.get("fetched_at") or "")[:10]
    # 公開日（YYYY-MM-DD に正規化。日本語形式は変換。kensetsunewsはタイトルから抽出）
    published = (
        _parse_pub_date(a.get("published_at") or "")
        or _pub_date_from_title(a.get("title") or "")
        or ""
    )
    acq_date = published or fetched

    # 竣工・完了年月を data 属性用に抽出（"2026年3月" → year=2026, month=3）
    _end_year, _end_month = 0, 0
    if period["end"]:
        _m = re.search(r'(\d{4})年(\d{1,2})月', period["end"])
        if not _m:
            _m2 = re.search(r'(\d{4})年', period["end"])
            if _m2:
                _end_year = int(_m2.group(1))
        else:
            _end_year, _end_month = int(_m.group(1)), int(_m.group(2))
    _end_attr = f'data-end-year="{_end_year}" data-end-month="{_end_month}"' if _end_year else ""

    # 計画詳細：箇条書きに変換
    detail_raw = (a.get("content") or a.get("summary") or "").strip()
    bullets = _to_bullets(detail_raw)

    if not bullets:
        # JS必須ページ（ボイラープレートのみで本文なし）
        if re.search(r'[Jj]ava[Ss]cript', detail_raw):
            bullets = ["（このページはJavaScriptが必要なため詳細を自動取得できませんでした。リンク先でご確認ください）"]
        elif not detail_raw:
            bullets = ["（詳細情報が取得されていません。リンク先でご確認ください）"]
        else:
            bullets = [detail_raw[:200]]
    elif len(bullets) == 1 and len(detail_raw) < 80:
        # スタブ記事（content≒title の速報記事）
        bullets.append("（速報のみ。詳細はリンク先でご確認ください）")

    # 開発期間表示
    if period["start"] or period["end"]:
        s_html = f'<span class="period-s">▶ 着工: {_html.escape(period["start"])}</span>' if period["start"] else ""
        arrow = '<span class="period-arrow">→</span>' if period["start"] and period["end"] else ""
        e_html = f'<span class="period-e">◀ 完成: {_html.escape(period["end"])}</span>' if period["end"] else ""
        period_html = f'<div class="period-box">{s_html}{arrow}{e_html}</div>'
    else:
        period_html = '<span class="no-period">記載なし（詳細を参照）</span>'

    # 優先度バッジ
    pri_html = ""
    if priority == "high":
        pri_html = '<span class="pri-high">優先度: 高</span>'
    elif priority == "medium":
        pri_html = '<span class="pri-medium">優先度: 中</span>'

    # プロジェクトスペック抽出
    specs = _extract_specs(content)
    specs_html = ""
    if specs:
        spec_items = "".join(
            f'<span class="spec-item"><span class="spec-k">{_html.escape(k)}</span>'
            f'<span class="spec-v">{_html.escape(v)}</span></span>'
            for k, v in specs.items()
        )
        specs_html = f'<div class="specs-bar">{spec_items}</div>'

    # 複数URL対応: _extra_urls が付加されている場合、全URLを列挙
    extra_urls = a.get("_extra_urls", [])
    primary_url_html = (
        f'<a class="src-link" href="{url}" target="_blank" rel="noopener">'
        f'{_html.escape(a.get("url", ""))}</a>'
        f'<span style="margin-left:6px;font-size:11px;color:#888">— {source}</span>'
    )
    extra_url_html = "".join(
        f'<br><a class="src-link" href="{_html.escape(eu["url"])}" target="_blank" rel="noopener">'
        f'{_html.escape(eu["url"])}</a>'
        f'<span style="margin-left:6px;font-size:11px;color:#888">— {_html.escape(eu.get("source_name",""))}</span>'
        for eu in extra_urls
    )

    return f"""<div class="plan-card" data-date="{acq_date}" {_end_attr}>
<dl class="fields">
  <div class="title-row">
    <dt>計画名</dt>
    <dd>
      <span class="pb pb-{phase}">{phase_emoji} {_html.escape(phase_label)}</span>
      {pri_html}
      <a href="{url}" target="_blank" rel="noopener">{_html.escape(title)}</a>
    </dd>
  </div>
  <div>
    <dt>エリア</dt>
    <dd><span class="area-chip">📍 {area}</span></dd>
  </div>
  <div>
    <dt>情報取得日</dt>
    <dd>{_html.escape(acq_date)}</dd>
  </div>
  <div>
    <dt>開発期間</dt>
    <dd>{period_html}</dd>
  </div>{f"""
  <div>
    <dt>規模・用途</dt>
    <dd>{specs_html}</dd>
  </div>""" if specs_html else ""}
  <div class="detail-row">
    <dt>計画詳細</dt>
    <dd><ul class="detail-list">{"".join(f"<li>{_html.escape(b)}</li>" for b in bullets)}</ul></dd>
  </div>
  <div>
    <dt>参照先（URL）</dt>
    <dd>{primary_url_html}{extra_url_html}</dd>
  </div>
</dl>
</div>"""


# フェーズ順（完成→工事中→着工予定→計画中→その他）
_PHASE_ORDER = {"completed": 0, "construction": 1, "pre_construction": 2, "planning": 3, "info": 4}


_DISPLAY_GENERIC_TITLES_RE = re.compile(
    r'^(ニュースリリース|プレスリリース|ニュース|お知らせ|新着情報|トピックス|'
    r'最新情報|information|news|topics|press release|\d{4}年(\d+月)?$|'
    r'.*(情報一覧|リリース一覧|お知らせ一覧)$|'
    r'\d{4,5}_\s|^ニュース .+| News$|'
    # 日付のみタイトル（例: "2026年02月17日"）
    r'^\d{4}年\d{1,2}月\d{1,2}日$|'
    # "〇〇│ニュースリリース" / "〇〇│プレスリリース" / "おしらせ・ニュースリリース" パターン
    r'.*[│|・]ニュースリリース.*|.*[│|・]プレスリリース.*|'
    r'ニュースリリース[│|・].*|プレスリリース[│|・].*|'
    # "〜公式サイト" で終わるナビページ / ナビパンくず（| ニュース | 等）
    r'.*公式サイト$|'
    r'.*[│|]\s*(?:ニュース|イベント|お知らせ|リリース)\s*[│|].*)',
    re.IGNORECASE,
)

# タイトル内に含まれる除外キーワード（海外のみ）
_OUT_OF_SCOPE_TITLE_KEYWORDS = frozenset([
    # 海外（国内都市開発と無関係）
    "ウクライナ", "ロシア", "中国", "韓国", "アメリカ", "欧州",
])

# 日付・カテゴリプレフィックスを除去して本質的なタイトルを抽出
# カテゴリは日本語のみで構成される短い語（例: 都市開発・ビル, 商業施設, グループ会社）
# ASCII文字や 「」 が混入するトークンはカテゴリとみなさない
_TITLE_DATE_PREFIX_RE = re.compile(
    r'^\d{4}(?:年\d{1,2}月\d{1,2}日|\.\d{2}\.\d{2})'
    r'[\s　]+'
    r'(?:[\u3040-\u30ff\u30fc\u4e00-\u9fff・/]{2,14}[\s　]+){0,6}'
)

# タイトルグループ化用パターン
# ① 鉤括弧・二重引用符内の固有名詞（7文字以上 = 固有のプロジェクト名と判定）
_PROJECT_BRACKET_RE = re.compile(r'[「"]([^」"]{5,25})[」"]')
# ② タイトル先頭の施設名パターン（ビル・タワー等で終わる最短マッチ）
_FACILITY_PREFIX_RE = re.compile(
    r'^([^\s　、，．。！？/・]{4,20}?'
    r'(?:ビル(?:\d+)?|タワー(?:\d+)?|ヒルズ|マンション|センター|アリーナ|スタジアム'
    r'|プレイス|ゲート|テラス|プロジェクト|シティ|TOWER|GATE|HILLS|PLAZA|SQUARE|CITY))',
    re.IGNORECASE,
)


def _title_group_key(title: str) -> str:
    """タイトルのグループ化キーを生成。
    ① 鉤括弧/二重引用符内の固有名詞（7文字以上）→ プロジェクト名として使用
    ② タイトル先頭がビル/タワー等の施設名 → 施設名部分を使用
    ③ デフォルト: 日付・カテゴリ除去後の先頭25文字
    """
    core = _TITLE_DATE_PREFIX_RE.sub("", title).strip() or title
    # ① 括弧内の固有名詞（プロジェクト・建物名）
    m = _PROJECT_BRACKET_RE.search(core)
    if m:
        return m.group(1)
    # ② 先頭が施設名で始まる（例: 「電通銀座ビルを解体/三菱地所」→「電通銀座ビル」）
    m = _FACILITY_PREFIX_RE.match(core)
    if m:
        return m.group(1)
    return core[:25].strip()

_DISPLAY_BAD_TITLE_PATTERNS = [
    # 施設・イベント系（開発無関係）
    '地域センター', '観光協会', '商店街', '水族館', 'コミュニティバス',
    'ダンスプロジェクト', 'フラワーイベント', '体験展示', 'バスツアー',
    'スローモビリティ', 'DONDON', 'さくらまつり', '花見', 'ひなまつり',
    'ウォークラリー', 'のりものフェスタ', 'Minecraft', 'オリエンテーリング',
    '講習会', 'みどりのカーテン', 'ストリートギャラリー',
    # 企業・人事・IR系
    '健康経営優良法人', '年頭所感', '統合報告書', 'IRニュース', '月報KAJIMA',
    '防災訓練のご報告', '震災訓練のご報告', 'プレスリリース | 企業情報',
    '本社機能移転', 'プラチナパートナー', 'アートアワード',
    '人事異動', '機構改革', '組織変更', '役員変更', '代表取締役',  # 人事系
    '採用情報', '求人', '障がい者雇用',
    # インフラ・下水道・橋梁（建物再開発と無関係なインフラ工事）
    '下水道', '橋梁', '上水道', '道路改良', '河川改修', '堤防',
    '舗装工事', '管路更新', '送水管', '配水管', '下水管',
    # PFI・公共施設（建設通信に多い非都市開発系記事）
    '美術館', '博物館', '図書館', '体育館', '市民ホール',
    '学校', '小学校', '中学校', '高校', '大学キャンパス',
    '病院整備', '診療所',
    # 行政一般（都市開発以外）
    '電話番号の廃止', '共生社会', 'キャップ＆トレード', 'イノベーション促進',
    '東京宝島', 'キッズ・ファミリー', 'キッズプロモーション',
    '放置自転車', '電動アシスト', '河川敷地占用', '住宅確保要配慮',
    '住宅基本計画',  # 行政住宅政策（個別開発事業でない）
    '宅地造成及び特定盛土',  # 規制法ガイドページ
    # 事業・マーケティング系
    '再生可能エネルギー', 'スタートアップ', 'ポイント キャンペーン', 'キャンペーン',
    '桜をライトアップ', '仮囲いを活用した', 'サステナビリティ',
    '観光需要', 'ひといきスペース', 'シェアモビリティ',
    '城ヶ島', 'ふふ ',  # リゾート旅館
    '販売力強化プロジェクト', '節電・省エネ', 'リニューアルオープン',
    # 汎用カテゴリページ
    '街づくり（複合開発）', 'リゾート/ホテル', 'シニア住宅/介護住宅',
    'ヘルスケアサービス', '産業まちづくり', '都市計画に関するお知らせ',
    'まちづくりに関するお知らせ', '建築物の建築に関わる参考データ',
    'メニューを閉じる', 'AWARD', 'Award',
    # コラム・一般記事（スターツ等の汎用コラム）
    '実写映画', 'アニメ化', 'ホテルの清掃', 'プロフェッショナル', '感動の物語',
    '不動産投資', '投資用', 'お部屋探し', '賃貸経営', '管理会社',
    '食のトレンド', '免震技術', 'レトロフィット工法', 'ブランド戦略',
    # 公園・自然・環境（建物再開発でないもの）
    'スポーツ公園', '企業の森', '花粉の少ない森', '公園整備',
    # 健美家 サービスページ（再開発情報でないプロモーションページ）
    '健美家の', '不動産会社様向け',
    # SmartNews チャンネル（SNSアグリゲータ、開発情報でない）
    'SmartNews',
    # モビリティ・交通系（建物開発と無関係）
    'グリーンスローモビリティ', 'スローモビリティ',
    # イベント・季節もの
    'さくらまつり', 'さくら祭', '花見イベント',
    # 施設名商標・玩具連携（建物開発でない）
    'タカラトミー', 'トミカ', 'プラレール',
    # リニューアル系（既存建物の内装改修等、新規開発でない場合が多い）
    # ※ 大規模再開発と重複しないよう部分マッチのみ
    '客室リニューアル', 'ロビーリニューアル',
    # 統計・調査系（個別開発情報でない）
    '公共工事動向', '着工統計', '建設工事施工統計', '建設業許可',
    # 子供公園・遊び場（建物開発でない）
    'こどもパーク', 'こどもの国', '子どもの広場',
    # ポイントキャンペーン・運賃系
    '均一運賃', 'ポイントサービス',
    # 行政ダウンロード・地図系（地図ファイルの配布情報）
    '全図のダウンロード', '区図のダウンロード', '指定道路図', '地形図のダウンロード',
    '都市計画図の販売', '都市計画図のダウンロード',
    # 行政書類・ボランティア（都市開発でない）
    '違反広告物', 'ボランティアを募集', '窓口自動交付機',
    # 計画書・概要書の一般案内（個別プロジェクト情報でない）
    '建築計画概要書', '積算参考資料',
    # 立地適正化計画（政策文書；個別開発プロジェクトでない）
    '立地適正化計画',
    # 行政審査会・委員会（開発プロジェクトでない）
    '建築審査会', '都市計画審議会', '景観審議会',
    # 条例・推進委員（住環境系行政、開発でない）
    '条例推進委員', 'まちをきれいにする',
    # 都市計画の案内（情報提供ページ、個別プロジェクトでない）
    '都市計画のお調べ', '都市計画に関するお調べ',
    # 景観・広告物規制（建物開発でない）
    '景観協定', '屋外広告物',
    # イベント・CSR系（品川区の京急PR等）
    'スタンプラリー', 'パートナー募集', '森林共創',
    # スコープ外企業（愛媛・伊予鉄道）
    '伊予鉄',
    # 京都観光記事（都市開発でない不動産コラム）
    '伏見稲荷',
    # 消防署統合整備（建物開発でなく行政インフラ）
    '消防署の統合',
    # 行政・公共住宅管理系（大阪市 osaka_urban_dev から混入）
    '市営住宅用地の使用事業者', '市営住宅の敷地を活用', '密集住宅市街地の整備と補助金',
    '仕様書等に対する質問', '二段階審査方式により募集',
    # クルーズ船・観光船（建物開発でない）
    'アクアシンフォニー',
    # 子供公園・遊び場（建物開発でないもの追加）
    'こどもふっかパーク',
    # 鉄道会社CM・MV・キャンペーン（開発でない）
    'ミュージックビデオ', 'オフィシャルMV', 'MV公開', 'アニメーションCM',
    'ライオンズ応援', 'チームカラー', '応援施策',
    # ダム・治水インフラ（都市開発でない）
    'ダム管理', 'ダム高度化', '治水計画', '河川管理',
    # 地方港湾・客船（都市開発でない）
    '客船乗り場', '客船ターミナル', '旅客ターミナル整備',
    # 地方公共施設（保健センター・新庁舎など）
    '総合保健センター', '保健センター新築', '新庁舎建設基本構想',
    # 統計・調査系（追加）
    '受注動態統計', '建設工事受注動態',
    # 学校施設（給食・校舎）
    '給食センター', '校舎棟解体', '学校給食',
    # アーバンスポーツ・ダンスイベント系
    'アーバンスポーツ', 'ダンスイベント', 'ダンスワークショップ',
    # 行政委員会委員募集（環境・景観系）
    '環境基本計画改定委員', '環境計画委員',
    # イベント系（みなとフェスタ等）追加
    'みなとフェスタ', 'ポートフェスタ',
    # 鉄道会社IR・表彰・サービス（開発でない）
    'DX認定', '観光貢献賞', 'バーチャルヒューマン',
    '手荷物当日配送', '手荷物配送', 'ガイドツアーの販売',
    'わーくはぴねす', '収穫体験', '撮影会',
    # 行政制度ページ（個別開発でない）
    '資料の閲覧について', 'マンション管理計画認定',
    '景観法に基づく届出',
    # 動物・自然（都市開発でない）
    'が死亡', 'コアラ', '動物公園',
    # スポーツ選手育成・誘致（都市開発でない）
    'FCバルセロナ', '選手育成機関', 'Ｊ３規格',
    # 港湾浚渫・治水インフラ
    '航路泊地浚渫', '洪水予報河川',
    # 防衛省・自衛隊関連施設
    '近畿中部防衛', '南関東防衛', '防衛省',
    # 地方空港・庁舎（小規模公共施設）
    '空港事務所', '消防組合新庁舎', '県庁舎', '庁舎整備', '庁舎等再整備',
    # 人事
    '新社長に', '取締役兼常務',
    # 耐震診断補助・説明会
    '耐震診断・改修補助事業',
    # 地域交流センター
    '地域交流Cの整備', '港地域交流',
]

# カバー対象外エリア（国内は除外しない）
_OUT_OF_SCOPE_AREAS = frozenset()

# この日付より前に取得・公開された記事は表示・投稿しない（情報鮮度カットオフ）
_DATE_CUTOFF = "2025-11-01"

# kenbiya 記事番号のしきい値（これ未満 ≈ 2025年10月以前の古い記事）
_KENBIYA_MIN_ARTICLE_NUM = 9600

_PUB_DATE_RE = re.compile(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?')
# kensetsunews 等のタイトル末尾に付く「最終更新 | YYYY/MM/DD HH:MM 【速報】」を除去
_TITLE_SUFFIX_RE = re.compile(r'\s*最終更新\s*[|｜]\s*\d{4}/\d{2}/\d{2}.*$')
_TITLE_DATE_RE = re.compile(r'最終更新\s*[|｜]\s*(\d{4})/(\d{2})/(\d{2})')


def _clean_title(title: str) -> str:
    """タイトル末尾の更新日時サフィックスを除去する。"""
    return _TITLE_SUFFIX_RE.sub('', title).strip()


def _pub_date_from_title(title: str) -> str | None:
    """kensetsunews タイトルの「最終更新 | YYYY/MM/DD」から日付を取得する。"""
    m = _TITLE_DATE_RE.search(title or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _get_kenbiya_article_number(url: str) -> int | None:
    """kenbiya.com の記事URLから記事番号を取得。非kenbiya URLはNoneを返す。"""
    if not url or 'kenbiya.com' not in url:
        return None
    m = re.search(r'/(\d+)(?:\.html|/?$)', url)
    return int(m.group(1)) if m else None


def _parse_pub_date(s: str) -> str | None:
    """'YYYY年M月D日' または 'YYYY-MM-DD' → 'YYYY-MM-DD'。解析不能なら None。"""
    if not s:
        return None
    m = _PUB_DATE_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if re.match(r'\d{4}-\d{2}-\d{2}', s):
        return s[:10]
    return None


def _is_display_worthy(a: dict) -> bool:
    """表示に値する記事かどうか判定（タイトル品質・エリアフィルタ・日付カットオフ）"""
    # 取得日カットオフ
    fetched = (a.get("fetched_at") or "")[:10]
    if fetched and fetched < _DATE_CUTOFF:
        return False
    # 記事公開日カットオフ（日付が判明している場合のみ適用）
    pub = _parse_pub_date(a.get("published_at") or "")
    if pub and pub < _DATE_CUTOFF:
        return False
    # kenbiya 記事番号チェック: 古い記事番号は除外（published_at:None でも排除可能）
    kb_num = _get_kenbiya_article_number(a.get("url", ""))
    if kb_num is not None and kb_num < _KENBIYA_MIN_ARTICLE_NUM:
        return False

    title = (a.get("title") or "").strip()
    if not title or len(title) < 8:
        return False
    if _DISPLAY_GENERIC_TITLES_RE.match(title):
        return False
    if any(pat in title for pat in _DISPLAY_BAD_TITLE_PATTERNS):
        return False
    # タイトル内に関東圏外地名 → 除外
    if any(kw in title for kw in _OUT_OF_SCOPE_TITLE_KEYWORDS):
        return False
    # 推定エリアが関東圏外 → 除外
    area = _effective_area(a)
    if area in _OUT_OF_SCOPE_AREAS:
        return False
    return True


def get_active_articles(articles: list[dict]) -> list[dict]:
    """HTMLと同じフィルタ＋重複統合を適用した「表示対象記事」リストを返す。
    ChatWork投稿など、HTML以外の出力でも件数・内容を一致させるために使う。"""
    from notifier import is_development_relevant as _is_dev_relevant

    worthy = [
        a for a in articles
        if _is_display_worthy(a)
        and _is_active_or_future((a.get("content") or a.get("summary") or ""))
    ]

    # タイトルグループ化・重複統合（generate_area_timeline_html と同一ロジック）
    title_groups: dict[str, list[dict]] = {}
    for a in worthy:
        title = (a.get("title") or "").strip()
        key = _title_group_key(title)
        title_groups.setdefault(key, []).append(a)

    _KANTO_STUB_SOURCES = {"kensetsunews_kanto"}
    _NUM_TOKEN_RE = re.compile(r'\d+[棟階万億]|\d+(?:㎡|平米|ha)')
    stub_keys = [
        key for key, group in title_groups.items()
        if (max(group, key=lambda x: len(x.get("content") or x.get("summary") or ""))
            .get("source_id") in _KANTO_STUB_SOURCES)
        and "/" in (max(group, key=lambda x: len(x.get("content") or x.get("summary") or "")).get("title") or "")
    ]
    for stub_key in stub_keys:
        if stub_key not in title_groups:
            continue
        stub_group = title_groups[stub_key]
        stub_rep = max(stub_group, key=lambda x: len(x.get("content") or x.get("summary") or ""))
        stub_title = stub_rep.get("title") or ""
        segments = [s.strip() for s in stub_title.split("/") if len(s.strip()) >= 4]
        absorbed = False
        for target_key, target_group in list(title_groups.items()):
            if target_key == stub_key:
                continue
            target_rep = max(target_group, key=lambda x: len(x.get("content") or x.get("summary") or ""))
            target_title = target_rep.get("title") or ""
            for seg in segments:
                if len(seg) >= 6 and (seg in target_title or seg in target_key):
                    title_groups[target_key].extend(stub_group)
                    del title_groups[stub_key]
                    absorbed = True
                    break
                for tok in _NUM_TOKEN_RE.findall(seg):
                    if len(tok) >= 3 and tok in target_title:
                        title_groups[target_key].extend(stub_group)
                        del title_groups[stub_key]
                        absorbed = True
                        break
                if absorbed:
                    break
            if absorbed:
                break

    active: list[dict] = []
    for group in title_groups.values():
        rep = max(group, key=lambda x: (
            0 if x.get("source_id") in _KANTO_STUB_SOURCES else 1,
            len(x.get("content") or x.get("summary") or ""),
        ))
        if len(group) > 1:
            rep = dict(rep)
            rep["_extra_urls"] = [
                {"url": x["url"], "source_name": x.get("source_name", "")}
                for x in group if x["url"] != rep["url"]
            ]
        active.append(rep)

    def _is_content_meaningful(a: dict) -> bool:
        detail_raw = (a.get("content") or a.get("summary") or "").strip()
        bullets = _to_bullets(detail_raw)
        if len(bullets) <= 1 and len(detail_raw) < 80:
            return False
        return _is_dev_relevant(a)

    return [a for a in active if _is_content_meaningful(a)]


def generate_area_timeline_html(articles: list[dict]) -> str:
    """エリア別・構造化カード形式のHTMLを生成する（2026年以降終了のもの）"""
    generated = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # 表示品質フィルタ → 2026年以降フィルタ の順に適用
    worthy = [
        a for a in articles
        if _is_display_worthy(a)
        and _is_active_or_future((a.get("content") or a.get("summary") or ""))
    ]

    # タイトルグループ化（プロジェクト名・施設名・先頭25文字の順で統合キーを決定）
    title_groups: dict[str, list[dict]] = {}
    for a in worthy:
        title = (a.get("title") or "").strip()
        key = _title_group_key(title)
        title_groups.setdefault(key, []).append(a)

    # 2nd pass: kensetsunews_kanto スタブ記事（content≒title）を
    # スラッシュセグメントのサブストリングマッチ or 数量トークンで既存グループに吸収
    _KANTO_STUB_SOURCES = {"kensetsunews_kanto"}
    _NUM_TOKEN_RE = re.compile(r'\d+[棟階万億]|\d+(?:㎡|平米|ha)')
    stub_keys = []
    for key, group in title_groups.items():
        rep = max(group, key=lambda x: len(x.get("content") or x.get("summary") or ""))
        content = rep.get("content") or rep.get("summary") or ""
        title = rep.get("title") or ""
        if (rep.get("source_id") in _KANTO_STUB_SOURCES
                and "/" in title
                and len(content) <= len(title) + 5):
            stub_keys.append(key)

    for stub_key in stub_keys:
        if stub_key not in title_groups:
            continue
        stub_group = title_groups[stub_key]
        stub_rep = max(stub_group, key=lambda x: len(x.get("content") or x.get("summary") or ""))
        stub_title = stub_rep.get("title") or ""
        segments = [s.strip() for s in stub_title.split("/") if len(s.strip()) >= 4]
        absorbed = False
        for target_key, target_group in list(title_groups.items()):
            if target_key == stub_key:
                continue
            target_rep = max(target_group, key=lambda x: len(x.get("content") or x.get("summary") or ""))
            target_title = target_rep.get("title") or ""
            for seg in segments:
                # サブストリングマッチ（セグメントがターゲットタイトルに含まれる）
                if len(seg) >= 6 and (seg in target_title or seg in target_key):
                    title_groups[target_key].extend(stub_group)
                    del title_groups[stub_key]
                    absorbed = True
                    break
                # 数量トークンマッチ（"19棟" など）
                for tok in _NUM_TOKEN_RE.findall(seg):
                    if len(tok) >= 3 and tok in target_title:
                        title_groups[target_key].extend(stub_group)
                        del title_groups[stub_key]
                        absorbed = True
                        break
                if absorbed:
                    break
            if absorbed:
                break

    # 各グループの代表（コンテンツ最長）を選んで複数URLを付加
    # ※ kanto スタブが吸収されたグループでは、スタブを代表にしないよう優先度付きで選択
    active: list[dict] = []
    for group in title_groups.values():
        rep = max(group, key=lambda x: (
            0 if x.get("source_id") in _KANTO_STUB_SOURCES else 1,
            len(x.get("content") or x.get("summary") or ""),
        ))
        if len(group) > 1:
            rep = dict(rep)  # shallowコピー（DBエントリ自体は変更しない）
            rep["_extra_urls"] = [
                {"url": x["url"], "source_name": x.get("source_name", "")}
                for x in group
                if x["url"] != rep["url"]
            ]
        active.append(rep)

    # 速報スタブ除外＋開発情報関連性チェック（notifier.py と共通ロジック）
    from notifier import is_development_relevant as _is_dev_relevant

    def _is_content_meaningful(a: dict) -> bool:
        # 開発関連性チェック（速報・短いコンテンツでも開発関連なら通す）
        # ※ JS必須ページ・UIボイラープレートは _is_dev_relevant 内の
        #   _content_is_real() で除外される
        return _is_dev_relevant(a)

    active = [a for a in active if _is_content_meaningful(a)]

    # エリア別にグループ化（「全国」や空は地名抽出してから分類・市区単位優先）
    area_map: dict[str, list[dict]] = {}
    for a in active:
        area = _effective_area(a)
        area_map.setdefault(area, []).append(a)

    # エリア内を最新日付順にソート（fetched_at 降順）
    def _article_date(a: dict) -> str:
        return a.get("fetched_at") or a.get("published_at") or ""

    for area in area_map:
        area_map[area].sort(key=_article_date, reverse=True)

    # エリア間も「各エリアの最新記事日付」降順
    sorted_areas = sorted(
        area_map.items(),
        key=lambda x: max((_article_date(a) for a in x[1]), default=""),
        reverse=True,
    )

    # フェーズ統計（フィルタ後）
    phase_counts = {p: 0 for p in _PHASE_META}
    for a in active:
        content = (a.get("content") or a.get("summary") or "")
        phase_counts[_detect_phase(content)] += 1

    # エリアナビリンク
    nav_links = " ".join(
        f'<a href="#ab-{_html.escape(area.replace(" ", "_"))}">'
        f'{_html.escape(area)}（{len(items)}）</a>'
        for area, items in sorted_areas
    )

    # エリアブロック生成
    area_blocks = []
    for area, items in sorted_areas:
        ab_id = f"ab-{area.replace(' ', '_')}"
        cards = "\n".join(_plan_card_html(a) for a in items)
        area_blocks.append(
            f'<div class="ab" id="{_html.escape(ab_id)}">'
            f'<div class="ah">'
            f'<span style="font-size:18px">📍</span>'
            f'<span class="ah-name">{_html.escape(area)}</span>'
            f'<span class="ah-cnt">{len(items)} 件</span>'
            f'</div>'
            f'{cards}'
            f'</div>'
        )

    body = "\n".join(area_blocks)

    return AREA_TIMELINE_TEMPLATE.format(
        generated=generated,
        total=len(active),
        cnt_areas=len(area_map),
        cnt_completed=phase_counts["completed"],
        cnt_construction=phase_counts["construction"],
        cnt_pre=phase_counts["pre_construction"],
        cnt_planning=phase_counts["planning"],
        cnt_info=phase_counts["info"],
        nav_links=nav_links,
        body=body,
    )


def open_area_timeline(articles: list[dict]):
    """エリア別タイムラインをブラウザで開く"""
    html = generate_area_timeline_html(articles)
    tmp = tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, mode="w",
        encoding="utf-8", prefix="urban_dev_timeline_",
    )
    tmp.write(html)
    tmp.close()
    print(f"ブラウザで開いています: {tmp.name}")
    subprocess.run(["open", tmp.name])


def export_area_timeline(articles: list[dict], out_path: Path = None) -> Path:
    if out_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = BASE_DIR / "reports" / f"timeline_{ts}.html"
    html = generate_area_timeline_html(articles)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def export_rich_html(articles: list[dict], out_path: Path = None,
                     password_hash: str = "", password: str = "") -> Path:
    """カード形式HTMLをファイルに保存して返す"""
    if out_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = BASE_DIR / "reports" / f"rich_report_{ts}.html"
    html = generate_rich_html(articles, password_hash=password_hash, password=password)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def deploy_rich_html(articles: list[dict], password_hash: str = "",
                     password: str = "", push: bool = True) -> str:
    """docs/rich.html を生成して GitHub Pages にプッシュする。公開URLを返す。
    push=False のときは HTML 生成のみ行い git 操作はスキップ（GitHub Actions 用）。
    password を渡すと AES-GCM で本文を暗号化して埋め込む。
    """
    import subprocess as _sp
    docs_path = BASE_DIR / "docs" / "rich.html"
    html = generate_rich_html(articles, password_hash=password_hash, password=password)
    with open(docs_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"docs/rich.html を生成しました ({len(articles)} 件)")

    if not push:
        # GitHub Actions 側のコミット＆プッシュステップに任せる
        return ""

    # git add → commit → push
    ts = datetime.now().strftime("%Y/%m/%d %H:%M")
    cmds = [
        ["git", "-C", str(BASE_DIR), "add", "docs/rich.html"],
        ["git", "-C", str(BASE_DIR), "commit", "-m", f"deploy: rich.html 更新 {ts}"],
        ["git", "-C", str(BASE_DIR), "push"],
    ]
    for cmd in cmds:
        r = _sp.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            # "nothing to commit" は無視
            if "nothing to commit" in r.stdout + r.stderr:
                print("変更なし（スキップ）")
                break
            print(f"git エラー: {r.stderr.strip()}")
            return ""
        print(r.stdout.strip() or " ".join(cmd[2:]))

    # GitHub Pages URL を推定
    try:
        r = _sp.run(["git", "-C", str(BASE_DIR), "remote", "get-url", "origin"],
                    capture_output=True, text=True)
        remote = r.stdout.strip()  # e.g. https://github.com/user/repo.git
        m = re.search(r'github\.com/([^/]+)/([^/.]+)', remote)
        if m:
            return f"https://{m.group(1)}.github.io/{m.group(2)}/rich.html"
    except Exception:
        pass
    return ""


def export_html(md_path: Path = None, out_path: Path = None) -> Path:
    """HTMLファイルとして保存して返す"""
    if md_path is None:
        md_path = BASE_DIR / "reports" / "latest.md"
    if out_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = BASE_DIR / "reports" / f"report_{ts}.html"

    html = render_html(md_path)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
