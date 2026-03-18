"""
notifier.py - ChatWork 送信モジュール

設定方法（どちらか一方でOK）:
  1. 環境変数:
       export CHATWORK_TOKEN=your_token
       export CHATWORK_ROOM_ID=12345678
  2. config.yaml:
       chatwork:
         token: your_token
         room_id: "12345678"
         min_priority: high  # 送信する最低優先度（high/medium/normal）
"""

import logging
import os
import re
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

CHATWORK_API = "https://api.chatwork.com/v2/rooms/{room_id}/messages"
PRIORITY_RANK = {"high": 3, "medium": 2, "normal": 1}
PRIORITY_ICON = {"high": "[toaster]", "medium": "[alarm]", "normal": "[info]"}

# チャット通知する記事の公開日上限（これより古い記事は通知しない）
_NOTIFY_RECENCY_DAYS = 30

# タイトル・本文から実際の所在地を検出するためのキーワードマップ
# 長いものを先にマッチさせるため降順ソート済みリストで管理
_AREA_KEYWORDS: list[tuple[str, str]] = sorted([
    # 政令指定都市・主要市
    ("札幌市", "札幌市"), ("仙台市", "仙台市"), ("さいたま市", "さいたま市"),
    ("千葉市", "千葉市"), ("横浜市", "横浜市"), ("川崎市", "川崎市"),
    ("相模原市", "相模原市"), ("新潟市", "新潟市"), ("静岡市", "静岡市"),
    ("浜松市", "浜松市"), ("名古屋市", "名古屋市"), ("京都市", "京都市"),
    ("大阪市", "大阪市"), ("堺市", "堺市"), ("神戸市", "神戸市"),
    ("岡山市", "岡山市"), ("広島市", "広島市"), ("北九州市", "北九州市"),
    ("福岡市", "福岡市"), ("熊本市", "熊本市"),
    # 主要都市
    ("那覇市", "那覇市"), ("沖縄県", "沖縄県"), ("鹿児島市", "鹿児島市"),
    ("長崎市", "長崎市"), ("大分市", "大分市"), ("宮崎市", "宮崎市"),
    ("松山市", "松山市"), ("高松市", "高松市"), ("高知市", "高知市"),
    ("徳島市", "徳島市"), ("広島県", "広島県"), ("岡山県", "岡山県"),
    ("金沢市", "金沢市"), ("富山市", "富山市"), ("福井市", "福井市"),
    ("長野市", "長野市"), ("松本市", "松本市"), ("甲府市", "甲府市"),
    ("岐阜市", "岐阜市"), ("四日市市", "四日市市"), ("津市", "三重県"),
    ("奈良市", "奈良市"), ("奈良県", "奈良県"), ("和歌山市", "和歌山市"),
    ("姫路市", "姫路市"), ("明石市", "明石市"),
    ("大津市", "大津市"), ("滋賀県", "滋賀県"),
    ("福島市", "福島市"), ("郡山市", "郡山市"), ("いわき市", "いわき市"),
    ("伊達市", "福島県"), ("阿武隈", "福島県"), ("白河市", "福島県"),
    ("会津若松市", "福島県"), ("南相馬市", "福島県"),
    ("山形市", "山形市"), ("盛岡市", "盛岡市"), ("青森市", "青森市"),
    ("秋田市", "秋田市"), ("函館市", "函館市"), ("旭川市", "旭川市"),
    ("水戸市", "水戸市"), ("宇都宮市", "宇都宮市"), ("前橋市", "前橋市"),
    ("高崎市", "高崎市"), ("川越市", "川越市"), ("船橋市", "船橋市"),
    ("松戸市", "松戸市"), ("柏市", "柏市"), ("流山市", "流山市"),
    ("八王子市", "八王子市"), ("立川市", "立川市"),
    # 北信越・甲信越の主要地名（kenbiya_shinhoku対策）
    ("軽井沢", "軽井沢"), ("糸魚川", "新潟県"), ("見附市", "新潟県"),
    ("長岡市", "長岡市"), ("上越市", "新潟県"), ("三条市", "新潟県"),
    ("燕市", "新潟県"), ("柏崎市", "新潟県"),
    ("飯田市", "長野県"), ("諏訪市", "長野県"), ("上田市", "長野県"),
    ("塩尻市", "長野県"), ("佐久市", "長野県"),
    ("富士吉田市", "山梨県"), ("都留市", "山梨県"),
    ("高岡市", "富山県"), ("射水市", "富山県"), ("砺波市", "富山県"),
    ("小松市", "石川県"), ("加賀市", "石川県"), ("七尾市", "石川県"),
    ("敦賀市", "福井県"), ("小浜市", "福井県"),
    # 東海・中部の主要地名（kenbiya_tokai対策）
    ("富士駅", "静岡県"), ("富士市", "静岡県"),
    ("沼津市", "静岡県"), ("三島市", "静岡県"), ("磐田市", "静岡県"),
    ("豊橋市", "愛知県"), ("豊田市", "愛知県"), ("岡崎市", "愛知県"),
    ("一宮市", "愛知県"), ("春日井市", "愛知県"), ("刈谷市", "愛知県"),
    ("津駅", "三重県"), ("鈴鹿市", "三重県"), ("桑名市", "三重県"),
    # 関西の主要地名（kenbiya_kansai対策）
    ("尼崎市", "尼崎市"), ("西宮市", "西宮市"), ("芦屋市", "兵庫県"),
    ("宝塚市", "兵庫県"), ("伊丹市", "兵庫県"), ("川西市", "兵庫県"),
    ("川西町", "奈良県"), ("橿原市", "奈良県"), ("大和郡山市", "奈良県"),
    ("生駒市", "奈良県"), ("磯城郡", "奈良県"), ("近鉄郡山", "奈良県"),
    ("彦根市", "滋賀県"), ("草津市", "滋賀県"), ("守山市", "滋賀県"),
    ("宇治市", "京都府"), ("舞鶴市", "京都府"), ("亀岡市", "京都府"),
    ("泉大津市", "大阪府"), ("高槻市", "大阪府"), ("吹田市", "大阪府"),
    ("豊中市", "大阪府"), ("枚方市", "大阪府"), ("東大阪市", "大阪府"),
    ("箕面市", "大阪府"), ("和泉市", "大阪府"), ("堺市", "大阪府"),
    # 九州・沖縄の主要地名（kenbiya_kyushu対策）
    ("那覇市", "那覇市"), ("沖縄市", "沖縄県"), ("宮古島", "沖縄県"),
    ("石垣市", "沖縄県"), ("名護市", "沖縄県"),
    ("鹿児島", "鹿児島市"), ("薩摩", "鹿児島県"),
    ("長崎駅", "長崎市"), ("佐世保市", "長崎県"),
    ("大分駅", "大分市"), ("別府市", "大分県"),
    ("宮崎駅", "宮崎市"), ("都城市", "宮崎県"),
    ("熊本駅", "熊本市"), ("八代市", "熊本県"),
    ("小倉駅", "北九州市"), ("直方市", "福岡県"), ("飯塚市", "福岡県"),
    ("久留米市", "福岡県"), ("大牟田市", "福岡県"), ("筑紫野市", "福岡県"),
    # 東京23区
    ("千代田区", "千代田区"), ("中央区", "東京都"), ("港区", "東京都"),
    ("新宿区", "新宿区"), ("文京区", "東京都"), ("台東区", "台東区"),
    ("墨田区", "東京都"), ("江東区", "江東区"), ("品川区", "品川区"),
    ("目黒区", "東京都"), ("大田区", "東京都"), ("世田谷区", "東京都"),
    ("渋谷区", "東京都"), ("中野区", "中野区"), ("杉並区", "東京都"),
    ("豊島区", "豊島区"), ("北区", "東京都"), ("荒川区", "東京都"),
    ("板橋区", "東京都"), ("練馬区", "東京都"), ("足立区", "東京都"),
    ("葛飾区", "東京都"), ("江戸川区", "東京都"),
    ("高輪", "東京都"), ("虎ノ門", "東京都"), ("渋谷", "東京都"),
    ("新宿", "新宿区"), ("池袋", "豊島区"), ("上野", "台東区"),
    ("品川", "品川区"), ("浜松町", "東京都"), ("豊洲", "江東区"),
    ("大宮", "大宮"), ("浦和", "さいたま市"), ("幕張", "幕張"),
    # 都道府県（マッチの最終手段）
    ("北海道", "北海道"), ("青森県", "青森市"), ("岩手県", "盛岡市"),
    ("宮城県", "仙台市"), ("秋田県", "秋田市"), ("山形県", "山形市"),
    ("福島県", "福島市"), ("茨城県", "水戸市"), ("栃木県", "宇都宮市"),
    ("群馬県", "前橋市"), ("埼玉県", "さいたま市"), ("千葉県", "千葉市"),
    ("東京都", "東京都"), ("神奈川県", "横浜市"), ("新潟県", "新潟市"),
    ("富山県", "富山市"), ("石川県", "金沢市"), ("福井県", "福井市"),
    ("山梨県", "甲府市"), ("長野県", "長野市"), ("岐阜県", "岐阜市"),
    ("静岡県", "静岡市"), ("愛知県", "名古屋市"), ("三重県", "三重県"),
    ("滋賀県", "大津市"), ("京都府", "京都市"), ("大阪府", "大阪市"),
    ("兵庫県", "神戸市"), ("奈良県", "奈良県"), ("和歌山県", "和歌山市"),
    ("鳥取県", "鳥取市"), ("島根県", "松江市"), ("岡山県", "岡山市"),
    ("広島県", "広島市"), ("山口県", "山口市"), ("徳島県", "徳島市"),
    ("香川県", "高松市"), ("愛媛県", "松山市"), ("高知県", "高知市"),
    ("福岡県", "福岡市"), ("佐賀県", "佐賀市"), ("長崎県", "長崎市"),
    ("熊本県", "熊本市"), ("大分県", "大分市"), ("宮崎県", "宮崎市"),
    ("鹿児島県", "鹿児島市"), ("沖縄県", "沖縄県"),
], key=lambda x: -len(x[0]))  # 長いキーワードを優先


