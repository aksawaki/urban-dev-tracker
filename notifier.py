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
    ("岡山市", "岡山市"), ("北広島市", "北広島市"), ("広島市", "広島市"), ("北九州市", "北九州市"),
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
    # 健康経営・ESG認定（企業HR情報、都市開発でない）
    "健康経営優良法人", "ホワイト500", "健康経営銘柄", "健康経営宣言",
    # ポイント・クーポン・キャンペーン（小売・交通）
    "ポイントキャンペーン", "使わなくちゃもったいない", "そうてつローゼン",
    # 市営住宅の行政手続き（入居者募集・結果通知、都市開発でない）
    "市営住宅の敷地を活用した", "市営住宅用地の使用事業者",
    "市営住宅.*募集の結果",
    # エネルギー・モビリティ（都市開発でない）
    "再生可能エネルギー事業", "シェアモビリティ", "再エネ事業",
    # 観光地・リゾート開業（都市開発でない）
    "城ヶ島", "城ケ島",
    # 行政委員・条例（都市開発でない市民活動）
    "条例推進委員", "みんなでまちをきれいにする",
    # 不動産投資サービス広告（収益物件売買、都市開発でない）
    "不動産会社様向け",
    # 建設会社・ゼネコンの人事・組織改編（都市開発でない）
    "人事異動", "機構改革",
    # 花粉・自然環境（都市開発でない）
    "花粉", "企業の森",
    # 環境規制・CO2削減制度（都市開発でない）
    "キャップ＆トレード",
    # ウォークラリー・スポーツイベント
    "ウォークラリー",
    # 観光資源・日本遺産（開発計画でない文化観光記事）
    "観光資源化", "日本遺産",
    # 体育館・スポーツ施設の解体（都市開発でない）
    "体育館解体",
    # 医療・福祉施設の解体（都市開発でない）
    "医療C東西棟解体", "医療センター解体",
    # 上下水道インフラ工事（都市開発でない）
    "送水管", "配水管", "下水道管",
    # 道路インフラ調査・計画（都市開発でない行政道路事業）
    "事業化前調査", "骨格幹線", "社整審道小委",
    # 噴水・光・音ショー（イベント演出、都市開発でない）
    "アクアシンフォニー", "噴水ショー", "光と音のショー",
    # 展望タワー・観光施設（都市開発でない観光インフラ）
    "展望タワー",
    # こども園・保育施設（市町村の公共施設、都市開発でない）
    "こども園新築", "こども園の設計", "保育園新築",
    # 完成済み建物の広報記事（開発情報でない）
    "TOFROM YAESU",
    # 廃棄物・環境施設（都市開発でない）
    "再資源化施設", "焼却施設", "廃棄物処理施設",
    # シェアオフィス・コワーキング開業（テナント情報、都市開発でない）
    "シェアオフィス",
    # 行政調査・統計（都市開発でない）
    "物資流動調査", "交通計画協議会",
    # コーポラス（古い組合型住宅の解体・建替、都市開発でない）
    "コーポラス",
    # 地方単体ビルの建替（都市開発でない単独建替）
    "第一生命ビル",
    # 工場・製造施設（都市開発でない産業施設）
    "新工場", "工場建設",
    # 博物館・美術館建設（都市開発でない文化施設）
    "博物館の建設", "博物館建設",
    # 都市公園整備（都市開発でない緑地整備）
    "都市公園整備",
    # ダム・治水インフラ（都市開発でない）
    "定礎",
    # 建設技術・工法ニュース（都市開発でない）
    "CUW工法", "免震工法",
    # 宇宙・月面開発（都市開発でない）
    "月面基地", "月の砂",
    # 物流施設（冷凍倉庫・物流センター）
    "冷凍自動倉庫", "冷凍倉庫",
    # 大学施設（都市開発でない教育施設）
    "大学施設新築",
    # 観光拠点施設（都市開発でない観光インフラ）
    "観光拠点施設",
    # 不動産取得・売買ニュース（開発計画でない）
    "本社ビルを取得",
    # 図書館・体育館複合施設PFI（都市開発でない公共施設）
    "図書館・体育館",
    # サッカースタジアム（都市開発でないスポーツ施設）
    "サッカースタジアム",
    # 製薬工場（フィルター漏れ補完）
    "製薬工場",
    # 健康福祉ゾーン（都市開発でない公共福祉施設）
    "健康福祉ゾーン",
    # 大型物流施設（都市開発でない物流不動産）
    "大型物流",
    # 新庁舎（行政庁舎系を一括除外）
    "新庁舎",
    # 総合健康センター（公共健康施設、都市開発でない）
    "総合健康センター",
    # 文化複合施設（公共文化施設、都市開発でない）
    "文化複合施設",
    # ビル解体（単体建替、都市開発でない）
    "ビル解体",
    # 砂防・治山工事（地方インフラ、都市開発でない）
    "砂防",
    # 環境影響評価書（道路・インフラ手続き文書、都市開発でない）
    "環境影響評価書",
    # 事務所ビル（小規模単体ビル、都市開発でない）※タイトル末尾切れ対策で「事務所ビ」で前方一致
    "事務所ビ",
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
    # ポイント・クーポン・キャンペーン（小売・交通）
    r"ポイント.{0,15}キャンペーン",
    r"キャンペーン.{0,10}開催.{0,30}\d{4}年",  # 「キャンペーン開催（2026年...」
    # 健康経営・表彰（HR情報）
    r"健康経営.{0,15}認定|ホワイト500",
    # 行政募集・結果通知
    r"募集の結果について|事業者.*募集.*結果",
    # 公聴会・公開説明会（都市計画手続きの告知ページ、実質コンテンツなし）
    r"公聴会.{0,10}(開催|について)",
    # 不動産投資サービス・収益物件広告
    r"不動産.{0,10}(様向け|向けサービス|会社向け)",
    # 公立小中高校の建設・新築工事（都市開発でない行政施設）
    r"(小学校|中学校|高等学校).{0,10}(新校舎|建設予定地|建設地|建設場所|建設工事|新築工事)",
    # 行政部署の電話番号変更・廃止通知（都市開発でない）
    r"電話番号.{0,5}(廃止|変更|移転)",
    # 中小企業向けイノベーション・助成支援（都市開発でない）
    r"(戦略的イノベーション|イノベーション促進支援)",
    # 企業ニュースページのナビゲーション（旧社名ページ等）
    r"^ニュース（旧.+）$",
    # 「○○駅・エリアを取材」型の紹介記事（開発計画でない）
    r"(駅|エリア|スポット|施設).{0,30}を取材$",
    # 行政・設計コンサルの計画策定支援プロポ（開発でない調達通知）
    r"基本計画策定支援",
    # 体育館・スポーツ施設の解体監理（都市開発でない）
    r"体育館.{0,5}解体",
    # 公共施設（医療・福祉）の解体・改修工事（都市開発でない）
    r"(医療C|医療センター|病院).{0,15}(解体|撤去)",
    # 地方自治体の庁舎建設工事入札・再公告（都市開発でない公共工事調達）
    r"(新庁舎|庁舎).{0,5}建設工事",
    r"(参加申請|参加表明).{0,15}(再公告|公告)",
    # 「○○市にマンション、○月着工」型の単発マンション着工記事（都市開発の流れでない）
    r"[都道府県市区町村]にマンション[、。,]",
    # 工場建設（地方工業施設、都市開発でない）
    r"工場.{0,3}(を建設|建設.*着工|建て替え|建替)",
    # 参加申請・参加表明・参加受付（調達通知全般）
    r"(参加申請|参加表明|参加受付).{0,30}(まで|締切|受付|再公告|公告)",
    # ○月○日まで受付/締切（調達・業務委託の締切告知）
    r"\d+月\d+日まで.{0,10}(受付|締切)",
    # 単体マンション着工・完成（大規模再開発でない単発マンション記事）
    r"[都道府県市区町村]のマンション.{0,5}(着工|完成)",
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
    # 鉄道運行情報（コンテンツではなくサイト上部のスクロールテロップ）
    "【運行情報】",
    "京急線は平常通り運転",
    "次の路線にて振替輸送",
    "ＩＣ乗車券チャージ残額",
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

    # 重複判定のソース優先度（小さいほど優先）
    _SRC_RANK: dict[str, int] = {
        "kensetsunews_p2":     0,
        "kensetsunews_sokuho": 1,
        "kensetsunews_p3":     2,
        "kensetsunews_kanto":  3,
        "kensetsunews_kansai": 3,
        "kensetsunews_csk":    3,
        "kensetsunews_ht2":    3,
    }

    @staticmethod
    def _title_bigrams(title: str) -> set[str]:
        t = re.sub(r"\s+", "", title)
        return {t[i: i + 2] for i in range(len(t) - 1)}

    def _dedup_by_title(self, articles: list[dict], threshold: float = 0.4) -> list[dict]:
        """タイトルの文字bigram類似度で重複記事を除去。
        ソース優先度の高い記事（p2 > sokuho > p3 > 地域版）を優先して残す。
        """
        sorted_arts = sorted(
            articles,
            key=lambda a: self._SRC_RANK.get(a.get("source_id", ""), 5),
        )
        kept: list[dict] = []
        for a in sorted_arts:
            bg_a = self._title_bigrams(a.get("title", ""))
            is_dup = any(
                (lambda bg_k: len(bg_a & bg_k) / max(min(len(bg_a), len(bg_k)), 1) >= threshold)(
                    self._title_bigrams(k.get("title", ""))
                )
                for k in kept
            )
            if not is_dup:
                kept.append(a)
        return kept

    def send(self, articles: list[dict], report_date: str = "") -> bool:
        """対象記事をChatWorkに送信。記事数が多い場合は分割して送る。"""
        # 同一イベントの重複記事を除去（全国版優先）
        articles = self._dedup_by_title(articles)
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

    # タイトル末尾の更新日時サフィックス除去用
    _TITLE_SUFFIX_RE_CW = re.compile(
        r'\s*最終更新\s*[|｜]\s*\d{4}/\d{2}/\d{2}.*$|'
        r'\s*【速報】.*$',
        re.DOTALL,
    )
    # enrich_content の建設関連フィルタ（viewer._ENRICH_RELEVANT_RE と同一）
    _ENRICH_RELEVANT_RE_CW = re.compile(
        r'建設|工事|着工|竣工|施工|開発|整備|改修|建替|新築|建築|ゼネコン|再開発|'
        r'不動産|業務代行|入札|落札|タワー|区画整理|土地区画|公共工事'
    )

    @classmethod
    def _clean_title_cw(cls, title: str) -> str:
        return cls._TITLE_SUFFIX_RE_CW.sub('', title).strip()

    @staticmethod
    def _bullets_cw(a: dict, max_bullets: int = 2) -> list[str]:
        """コンテンツ or enrich から箇条書きリストを生成（最大 max_bullets 件）"""
        title_norm = re.sub(r'[^\w]', '', ChatWorkNotifier._clean_title_cw(
            (a.get("title") or "").replace("【更新検知】", "").strip()
        ), flags=re.UNICODE)

        # content から生成（タイトル重複除外）
        raw = (a.get("content") or a.get("summary") or "").strip()
        content_bullets = []
        if raw:
            raw = re.sub(r"[　 \t]+", " ", raw).strip()
            parts = []
            for para in re.split(r"\n+", raw)[:4]:
                parts.extend(re.split(r"(?<=。)", para))
            for p in parts:
                p = p.strip().rstrip("。").strip()
                if len(p) < 15:
                    continue
                if re.search(r'[Jj]ava[Ss]cript', p):
                    continue
                p_norm = re.sub(r'[^\w]', '', p, flags=re.UNICODE)
                if p_norm in title_norm or title_norm in p_norm:
                    continue
                if p not in content_bullets:
                    content_bullets.append(p)
                if len(content_bullets) >= max_bullets:
                    break

        if content_bullets:
            return content_bullets

        # enrich_content から生成（Google News RSS 由来）
        enrich = (a.get("enrich_content") or "").strip()
        if not enrich:
            return []
        is_gnews = enrich.startswith("【関連報道】")
        raw_lines = [l.strip().lstrip("・") for l in enrich.splitlines()
                     if l.strip() and l.strip() != "【関連報道】"]
        filtered = ([l for l in raw_lines if ChatWorkNotifier._ENRICH_RELEVANT_RE_CW.search(l)]
                    if is_gnews else raw_lines)

        deduped: list[str] = []
        seen_prefixes: list[str] = []
        for line in filtered:
            ln = re.sub(r'[^\w]', '', line, flags=re.UNICODE)
            if title_norm and (ln[:20] in title_norm or title_norm[:20] in ln):
                continue
            prefix = ln[:15]
            if prefix in seen_prefixes:
                continue
            deduped.append(line)
            seen_prefixes.append(prefix)
            if len(deduped) >= max_bullets:
                break
        return deduped

    def _build_message(self, articles: list[dict], report_date: str, suffix: str = "") -> str:
        date_str = report_date or datetime.now().strftime("%Y年%m月%d日")

        SEP = "━━━━━━━━━━━━━━━━━━━━━"

        lines = [
            f"[info][title]🏙 都市開発情報　{date_str}　{len(articles)}件{suffix}[/title]",
        ]

        try:
            from viewer import _effective_area as _eff_area
        except Exception:
            _eff_area = None

        import urllib.parse as _up
        for a in articles:
            title   = self._clean_title_cw(a.get("title", "").replace("【更新検知】", "").strip())
            content = a.get("content") or a.get("summary") or ""
            area    = (_eff_area(a) if _eff_area else None) or detect_area(title, content, fallback=a.get("area", ""))
            url     = a.get("url", "")
            gnews   = a.get("enrich_source", "")
            source_id = a.get("source_id", "")
            phase   = self._detect_phase_cw(content + " " + title)
            start, end = self._extract_period_cw(content)
            bullets = self._bullets_cw(a)

            # kensetsunews は有料記事なので Google News 検索リンクを自動生成
            if not gnews and source_id.startswith("kensetsunews"):
                gnews = (
                    "https://news.google.com/search?q="
                    + _up.quote(title)
                    + "&hl=ja&gl=JP&ceid=JP%3Aja"
                )

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
                    b_short = b[:50] + ("…" if len(b) > 50 else "")
                    lines.append(f"・{b_short}")
            lines.append("")
            lines.append(f"🔗 {url}")
            if gnews:
                lines.append(f"🔍 Google News関連 → {gnews}")

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