def detect_area(title: str, content: str, fallback: str = "") -> str:
    """タイトルと本文から実際の所在地を検出する。見つからなければ fallback を返す。

    改善点:
    - タイトルを最優先で検索（contentのノイズに引きずられない）
    - NFKC正規化で異体字（例: 神⼾の⼾ U+2F3E → 神戸の戸 U+6238）を統一
    """
    import unicodedata

    def _normalize(text: str) -> str:
        # NFKC正規化で互換漢字・異体字を統一
        return unicodedata.normalize("NFKC", text or "")

    norm_title = _normalize(title)
    norm_content = _normalize((content or "")[:500])

    # まずタイトルのみで検索
    for keyword, area in _AREA_KEYWORDS:
        if keyword in norm_title:
            return area

    # タイトルで見つからなければ本文（先頭500文字）で検索
    for keyword, area in _AREA_KEYWORDS:
        if keyword in norm_content:
            return area

    return fallback


# ── 開発情報の関連性フィルタ ──────────────────────────────────────

# タイトルにこれらが含まれていたら除外（行政手続き・案内・ナビ系）
_BAD_TITLE_KEYWORDS: frozenset[str] = frozenset([
    # 行政手続き・案内
    "ダウンロード", "地形図", "区図", "市全図", "本庁舎のご案内",
    "市役所のご案内", "安全管理について", "手続きについて",
    "よくある質問", "窓口自動交付", "概要書等が窓口",
    "PDFファイル", "アクセス方法", "営業時間", "開庁時間",
    "届出制度について", "立地適正化計画に係る",
    # ナビ・カテゴリラベル（サイトのメニュー等が混入する場合）
    "メニューを閉じる", "ページトップ", "サイトマップ",
    # 開発と無関係なイベント・自然
    "桜をライトアップ", "桜ライトアップ", "花見",
    # イベント・フェスタ（開発計画と無関係な単発イベント）
    "フェスタ", "フェス", "まつり", "祭り", "見学会", "内覧会",
    # 観光・宿泊キャンペーン（開発計画でない）
    "まるごとホテル", "まるごときっぷ", "限定特典」プラン",
    "プラン発売", "きっぷ限定", "さくらまつり", "桜まつり",
    # 行政通知・補助金・案内（開発情報でない）
    "バリアフリー化設備", "バリアフリー化整備", "補助金のご案内",
    "給付金", "助成金のご案内",
    # 会議・資料閲覧案内
    "資料の閲覧", "会議の開催", "会議の結果", "審議会の開催",
    "委員会の開催", "部会の開催", "策定委員会",
    # 説明会・セミナー（開発計画説明でない一般的なもの）
    "説明会のご案内", "セミナーのご案内",
    # 広告・募集・入札（開発情報でない）
    "バナー広告", "広告を募集", "広告掲載申込", "広告募集",
    "入札結果", "入札のお知らせ", "プロポーザル募集",
    # 鉄道ダイヤ改正（運行計画であり開発計画でない）
    "ダイヤ改正", "時刻改正", "ダイヤ変更",
    # 屋外広告・景観条例（行政制度ページ）
    "屋外広告業登録", "屋外広告物", "景観条例",
    # まちづくり方針・基本計画・指針（具体的な開発でない政策文書）
    "まちづくり方針", "まちづくり基本方針", "まちづくり指針",
    "景観計画", "景観形成", "都市計画マスタープラン",
    # 鉄道会社CM・MV・キャンペーン（開発でない）
    "ミュージックビデオ", "オフィシャルMV", "MV公開", "アニメーションCM",
    "ライオンズ応援", "応援施策",
    # ダム・地方インフラ（都市開発でない）
    "ダム管理", "ダム高度化", "治水計画",
    # 地方港湾・客船（都市開発でない）
    "客船乗り場", "客船ターミナル",
    # 地方公共施設（保健センター・新庁舎等）
    "総合保健センター", "保健センター新築", "新庁舎建設基本構想",
    # 統計・調査系
    "受注動態統計", "建設工事受注動態",
    # 学校施設（給食・校舎解体）
    "給食センター", "校舎棟解体", "学校給食",
    # アーバンスポーツ・ダンスイベント系
    "アーバンスポーツ", "ダンスイベント", "ダンスワークショップ",
    # 行政委員会委員募集
    "環境基本計画改定委員", "環境計画委員",
    # 鉄道会社IR・表彰・サービス（開発でない）
    "DX認定", "観光貢献賞", "バーチャルヒューマン",
    "手荷物当日配送", "手荷物配送", "ガイドツアーの販売",
    "わーくはぴねす", "収穫体験", "撮影会",
    # 行政制度ページ（個別開発でない）
    "資料の閲覧について", "マンション管理計画認定",
    "景観法に基づく届出",
    # 動物・自然（都市開発でない）
    "が死亡", "コアラ", "動物公園",
    # スポーツ選手育成・誘致（都市開発でない）
    "FCバルセロナ", "選手育成機関", "Ｊ３規格",
    # 港湾浚渫・治水インフラ
    "航路泊地浚渫", "洪水予報河川",
    # 防衛省・自衛隊関連施設
    "近畿中部防衛", "南関東防衛", "防衛省",
    # 地方空港・庁舎（都市開発でない小規模公共施設）
    "空港事務所", "消防組合新庁舎", "県庁舎", "庁舎整備", "庁舎等再整備",
    # 人事・組織変更
    "新社長に", "取締役兼常務",
    # 耐震診断補助・説明会（制度案内）
    "耐震診断・改修補助事業",
    # 地域交流センター・コミュニティ施設
    "地域交流Cの整備", "港地域交流",
])

# タイトルにこのパターンが含まれていたら除外（正規表現）
_BAD_TITLE_PATTERNS: list[str] = [
    r"^ニュースリリース[｜|：:]",         # 「ニュースリリース｜東急不動産」「ニュースリリース：2026年」等
    r"^ニュース\s*[|｜]\s*ニュース",    # 「ニュース | ニュース | 東京建物」等
    r"^ニュース\s*[|｜]\s*\S+株式会社", # 「ニュース | ○○株式会社」
    r"[|｜].{2,20}株式会社$",           # 末尾が「| ○○株式会社」
    r"^お知らせ\s+\S+ホームページ",     # 「お知らせ　○○ホームページ」
    r"^\d{4}年度?$",                    # 「2023年度」「2026年」だけ
    r"^【更新検知】.{0,30}[|｜]",       # 「【更新検知】サイト名 | ページ名」
    r"^(スタートアップ|データセンター|産業まちづくり|ヘルスケアサービス|"
    r"街づくり|環境・サステナビリティ|歴史的建造物|プレスリリース)$",
    r"^KAJIMA\s+MONTHLY",
    # 会議・審議会の開催案内・資料閲覧
    r"会議[（(].+[）)].*(開催|閲覧)",   # 「○○会議（第N回）の開催」「○○会議（第N回）…資料の閲覧」
    r"(審議会|委員会|協議会|部会|分科会).*(開催|案内|閲覧)",
    # 「〜についてのお知らせ」（行政通知系）
    r"についてのお知らせ$",
    # イベント開催告知パターン
    r"(フェスタ|フェスティバル|まつり|祭り).*(開催|のご案内|について)$",
    r"^.{0,20}(フェスタ|フェスティバル)[　\s]*\d{4}",   # 「〇〇フェスタ2026」形式
    # 「〜の見学会・説明会のご案内」
    r"(見学会|内覧会|説明会)[のをに].{0,10}(ご案内|開催|実施)",
    # 「最終更新 | YYYY/MM/DD」だけのタイトル（タイムスタンプのみ、先頭マッチ）
    r"^最終更新\s*[|｜]\s*\d{4}/\d{2}/\d{2}",
]
_BAD_TITLE_RE = re.compile("|".join(_BAD_TITLE_PATTERNS))

# コンテンツがこれらのパターンのみ → 実質コンテンツなし（UIゴミ・JS通知）
_BAD_CONTENT_INLINE: frozenset[str] = frozenset([
    "URLがコピーされました",
    "リンクを適切な場所に貼りつけて",
    "URLを共有・活用",
    "本文へスキップします",
    "閉じる",
    "このサイトではJavaScript",
    "JavaScriptを有効",
    "JavaScriptを使用",
    "ブラウザの設定でJavaScript",
    "お手数ですがJavaScript",
])

# タイトルまたは本文にこれらが1つ以上含まれていれば「開発情報」と判定
_DEV_KEYWORDS: frozenset[str] = frozenset([
    "再開発", "開発", "着工", "竣工", "建設", "計画", "整備",
    "完成", "開業", "解体", "新築", "工事", "施工", "分譲",
    "マンション", "タワー", "ビル", "商業施設", "ホテル", "オフィス",
    "複合施設", "スタジアム", "アリーナ", "再整備", "跡地",
    "土地区画整理", "都市計画", "市街地再開発", "組合", "権利変換",
    "誘致", "公募", "事業者", "PFI", "PPP", "リニューアル",
    "新駅", "駅前", "駅直結", "街づくり", "まちづくり",
])


def _content_is_real(content: str) -> bool:
    """コンテンツが実質的なテキストを含むか判定する。
    JS通知・UIボイラープレートのみの場合は False を返す。"""
    if not content:
        return False
    lines = [l.strip() for l in re.split(r'[\n。]', content) if l.strip() and len(l.strip()) >= 8]
    if not lines:
        return False
    good = []
    for line in lines:
        # JavaScript通知
        if re.search(r'[Jj]ava[Ss]cript', line):
            continue
        # UIボイラープレートキーワード
        if any(kw in line for kw in _BAD_CONTENT_INLINE):
            continue
        good.append(line)
    # 実質コンテンツが30文字以上あれば OK
    return sum(len(l) for l in good) >= 30


_PUB_DATE_RE_NOTIFY = re.compile(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日')


def _parse_pub_date_notify(s: str) -> str | None:
    """'YYYY年M月D日' または 'YYYY-MM-DD' → 'YYYY-MM-DD'。解析不能なら None。"""
    if not s:
        return None
    m = _PUB_DATE_RE_NOTIFY.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if re.match(r'\d{4}-\d{2}-\d{2}', s):
        return s[:10]
    return None


def is_development_relevant(a: dict) -> bool:
    """都市開発に関連する情報かどうかを判定する。

    以下のいずれかに該当すれば False（除外）:
    - タイトルに行政手続き・ナビ系キーワード／パターンが含まれる
    - タイトルが極端に短い（12文字未満）
    - コンテンツが JS通知・UIボイラープレートのみ（実質コンテンツなし）
    - タイトル＋本文に開発関連キーワードが1つも含まれない
    - 公開日が _NOTIFY_RECENCY_DAYS 日より前（古すぎる記事は通知しない）
    """
    title = (a.get("title") or "").strip()
    content = (a.get("content") or a.get("summary") or "").strip()
    text = title + " " + content[:300]

    # 除外キーワードチェック
    for kw in _BAD_TITLE_KEYWORDS:
        if kw in title:
            return False

    # 除外パターンチェック（正規表現）
    if _BAD_TITLE_RE.search(title):
        return False

    # 極端に短いタイトル（ナビ項目・カテゴリ名）
    if len(title) < 12:
        return False

    # コンテンツ品質チェック: JS必須ページ・UIボイラープレートのみは除外
    if not _content_is_real(content):
        return False

    # 公開日の新しさチェック: 日付が判明していて古すぎる記事は通知しない
    pub = _parse_pub_date_notify(a.get("published_at") or "")
    if pub is not None:
        cutoff = (datetime.now() - timedelta(days=_NOTIFY_RECENCY_DAYS)).strftime('%Y-%m-%d')
        if pub < cutoff:
            return False

    # 開発キーワード必須チェック
    for kw in _DEV_KEYWORDS:
        if kw in text:
            return True

    return False


class ChatWorkNotifier:
    def __init__(self, token: str, room_id: str, min_priority: str = "high"):
        self.token = token
        self.room_id = str(room_id)
        self.min_priority = min_priority
        self.min_rank = PRIORITY_RANK.get(min_priority, 3)

    @classmethod
    def from_config(cls, config: dict) -> "ChatWorkNotifier | None":
        """config.yaml or 環境変数から初期化。設定がなければ None を返す"""
        cw = config.get("chatwork", {})

        token = os.environ.get("CHATWORK_TOKEN") or cw.get("token", "")
        room_id = os.environ.get("CHATWORK_ROOM_ID") or str(cw.get("room_id", ""))
        min_priority = cw.get("min_priority", "high")

        if not token or not room_id:
            return None
        return cls(token, room_id, min_priority)

    # ChatWork の1メッセージ上限（API制限: 7000文字）
    _MAX_MSG_CHARS = 6000
    # 1メッセージに含める記事数の上限
    _MAX_ARTICLES_PER_MSG = 10

    def send(self, articles: list[dict], report_date: str = "") -> bool:
        """対象記事をChatWorkに送信。記事数が多い場合は分割して送る。"""
        targets = [
            a for a in articles
            if PRIORITY_RANK.get(a.get("priority", "normal"), 1) >= self.min_rank
        ]
        if not targets:
            logger.info("ChatWork: 送信対象記事なし")
            return True

        import time
        # 記事を _MAX_ARTICLES_PER_MSG 件ずつのチャンクに分割
        chunks = [
            targets[i: i + self._MAX_ARTICLES_PER_MSG]
            for i in range(0, len(targets), self._MAX_ARTICLES_PER_MSG)
        ]
        total = len(chunks)
        all_ok = True
        for idx, chunk in enumerate(chunks, 1):
            suffix = f"（{idx}/{total}）" if total > 1 else ""
            msg = self._build_message(chunk, report_date, suffix=suffix)
            ok = self._post(msg)
            if not ok:
                all_ok = False
            if idx < total:
                time.sleep(1.2)  # API レート制限対策
        return all_ok

    def send_daily_digest(self, articles: list[dict]) -> bool:
        """日次ダイジェストを送信（全優先度をまとめて）"""
        if not articles:
            logger.info("ChatWork: 送信対象記事なし（日次）")
            return True
        msg = self._build_digest(articles)
        return self._post(msg)

    def _post(self, body: str) -> bool:
        url = CHATWORK_API.format(room_id=self.room_id)
        try:
            resp = requests.post(
                url,
                headers={"X-ChatWorkToken": self.token},
                data={"body": body},
                timeout=15,
            )
            resp.raise_for_status()
            logger.info(f"ChatWork: 送信成功 ({len(body)} 文字)")
            return True
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                logger.error("ChatWork: 認証エラー。APIトークンを確認してください")
            elif e.response is not None and e.response.status_code == 404:
                logger.error("ChatWork: ルームが見つかりません。ROOM IDを確認してください")
            else:
                logger.error(f"ChatWork: HTTPエラー {e}")
            return False
        except Exception as e:
            logger.error(f"ChatWork: 送信エラー {e}")
            return False

    @staticmethod
    def _excerpt(a: dict, max_chars: int = 500) -> str:
        """記事の本文から読みやすい抜粋を生成する"""
        raw = (a.get("content") or a.get("summary") or "").strip()
        if not raw or "JavaScript" in raw or "javascript" in raw:
            return ""

        # 全角・半角スペースや連続スペースを整理
        raw = re.sub(r"[　 \t]+", " ", raw).strip()

        # 改行で段落に分割
        paragraphs = [p.strip() for p in re.split(r"\n+", raw) if p.strip()]
        if not paragraphs:
            return ""

        # 一つの段落に複数の文が「。 」でつながっている場合は文単位に分割
        sentences = []
        for para in paragraphs[:3]:
            # 「。」の後ろで分割（「。」は残す）
            parts = re.split(r"(?<=。)", para)
            sentences.extend([s.strip() for s in parts if s.strip()])

        # max_chars に収まる文数を選ぶ
        selected = []
        total = 0
        for s in sentences:
            if total + len(s) > max_chars:
                break
            selected.append(s)
            total += len(s)

        if not selected:
            # 文が1つも収まらない場合は最初の文を切り詰め
            first = sentences[0] if sentences else ""
            cutoff = first.rfind("。", 0, max_chars)
            selected = [first[:cutoff + 1] + "…" if cutoff > 0 else first[:max_chars] + "…"]

        text = "\n".join(selected)

        # タイトルと実質同一なら返さない
        title = (a.get("title") or "").strip()
        if text.strip() == title or text.strip().startswith(title[:30]):
            return ""
        return text

    # フェーズ判定（viewer.py の _PHASE_PATTERNS と同一キーワード）
    _PHASE_PATTERNS_CW = {
        "completed":        (["竣工済", "開業済", "供用開始", "完成しました", "グランドオープン", "竣工しました", "オープンしました"], "✅ 完成・供用中"),
        "construction":     (["工事中", "施工中", "建設中", "整備中", "着工しました", "工事を開始", "工事に着手"], "🔨 工事中"),
        "pre_construction": (["着工予定", "工事着工予定", "着工を予定", "工事予定", "着工に向け"], "📐 着工予定"),
        "planning":         (["計画決定", "都市計画決定", "計画を策定", "事業認可", "計画中", "検討中", "基本計画", "事業計画", "都市計画変更"], "📋 計画・検討中"),
    }
    _PERIOD_END_RE_CW = re.compile(
        r'(?:\d{4}年(?:\d{1,2}月)?|\d{4}年度)'
        r'[^\n。]{0,12}'
        r'(?:竣工|完成|開業|供用|オープン|完工|引渡)'
        r'(?:予定|見込み|を予定|する予定)?'
    )
    _PERIOD_START_RE_CW = re.compile(
        r'(?:\d{4}年(?:\d{1,2}月)?|\d{4}年度)'
        r'[^\n。]{0,10}'
        r'(?:着工|工事着手|工事開始|着手)'
    )

    @staticmethod
    def _detect_phase_cw(content: str) -> str:
        for phase, (keywords, label) in ChatWorkNotifier._PHASE_PATTERNS_CW.items():
            if any(kw in content for kw in keywords):
                return label
        return "📄 情報"

    @staticmethod
    def _extract_period_cw(content: str) -> tuple[str, str]:
        """着工日・竣工予定日をテキストから抽出（start, end）"""
        start, end = "", ""
        m = ChatWorkNotifier._PERIOD_START_RE_CW.search(content)
        if m:
            start = m.group(0)[:30].strip()
        m = ChatWorkNotifier._PERIOD_END_RE_CW.search(content)
        if m:
            end = m.group(0)[:30].strip()
        return start, end

    @staticmethod
    def _bullets_cw(a: dict, max_bullets: int = 3) -> list[str]:
        """コンテンツから箇条書きリストを生成（最大 max_bullets 件）"""
        raw = (a.get("content") or a.get("summary") or "").strip()
        if not raw:
            return []
        raw = re.sub(r"[　 \t]+", " ", raw).strip()
        parts = []
        for para in re.split(r"\n+", raw)[:4]:
            parts.extend(re.split(r"(?<=。)", para))
        bullets = []
        for p in parts:
            p = p.strip().rstrip("。").strip()
            if len(p) < 15:
                continue
            if re.search(r'[Jj]ava[Ss]cript', p):
                continue
            if p not in bullets:
                bullets.append(p)
            if len(bullets) >= max_bullets:
                break
        return bullets

    def _build_message(self, articles: list[dict], report_date: str, suffix: str = "") -> str:
        date_str = report_date or datetime.now().strftime("%Y年%m月%d日")

        SEP = "━━━━━━━━━━━━━━━━━━━━━"

        lines = [
            f"[info][title]🏙 都市開発情報　{date_str}　{len(articles)}件{suffix}[/title]",
        ]

        for a in articles:
            title   = a.get("title", "").replace("【更新検知】", "").strip()
            content = a.get("content") or a.get("summary") or ""
            area    = detect_area(title, content, fallback=a.get("area", ""))
            url     = a.get("url", "")
            phase   = self._detect_phase_cw(content)
            start, end = self._extract_period_cw(content)
            bullets = self._bullets_cw(a)

            lines.append("")
            lines.append(SEP)
            lines.append(f"{phase}　📍 {area}")
            lines.append(f"【{title}】")
            # 期間情報
            if start or end:
                period_parts = []
                if start:
                    period_parts.append(f"着工: {start}")
                if end:
                    period_parts.append(f"完成: {end}")
                lines.append("🗓 " + " → ".join(period_parts))
            # 箇条書き詳細
            if bullets:
                lines.append("")
                for b in bullets:
                    lines.append(f"・{b}")
            lines.append("")
            lines.append(f"🔗 {url}")

        lines.append("")
        lines.append(SEP)
        lines.append("[/info]")
        return "\n".join(lines)

    def _build_digest(self, articles: list[dict]) -> str:
        date_str = datetime.now().strftime("%Y年%m月%d日")
        high = [a for a in articles if a.get("priority") == "high"]
        medium = [a for a in articles if a.get("priority") == "medium"]
        normal = [a for a in articles if a.get("priority") == "normal"]

        lines = [
            f"[info][title]都市開発計画 日次ダイジェスト {date_str}[/title]",
            f"本日の収集: 計 {len(articles)} 件",
            f"  重要 {len(high)} 件 / 中 {len(medium)} 件 / 通常 {len(normal)} 件",
            "",
        ]

        if high:
            lines.append("[toaster] ■ 重要情報")
            for a in high:
                area = a.get("area", "")
                title = a.get("title", "").replace("【更新検知】", "").strip()
                url = a.get("url", "")
                lines.append(f"  【{area}】{title}")
                lines.append(f"  {url}")
            lines.append("")

        if medium:
            lines.append("[alarm] ■ 注目情報")
            for a in medium:
                area = a.get("area", "")
                title = a.get("title", "").replace("【更新検知】", "").strip()
                url = a.get("url", "")
                lines.append(f"  【{area}】{title}")
                lines.append(f"  {url}")
            lines.append("")

        if normal:
            lines.append("[info] ■ 通常情報")
            for a in normal:
                area = a.get("area", "")
                title = a.get("title", "").replace("【更新検知】", "").strip()
                lines.append(f"  【{area}】{title}")
            lines.append("")

        lines.append("詳細: ~/urban-dev-tracker/reports/latest.md を参照")
        lines.append("[/info]")
        return "\n".join(lines)


def test_connection(token: str, room_id: str) -> bool:
    """接続テスト用: 自分のルームに疎通確認メッセージを送る"""
    notifier = ChatWorkNotifier(token, room_id)
    test_msg = (
        "[info][title]urban-dev-tracker 接続テスト[/title]"
        "ChatWork連携の設定が完了しました！\n"
        f"設定日時: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}"
        "[/info]"
    )
    return notifier._post(test_msg)
