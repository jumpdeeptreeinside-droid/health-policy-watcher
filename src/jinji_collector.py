#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人事ウォッチ収集（2026-07-07 ロードマップ2・ウィークリー自動欄の原材料）。

2つの公開ソースから人事イベントを集め、ウィークリー用のMarkdownセクションを自動生成する:
  1. 厚労省幹部名簿PDF（更新検知→前回名簿との差分＝誰がどの官職に就いたか）
  2. TDnet適時開示（上場ドラッグ/調剤/医薬品卸のウォッチリスト×人事系キーワード）

使い方:
  python3 src/jinji_collector.py --daily        # 毎日実行（launchd）: 名簿チェック+TDnet収集+セクション再生成
  python3 src/jinji_collector.py --tdnet-backfill 7   # TDnetを過去N日ぶん遡って収集（--dry-run で読むだけ）
  python3 src/jinji_collector.py --weekly       # セクションだけ再生成（直近7日）
  python3 src/jinji_collector.py --self-test    # 語の拾い方・ページの終わり・0件の裏取りを陽性対照つきで確認

🔴 取れなかった日は黙って0件にしない（2026-10-17）:
   TDnetを読めなかった日があれば標準出力と ntfy で知らせ、終了コードを1にする。
   週次セクションも、読めていない日がある窓では「ありませんでした」と書かず「不完全です」と書く。
   0件のときは TDnet 自身の「開示された情報はありません」の文と、語の層の陽性対照で裏を取る。

データ: ~/jinji_data/（events.jsonl・mhlw_rosters/・state.json）
出力:   output/jinji/section_latest.md（publish_weekly.pyが自動で差し込む）
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

DATA = os.path.expanduser("~/jinji_data")
ROSTERS = os.path.join(DATA, "mhlw_rosters")
EVENTS = os.path.join(DATA, "events.jsonl")
STATE = os.path.join(DATA, "state.json")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "jinji")
UA = {"User-Agent": "Mozilla/5.0 (CrossHealth research; contact: info@crosshealthjp.org)"}
WAIT = 1.5
# 1日のページ送りの上限（暴走止め）。🔴 ここに当たったら黙って打ち切らず ok=False で返す
#（件数で打ち切って黙る、が今回直した不具合そのものなので、同じ形を作らない）。実測の最大は6ページ。
MAX_PAGES = 40

KANBU_INDEX = "https://www.mhlw.go.jp/kouseiroudoushou/kanbumeibo/index.html"
TDNET_LIST = "https://www.release.tdnet.info/inbs/I_list_{page:03d}_{ymd}.html"
TDNET_BASE = "https://www.release.tdnet.info/inbs/"

# ウォッチリスト（証券コード4桁→(表示名, カテゴリ)・2026-07-07 木内さん決定）。足すだけで増える。
# 構成: 調剤専業5＋ドラッグストア12＋医薬品卸5＋内資製薬10＋CRO関連（上場社）＝35社。
# 「調剤大手10社」はドラッグ側の調剤併設大手（ウエルシア/ツルハ/スギ/マツキヨ/クリエイトSD）込みでカバー。
# TDnetは上場企業のみ＝非上場はここでは拾えない: 調剤（クラフト/総合メディカル/I&H等）、
# 外資製薬日本法人（ファイザー/MSD等）、CRO大手（IQVIA/シミック=2023MBO/EPS/パレクセル等）
# → 外資・非上場は各社ニュースルーム監視（別レイヤー・実装予定）でカバーする。
WATCHLIST_FULL = {
    # 調剤専業
    "9627": ("アインHD", "調剤"),
    "3341": ("日本調剤", "調剤"),
    "3034": ("クオールHD", "調剤"),
    "4350": ("メディカルシステムネットワーク", "調剤"),
    "2796": ("ファーマライズHD", "調剤"),
    # ドラッグストア（調剤併設含む）
    "3141": ("ウエルシアHD", "ドラッグストア"),
    "3391": ("ツルハHD", "ドラッグストア"),
    "3088": ("マツキヨココカラ&カンパニー", "ドラッグストア"),
    "3349": ("コスモス薬品", "ドラッグストア"),
    "9989": ("サンドラッグ", "ドラッグストア"),
    "7649": ("スギHD", "ドラッグストア"),
    "3148": ("クリエイトSDHD", "ドラッグストア"),
    "2664": ("カワチ薬品", "ドラッグストア"),
    "7679": ("薬王堂HD", "ドラッグストア"),
    "9267": ("Genky DrugStores", "ドラッグストア"),
    "3549": ("クスリのアオキHD", "ドラッグストア"),
    "3544": ("サツドラHD", "ドラッグストア"),
    # 医薬品卸
    "7459": ("メディパルHD", "医薬品卸"),
    "2784": ("アルフレッサHD", "医薬品卸"),
    "9987": ("スズケン", "医薬品卸"),
    "8129": ("東邦HD", "医薬品卸"),
    "3151": ("バイタルケーエスケーHD", "医薬品卸"),
    # 内資製薬10社
    "4502": ("武田薬品工業", "製薬(内資)"),
    "4503": ("アステラス製薬", "製薬(内資)"),
    "4568": ("第一三共", "製薬(内資)"),
    "4519": ("中外製薬", "製薬(内資)"),
    "4523": ("エーザイ", "製薬(内資)"),
    "4578": ("大塚HD", "製薬(内資)"),
    "4507": ("塩野義製薬", "製薬(内資)"),
    "4151": ("協和キリン", "製薬(内資)"),
    "4528": ("小野薬品工業", "製薬(内資)"),
    "4506": ("住友ファーマ", "製薬(内資)"),
    # CRO関連（上場社）※アイロムグループ(2372)は2025-05にMBOで上場廃止→ニュースルーム監視側へ
    "2183": ("リニカル", "CRO"),
    "2395": ("新日本科学", "CRO"),
}
WATCHLIST = {k: v[0] for k, v in WATCHLIST_FULL.items()}
CATEGORY = {k: v[1] for k, v in WATCHLIST_FULL.items()}
# M&A・再編ウォッチ（2026-07-08未明 木内さん発案）。承継事業のレーダーを兼ねる。
#
# 🔴 2026-10-17 まで「言い回し」をそのまま並べていた（"株式取得" "事業譲渡" "資本業務提携" 等）。
#    開示のタイトルは助詞や言い換えが入るので、同じ中身でも落ちる:
#      「株式の取得に関するお知らせ」      ← "株式取得" に当たらない
#      「事業の譲受に関するお知らせ」      ← "事業譲渡" に当たらない
#      「業務提携に関するお知らせ」        ← "資本業務提携"/"資本提携" に当たらない
#      「特定子会社の異動に関するお知らせ」← "子会社化"/"完全子会社" に当たらない
#    30日ぶん（2026-09-18〜10-17・上場全社 4,602件）で実測すると、核の名詞で広く抜いた
#    1,213件のうち 109件がこの型で落ちていた。[[feedback_grep_by_core_word]] の型。
#    → **核になる名詞の組**で引く。助詞「の」・送り仮名・修飾語をまたげる形にする。
#
# 🔴 禁止語と同じく、拾う語も **(語, 文脈) の組** で持つ（語だけだと別物を巻き添えにする）。
#    「自己株式の取得」「新株予約権」「譲渡制限付株式報酬」は "株式の取得"/"譲渡" を含むが M&A ではない。
#    実測では落ちていた 883件のうち 555件がこの手の別物だった＝VETO が無いと誤って拾う。
MA_RULES = (
    ("公開買付",   r"公開買付|TOB"),
    ("MBO",        r"MBO|マネジメント・?バイアウト"),
    ("買収",       r"買収|買集め|大規模買付"),
    ("合併",       r"合併"),
    ("経営統合",   r"経営統合|統合契約|統合に関する基本合意"),
    ("子会社",     r"(?:完全)?子会社化|完全子会社|(?:特定|連結|海外)?子会社の異動|子会社株式の(?:一部)?譲渡|孫会社の異動"),
    ("株式の取得", r"株式(?:等)?(?:の)?(?:一部)?取得|株式を(?:一部)?取得"),
    ("株式の譲渡", r"株式(?:等)?(?:の)?(?:一部)?譲渡|持分(?:の)?譲渡|株式売渡"),
    ("事業の譲渡", r"事業(?:の)?譲[渡受]|事業譲受|事業承継"),
    ("提携",       r"(?:資本|業務|事業|包括|経営)[・及びと]{0,3}(?:業務|資本)?提携"),
    ("持株会社",   r"持株会社|持ち株会社|ホールディングス体制"),
    ("株式交換",   r"株式交換|株式移転"),
    ("会社分割",   r"会社分割|吸収分割|新設分割|分割契約"),
    ("上場廃止",   r"上場廃止"),
)
# (語, 文脈) の組で外す＝M&Aの名詞を含むが M&A ではないもの。
# 🔴 ここは「当たったら全体を捨てる」ではなく **当たった所だけ伏せてから** M&A の語を見る。
#    全体を捨てると、別の本物の語が同じタイトルに入っていたときに一緒に捨ててしまう。実測:
#    「持株会社体制への移行に伴う代表取締役の異動（退任）及びその他役員の人事に関するお知らせ」
#    ＝持株会社体制への移行（本物のM&A）が「取締役の異動」のせいで人事に化けていた。
#    長い組み合わせを先に書く（「自己株式の公開買付け」は「自己株式」より先）。
MA_VETO = (
    ("自己株の公開買付", r"自己株式[^。]{0,8}公開買付|公開買付[^。]{0,8}自己株式"),
    ("自己株式",         r"自己株式"),
    ("新株予約権",       r"新株予約権|ストックオプション"),
    ("株式報酬",         r"譲渡制限付株式報酬|株式報酬|役員報酬"),
    # 🔴 間を [^。]{0,10} で空けると「不動産子会社**の株式**譲渡」をまたいで伏せ、
    #    本物の株式譲渡を消していた（ミハル監査 10/17）。間に本物の語が来たら当たらない形にする。
    ("資産の譲渡",       r"(?:資産|不動産|信託受益権|物件|土地|建物)(?:等)?"
                        r"(?:(?!株式|子会社|孫会社|持分|事業|会社)[^。]){0,10}?(?:の)?譲渡"),
    ("ファンド持分",     r"(?:匿名組合|投資事業有限責任組合|ファンド)[^。]{0,8}(?:出資)?持分"),
    ("役職の異動",       r"(?:執行)?役員の異動|(?:代表)?取締役の異動|人事異動"),
)
# 人事（こちらも (語, 文脈) の組で。M&Aに当たったものは M&A を優先する）
# 🔴 役職の語だけで引くと人事でないものを巻き添えにする。実測:
#    「当社及び当社グループ会社元取締役に対する最高裁判所の判決について」（9/28 アインHD）は
#    裁判の話で人事ではない。**役職の語＋動きの語**の組を要る形にした。
JINJI_POST = r"役員|取締役|監査役|執行役|社長|会長|頭取|CEO|COO|CFO|代表者|幹部"
JINJI_MOVE = r"異動|就任|退任|辞任|解任|選任|交代|交替|昇任|委嘱"
JINJI_SELF = r"人事|機構改革|組織変更|組織再編|組織改編"   # これ単体で人事と決まる語


def classify(title: str):
    """タイトルを "M&A" / "人事" / None に分ける。当たった語の名前も返す。

    M&A を先に見る（「子会社の異動」は人事ではなく再編として扱う）。
    返り値 = (topic か None, 当たった語の名前のリスト)
    """
    t = str(title)
    # 当たった所だけ伏せる（捨てるのは語ではなく、その語が占めている場所）
    masked, veto = t, []
    for n, p in MA_VETO:
        masked, k = re.subn(p, "〇", masked)
        if k:
            veto.append(n)
    ma = [n for n, p in MA_RULES if re.search(p, masked)]
    if ma:
        return "M&A", ma
    if re.search(JINJI_SELF, t):
        return "人事", ["人事・組織"]
    if re.search(JINJI_POST, t) and re.search(JINJI_MOVE, t):
        return "人事", ["役職＋動き"]
    return None, veto if veto else []


# TDnet は開示が無い日も 200 を返し、この文を出す（0件の裏取りに使う。下の tdnet_day 参照）
TDNET_EMPTY = "に開示された情報はありません"

# 非上場・外資のニュースルーム監視（TDnetに存在しない企業・2026-07-07 木内さん決定）。
# 各社のプレスリリース一覧ページを毎日巡回し、新着リンクのタイトルを人事キーワードで拾う。
# 初回はベースライン登録のみ（過去分でイベントを量産しない）。
# ベーリンガー/リリーは自社サイトがbot遮断・TLS問題のためPR TIMESの配信ページを使う。
NEWSROOMS = {
    # 外資製薬10社
    "pfizer": ("ファイザー", "製薬(外資)", "https://www.pfizer.co.jp/pfizer/company/press"),
    "msd": ("MSD", "製薬(外資)", "https://www.msd.co.jp/news/"),
    "astrazeneca": ("アストラゼネカ", "製薬(外資)", "https://www.astrazeneca.co.jp/media.html"),
    "novartis": ("ノバルティス", "製薬(外資)", "https://www.novartis.com/jp-ja/news"),
    "sanofi": ("サノフィ", "製薬(外資)", "https://www.sanofi.co.jp/ja/media-room/press-releases"),
    "gsk": ("GSK", "製薬(外資)", "https://jp.gsk.com/ja-jp/news/press-releases/"),
    "lilly": ("日本イーライリリー", "製薬(外資)", "https://prtimes.jp/companyrdf.php?company_id=5823"),
    "bms": ("ブリストル・マイヤーズ スクイブ", "製薬(外資)", "https://www.bms.com/jp/media.html"),
    "jnj": ("ジョンソン・エンド・ジョンソン", "製薬(外資)", "https://www.jnj.co.jp/media-center"),
    "boehringer": ("日本ベーリンガーインゲルハイム", "製薬(外資)", "https://prtimes.jp/companyrdf.php?company_id=2981"),
    # 非上場CRO
    "iqvia": ("IQVIAジャパン", "CRO(非上場)", "https://www.iqvia.com/ja-jp/newsroom"),
    "cmic": ("シミックHD", "CRO(非上場)", "https://www.cmicgroup.com/news"),
    "eps": ("EPSホールディングス", "CRO(非上場)", "https://www.eps-holdings.co.jp/news/"),
    "irom": ("アイロムグループ", "CRO(非上場)", "https://www.iromgroup.co.jp/news/"),
    # 非上場調剤大手
    "kraft": ("クラフト（さくら薬局）", "調剤(非上場)", "https://www.kraft-net.co.jp/news/"),
    "sogo": ("総合メディカル", "調剤(非上場)", "https://www.sogo-medical.co.jp/ja/news.html"),
    "ih": ("I&H（阪神調剤）", "調剤(非上場)", "https://i-h-inc.co.jp/news/ir.html"),
    "aisei": ("アイセイ薬局", "調剤(非上場)", "https://www.aisei.co.jp/news/"),
}

WAREKI = {"令和": 2018, "平成": 1988}


def get(url: str, binary=False, tries=3):
    """取りに行く。4xx はそのまま上へ（呼び手が 404 の意味を判断する）。
    それ以外（DNS・timeout・5xx）は間を置いて tries 回まで試し、最後は例外を上げる。
    🔴 黙って空を返さない＝呼び手が「0件」と「取れなかった」を区別できるようにする。"""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
    if binary:
        return raw
    for enc in ("utf-8", "shift_jis", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def load_state() -> dict:
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    return {}


def save_state(st: dict):
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def load_events() -> list:
    if not os.path.exists(EVENTS):
        return []
    return [json.loads(l) for l in open(EVENTS, encoding="utf-8") if l.strip()]


def append_events(evs: list):
    with open(EVENTS, "a", encoding="utf-8") as f:
        for e in evs:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def wareki_to_date(label: str):
    m = re.search(r"(令和|平成)([０-９\d元]+)年([０-９\d]+)月([０-９\d]+)日", label)
    if not m:
        return None
    z = str.maketrans("０１２３４５６７８９", "0123456789")
    y = 1 if m.group(2) == "元" else int(m.group(2).translate(z))
    return f"{WAREKI[m.group(1)] + y}-{int(m.group(3).translate(z)):02d}-{int(m.group(4).translate(z)):02d}"


# ---------- 厚労省幹部名簿 ----------

def parse_roster(txt: str) -> dict:
    """pdftotext -layout の出力から {官職: 氏名} を作る。官職の折返し行に対応。"""
    # 末尾が「漢字姓 漢字名 ふりがな(2語以上)」の行が1エントリ。
    # 官職の折返し行は名前より前にも後にも出る（PDF上で名前が折返しブロックの
    # 中央に印字されるため）。括弧の開閉バランスで前後どちらに属すかを判定。
    kan = r"[㐀-鿿豈-﫿々〇]"  # 互換漢字(﨑など)含む
    pat = re.compile(r"^(.*?)\s*(" + kan + r"+)\s+(" + kan + r"+)\s+([ぁ-ゖー]+(?:\s+[ぁ-ゖー]+)+)\s*$")
    def unbalanced(p: str) -> bool:
        return p.count("（") + p.count("(") > p.count("）") + p.count(")")

    entries, buf = [], ""
    for line in txt.splitlines():
        s = line.rstrip()
        t = s.strip()
        if not t:
            continue
        if t.startswith("【") or "幹 部 名 簿" in t or re.match(r"^官\s*職", t) or re.match(r"^[-−ー\d\s]+$", t):
            buf = ""
            continue
        m = pat.match(s)
        if m:
            post = re.sub(r"\s+", "", buf + m.group(1))
            buf = ""
            if post:
                entries.append([post, m.group(2) + m.group(3)])
        else:
            frag = re.sub(r"\s+", "", t)
            if entries and unbalanced(entries[-1][0]):
                entries[-1][0] += frag  # 直前の官職の閉じ括弧待ち＝続き
            else:
                buf += frag  # 次エントリの官職の前半
    roster = {}
    for post, name in entries:
        key, n = post, 2
        while key in roster:  # 同名官職（秘書官等）は連番で区別
            key = f"{post}#{n}"
            n += 1
        roster[key] = name
    return roster


def diff_rosters(old: dict, new: dict) -> list:
    changes = []
    for post, name in new.items():
        if post not in old:
            changes.append({"kind": "新任", "post": post, "name": name})
        elif old[post] != name:
            changes.append({"kind": "交代", "post": post, "name": name, "prev": old[post]})
    for post, name in old.items():
        if post not in new:
            changes.append({"kind": "官職消滅", "post": post, "name": name})
    return changes


def check_mhlw(st: dict) -> list:
    soup = BeautifulSoup(get(KANBU_INDEX), "html.parser")
    link = None
    for a in soup.find_all("a", href=True):
        t = a.get_text(strip=True)
        if "幹部名簿" in t and a["href"].lower().endswith(".pdf"):
            link, label = a["href"], t
            break
    if not link:
        print("✗ 幹部名簿PDFリンクが見つからない（ページ構造変更?）")
        return []
    url = link if link.startswith("http") else "https://www.mhlw.go.jp" + link
    if st.get("mhlw_url") == url:
        print(f"  幹部名簿: 変更なし（{st.get('mhlw_label', '')}）")
        return []
    d = wareki_to_date(label) or date.today().isoformat()
    os.makedirs(ROSTERS, exist_ok=True)
    pdf = os.path.join(ROSTERS, f"{d}.pdf")
    open(pdf, "wb").write(get(url, binary=True))
    txt = pdf[:-4] + ".txt"
    subprocess.run(["pdftotext", "-layout", pdf, txt], check=True)
    roster = parse_roster(open(txt, encoding="utf-8").read())
    print(f"  幹部名簿: 更新検知 → {label}（{len(roster)}官職）")
    evs = []
    if st.get("mhlw_roster"):
        changes = diff_rosters(st["mhlw_roster"], roster)
        print(f"  前回名簿（{st.get('mhlw_label', '')}）との差分: {len(changes)}件")
        if changes:
            evs = [{"source": "mhlw", "date": d, "label": label, "changes": changes,
                    "url": url, "collected": datetime.now().isoformat(timespec="seconds")}]
    else:
        print("  初回のため基準名簿として保存（差分なし）")
    st.update({"mhlw_url": url, "mhlw_label": label, "mhlw_date": d, "mhlw_roster": roster})
    return evs


# ---------- TDnet ----------

def tdnet_day(d: date, seen: set):
    """その日の適時開示を最後のページまで読み、(イベント, その日の結果) を返す。

    🔴 2026-10-17 まで「数字5桁の行が100件未満ならそれが最後のページ」として打ち切っていた。
       TDnet の証券コードには 293A0 のような英字入りがあるので、1ページ100件でも
       数字だけ数えると78〜93件になり、**毎日 page1 で止まっていた**。
       実測（2026-09-18〜10-17・18営業日）: 全4,602件のうち読めていたのは1,701件＝**63%が未読**。
       このため 10/5 の塩野義製薬「米国IntraBio社の完全子会社化に関するお知らせ」（page2）が落ち、
       その週の M&A 欄が「開示・発表はありませんでした」になっていた（語の層は当たっていたのに、
       そもそも行を読んでいなかった＝語をいくら足しても直らない型）。
       → ページの終わりは **404** か **開示0件** で判断する。数えるのは英字入りも含む全開示。

    🔴 同じく、どんな例外も `except Exception: break` で「その日はページなし」として黙って0件に
       していた。DNS失敗・403・timeout でも「なし」に見える。404（＝一覧がまだ無い日）と
       それ以外を分け、それ以外は ok=False で呼び手に返す。[[feedback_dont_dismiss_your_own_alarm]]

    🔴 0件のときは TDnet 自身の文で裏を取る（[[feedback_fulltext_zero_hits]] 0件は結論ではない）。
       開示が無い日の一覧は 200 で「<その日付> に開示された情報はありません。」と書く。
       **その日付で**その文が出ていれば0件は本物。出ていなければ読めていないと見なす。
    """
    ymd = d.strftime("%Y%m%d")

    def done(pages, rows, ok, why):
        return evs, {"date": d.isoformat(), "pages": pages, "rows": rows,
                     "hits": len(evs), "ok": ok, "why": why}

    evs, rows, pages, page = [], 0, 0, 1
    while True:
        if page > MAX_PAGES:
            return done(pages, rows, False, f"ページが{MAX_PAGES}を超えました（読み切れていません）")
        try:
            html = get(TDNET_LIST.format(page=page, ymd=ymd))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                if page == 1:
                    # 🟡 404 は2つの意味がある（ミハル監査 10/17）。
                    #    ・まだ先の日付／土日 → 一覧が無いのが正しい
                    #    ・過去の平日 → TDnet の保管期間（およそ40日）を過ぎた＝**読めなかった**
                    #      これを「0件」と記録すると、遡れない日が静かに「開示なし」になる。
                    if d > date.today():
                        return done(0, 0, True, "まだ先の日付（一覧なし）")
                    if d.weekday() >= 5:
                        return done(0, 0, True, "一覧なし（404・土日）")
                    return done(0, 0, False, "平日なのに一覧が404（保管期間を過ぎて遡れない日）")
                break  # 最後のページの次＝正常な終わり
            return done(pages, rows, False, f"page{page} が HTTP {e.code}")
        except Exception as e:
            return done(pages, rows, False, f"page{page} が {type(e).__name__}: {str(e)[:60]}")

        soup = BeautifulSoup(html, "html.parser")
        n = 0
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            tm, code, company, title = (td.get_text(strip=True) for td in tds[:4])
            # 🔴 証券コードは数字だけではない（293A0 のような英字入りがある）。
            #    ここで数え落とすと「最後のページ」の判断が狂うので、全部数えてから絞る。
            if not re.match(r"^[0-9A-Z]{5}$", code):
                continue
            n += 1
            code4 = code[:4]
            if code4 not in WATCHLIST:
                continue
            topic, hit = classify(title)
            if not topic:
                continue
            a = tds[3].find("a", href=True)
            url = (TDNET_BASE + a["href"]) if a else ""
            key = f"{ymd}|{code4}|{title}"
            if key in seen:
                continue
            seen.add(key)
            evs.append({"source": "tdnet", "date": d.isoformat(), "time": tm,
                        "code": code4, "company": WATCHLIST[code4],
                        "category": CATEGORY.get(code4, ""), "title": title, "url": url,
                        "topic": topic, "hit": hit,
                        "collected": datetime.now().isoformat(timespec="seconds")})
        pages, rows = page, rows + n
        if n == 0:
            if page > 1:
                # 🟡 最後のページの次は 404 が返る（実測）。200 で0件は本来起きない形なので
                #    「終わった」と決めずに読めなかった扱いにする（ミハル監査 10/17）。
                return done(pages, rows, False, f"page{page} が200で0件（最後のページの次は404のはず）")
            if page == 1:
                # 0件の裏取り（陽性対照）。日付も照らす＝別の日のページを読んでいないこと。
                # 🔴 一覧の日付は0埋め（「2026年09月19日」）。埋めずに照らすと一桁の月日で全部外れる
                if TDNET_EMPTY in html and d.strftime("%Y年%m月%d日") in html:
                    # 🔑 理由の頭を「開示なし（」にそろえる。失敗の理由にも
                    #    「開示された情報はありません」が入るので、その語では経路を見分けられない
                    #    （ミハル監査 10/17）。自己検査はこの頭で照らす。
                    return done(1, 0, True, "開示なし（TDnet がその日付で「開示された情報はありません」）")
                return done(1, 0, False, "0件だが「開示された情報はありません」がその日付で出ていない")
            break
        page += 1
        time.sleep(WAIT)
    return done(pages, rows, True, "")


def record_day(st: dict, info: dict):
    """その日ぶんを読めたかを state に残す（週次の欄が「なし」と書いていいかの判断に使う）。"""
    dd = st.setdefault("tdnet_days", {})
    dd[info["date"]] = {k: info[k] for k in ("pages", "rows", "hits", "ok", "why")}
    for k in sorted(dd)[:-120]:   # 120日より古いぶんは落とす
        del dd[k]


def notify(title: str, body: str):
    """無人ジョブなのでログに書くだけにしない。ntfy は話題名＝鍵の公開サービスなので**数だけ**。"""
    env = os.path.expanduser("~/sakuma/.env")
    topic = ""
    if os.path.exists(env):
        for line in open(env, encoding="utf-8", errors="ignore"):
            if line.startswith("NTFY_TOPIC="):
                topic = line.split("=", 1)[1].strip()
                break
    if not topic:
        return
    try:
        # 🔴 HTTPヘッダは latin-1 でしか送れない（urllib）。日本語の見出しは生のUTF-8を
        #    そのまま載せる（curl と同じ振る舞い）。しないと UnicodeEncodeError で黙って通知が消える。
        req = urllib.request.Request(f"https://ntfy.sh/{topic}",
                                     data=body.encode("utf-8"),
                                     headers={"Title": title.encode("utf-8").decode("latin-1"),
                                              "Content-Type": "text/plain; charset=utf-8"})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"  ✗ 通知できませんでした: {type(e).__name__}")


# ---------- ニュースルーム（非上場・外資） ----------

def scan_newsrooms(st: dict) -> list:
    """各社プレスリリース一覧を巡回し、新着×人事キーワードをイベント化。

    初回はベースライン登録のみ。URL集合の差分＝新着（タイトル変更には追従しない）。
    """
    from urllib.parse import urljoin
    seen_map = st.setdefault("newsroom_seen", {})
    evs = []
    for key, (name, cat, url) in NEWSROOMS.items():
        try:
            body = get(url)
        except Exception as e:
            print(f"  ✗ newsroom {name}: {type(e).__name__} {str(e)[:60]}")
            continue
        items = {}
        if "<rdf:RDF" in body[:500] or "<rss" in body[:500]:
            # RSS（PR TIMES等）: <item>のtitle/linkを拾う
            for m in re.finditer(r"<item[^>]*>.*?<title>(.*?)</title>.*?<link>(.*?)</link>", body, re.S):
                t = re.sub(r"\s+", " ", re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip()
                if t:
                    items[m.group(2).strip()] = t[:120]
        else:
            soup = BeautifulSoup(body, "html.parser")
            for a in soup.find_all("a", href=True):
                t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
                if len(t) < 12:  # ナビ・ボタン類を除外（見出しリンクだけ拾う）
                    continue
                items[urljoin(url, a["href"])] = t[:120]
        first = key not in seen_map
        seen = set(seen_map.get(key, []))
        new = {u: t for u, t in items.items() if u not in seen}
        hit = 0
        if not first:
            for u, t in new.items():
                topic, which = classify(t)
                if topic:
                    hit += 1
                    evs.append({"source": "newsroom", "date": date.today().isoformat(),
                                "company": name, "category": cat, "title": t, "url": u,
                                "topic": topic, "hit": which,
                                "collected": datetime.now().isoformat(timespec="seconds")})
        seen.update(items)
        seen_map[key] = sorted(seen)[-800:]  # 肥大化防止
        print(f"  newsroom {name}: リンク{len(items)}件 新着{len(new)}件"
              + ("（初回＝ベースライン）" if first else f" ヒット{hit}件"))
        time.sleep(WAIT)
    return evs


# ---------- ウィークリー用セクション ----------

def window_health(days=7) -> dict:
    """直近 days 日のうち、TDnet をちゃんと読めた日・読めなかった日・記録の無い日を数える。

    🔴 この欄は「ありませんでした」と断言する欄なので、**読めていない日が1日でもあれば
       断言してはいけない**（[[feedback_fulltext_zero_hits]] 0件は結論ではない）。
       読めたかは tdnet_day がその場で state に残した記録（record_day）で見る。
    """
    dd = load_state().get("tdnet_days", {})
    win = [(date.today() - timedelta(days=i)).isoformat() for i in range(days, -1, -1)]
    bad = sorted(x for x in win if x in dd and not dd[x]["ok"])
    unknown = sorted(x for x in win if x not in dd)
    return {"days": win, "bad": bad, "unknown": unknown,
            "rows": sum(dd[x]["rows"] for x in win if x in dd),
            "read": sorted(x for x in win if x in dd and dd[x]["ok"]),
            "complete": not bad and not unknown}


def incomplete_note(h: dict) -> str:
    """読めていない日があるときに「ありませんでした」の代わりに書く1行。"""
    if h["complete"]:
        raise RuntimeError("全部読めている窓で incomplete_note を呼んでいます（呼び手の条件が逆）")
    parts = []
    if h["bad"]:
        parts.append(f"取れなかった日 {len(h['bad'])}日（{'・'.join(d[5:] for d in h['bad'])}）")
    if h["unknown"]:
        parts.append(f"収集の記録が無い日 {len(h['unknown'])}日（{'・'.join(d[5:] for d in h['unknown'])}）")
    return ("🔴 この欄は不完全です：" + "／".join(parts)
            + "。「該当なし」とは読まないでください（TDnetの一覧を読めた日は "
            + f"{len(h['read'])}日・開示 {h['rows']}件）。")


def build_weekly_section(days=7) -> str:
    since = (date.today() - timedelta(days=days)).isoformat()
    evs = [e for e in load_events() if e["date"] >= since]
    health = window_health(days)
    mhlw = [e for e in evs if e["source"] == "mhlw"]
    corp = sorted([e for e in evs if e["source"] in ("tdnet", "newsroom")],
                  key=lambda e: (e["date"], e.get("time", "")))
    tdnet = [e for e in corp if e.get("topic", "人事") == "人事"]
    ma = [e for e in corp if e.get("topic") == "M&A"]
    lines = ["## 今週の人事ウォッチ", ""]
    lines.append("### 厚労省幹部")
    if mhlw:
        for e in mhlw:
            lines.append(f"**{e['label']}**（[名簿PDF]({e['url']})）に更新。主な変化:")
            lines.append("")
            shown = e["changes"][:15]
            for c in shown:
                if c["kind"] == "交代":
                    lines.append(f"- {c['post']}：{c['prev']} → **{c['name']}**")
                elif c["kind"] == "新任":
                    lines.append(f"- {c['post']}（新設/新任）：**{c['name']}**")
                else:
                    lines.append(f"- {c['post']}：廃止・転出（前任 {c['name']}）")
            if len(e["changes"]) > len(shown):
                lines.append(f"- ほか{len(e['changes']) - len(shown)}件")
            lines.append("")
    else:
        lines += ["今週、幹部名簿の更新はありませんでした。", ""]
    lines.append("### 企業（調剤・ドラッグストア・医薬品卸・製薬・CRO）")
    if tdnet:
        order = ["調剤", "調剤(非上場)", "ドラッグストア", "医薬品卸",
                 "製薬(内資)", "製薬(外資)", "CRO", "CRO(非上場)", ""]
        for cat in order:
            grp = [e for e in tdnet if e.get("category", "") == cat]
            if not grp:
                continue
            if cat:
                lines.append(f"**{cat}**")
            for e in grp:
                md = f"{int(e['date'][5:7])}/{int(e['date'][8:10])}"
                t = f"[{e['title']}]({e['url']})" if e["url"] else e["title"]
                lines.append(f"- {md}　**{e['company']}**　{t}")
    elif health["complete"]:
        lines.append("今週、ウォッチ対象企業の人事関連開示はありませんでした。")
    else:
        lines.append(incomplete_note(health))
    # M&A・再編ウォッチ（承継・業界再編のレーダー）
    lines += ["", "## 今週のM&A・再編ウォッチ", ""]
    if ma:
        for e in ma:
            md = f"{int(e['date'][5:7])}/{int(e['date'][8:10])}"
            t = f"[{e['title']}]({e['url']})" if e.get("url") else e["title"]
            lines.append(f"- {md}　**{e['company']}**（{e.get('category','')}）　{t}")
    elif health["complete"]:
        lines.append("今週、ウォッチ対象企業（調剤・ドラッグストア・医薬品卸・製薬・CRO 52社）のM&A・再編関連の開示・発表はありませんでした。")
    else:
        lines.append(incomplete_note(health))
    lines += ["", "（出典：厚生労働省 幹部名簿／TDnet適時開示／各社ニュースルーム。毎日自動収集）", ""]
    return "\n".join(lines)


BLOCK_FLAG = os.path.join(OUT_DIR, "section_blocked.flag")

# 欄の**作成日**を機械が読めるようにする印（モリの依頼・サクマさん決定 2026-10-18）。
#   publish_weekly.py は md の1行目のこの行を読んで今日と照らし、今日でなければ公開を止める。
#   差し込む前にこの行を取り除くので、会員の1冊には出ない。
#   ファイルの mtime では照らさない（コピー・touch・再配置で日付が変わる＝作ったときの日付ではない）。
# 🔴 形を変えるときは publish_weekly.py の正規表現と**同時に**変える（先に言う）。
#    印が無い＝「作成日が読めない」として向こうが止まるので、消すだけでも公開が止まる。
CREATED_MARK = "<!-- jinji-section-created: {} -->"


def write_section(days=7):
    """セクションを作る。**窓が不完全なら止める側に倒す**（サクマさん決定 2026-10-17）。

    🔴 「この欄は不完全です」がそのまま会員の1冊に載ってしまうので、載せる前に止める。
       止め方＝`section_blocked.flag` を置き、ntfy で**件数だけ**知らせ、終了コードを1にする。
       セクション自体は作って残す（中を見て、直すか注記を付けて出すかを翔太さんが決める）。

    🔴 **この通知だけでは守りにならない**（2026-10-17 の監査で私の思い違いが出た）。
       私は「欄は土曜07:05・公開は日曜05:55なので1日の猶予がある」と書いたが、**誤り**。
       日曜05:55 の LaunchAgent は用意済みの JSON を commit/push するだけで、1冊は作り直さない。
       会員の1冊（KV）に欄が入るのは **publish_weekly.py を回した時点**。実測:
         w20261018 … 欄 10/17(土)07:06 → 1冊 10/17(土)08:27（差 約1時間20分）
         w20261011 … 欄 10/10(土)07:05 → 1冊 10/10(土)10:38
       猶予は1時間ほどしかない。**止めるのは publish_weekly 側（1冊を作る時点）**で、
       `section_blocked.flag` を見る行をそこに入れる必要がある（モリが担当・crosshealthjp）。
       commit 時刻を公開時刻と読み違えたのが原因＝[[feedback_which_field_did_you_read]]。

    md の1行目には作成日の印（CREATED_MARK）を置く。publish_weekly 側は
    「印が無い＝作成日が読めない」「印の日付が今日でない＝毎朝のジョブが止まっている」
    のどちらでも公開を止める（モリ 2026-10-18）。mtime では照らさない。
    """
    os.makedirs(OUT_DIR, exist_ok=True)
    # 1行目に作成日の印を置く（publish_weekly が今日と照らす・向こうが取り除いてから差し込む）。
    # 返す文字列もファイルと同じ形にする（出口を2つにしない＝片方だけ印が無い状態を作らない）。
    md = CREATED_MARK.format(date.today().isoformat()) + "\n" + build_weekly_section(days)
    h = window_health(days)
    dated = os.path.join(OUT_DIR, f"section_{date.today().strftime('%Y%m%d')}.md")
    latest = os.path.join(OUT_DIR, "section_latest.md")
    for p in (dated, latest):
        # 🔴 先に中身を作ってから置き換える（途中で落ちて0バイトにしない）
        with open(p + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(md); fh.flush(); os.fsync(fh.fileno())
        os.replace(p + ".tmp", p)
    print(f"✓ セクション生成 → {latest}")
    if h["complete"]:
        if os.path.exists(BLOCK_FLAG):
            os.remove(BLOCK_FLAG)
        return md, True
    why = (f"取れなかった日 {len(h['bad'])}日／収集の記録が無い日 {len(h['unknown'])}日"
           f"（読めた日 {len(h['read'])}日・開示 {h['rows']}件）")
    with open(BLOCK_FLAG + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(f"{date.today().isoformat()} {why}\n"); fh.flush(); os.fsync(fh.fileno())
    os.replace(BLOCK_FLAG + ".tmp", BLOCK_FLAG)
    print(f"🔴 人事ウォッチ欄が不完全です＝週次に載せないでください: {why}")
    print(f"   止めの印: {BLOCK_FLAG}")
    notify("人事ウォッチ欄 不完全です（週次を止めてください）", why)
    return md, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--tdnet-backfill", type=int, metavar="N")
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="語の拾い方と「最後のページ」の判断を陽性対照つきで確かめる")
    ap.add_argument("--dry-run", action="store_true", help="読むだけ・events.jsonl と state に書かない")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(0 if self_test() else 1)

    os.makedirs(DATA, exist_ok=True)
    st = load_state()
    seen = {f"{e['date'].replace('-', '')}|{e['code']}|{e['title']}"
            for e in load_events() if e["source"] == "tdnet"}
    rc, bad = 0, []

    def run_days(ds):
        out = []
        for d in ds:
            got, info = tdnet_day(d, seen)
            out += got
            record_day(st, info)
            print(f"  TDnet {d}: {info['pages']}ページ 開示{info['rows']}件 ヒット{len(got)}件"
                  + (f"  {'🔴 ' if not info['ok'] else ''}{info['why']}" if info["why"] else ""))
            if not info["ok"]:
                bad.append(info)
            time.sleep(WAIT)
        return out

    if args.tdnet_backfill:
        evs = run_days([date.today() - timedelta(days=i)
                        for i in range(args.tdnet_backfill, -1, -1)])
        if args.dry_run:
            print(f"（--dry-run）TDnet backfill: {len(evs)}件 ＝ 書いていません")
            for e in evs:
                print(f"    {e['date']} {e['topic']}({'/'.join(e['hit'])}) {e['company']}  {e['title'][:52]}")
        else:
            append_events(evs)
            save_state(st)
            print(f"✓ TDnet backfill: {len(evs)}件")

    if args.daily:
        evs = check_mhlw(st)
        save_state(st)
        evs += run_days([date.today() - timedelta(days=i) for i in (1, 0)])  # 昨日＋当日（dedupあり）
        evs += scan_newsrooms(st)
        save_state(st)
        append_events(evs)
        print(f"✓ daily: 新規イベント{len(evs)}件")
        # 🔴 0件のときは、語の層が生きているかを陽性対照で確かめてから「なし」とする。
        #    [[feedback_fulltext_zero_hits]]／[[feedback_checks_can_silently_no_op]]
        if not [e for e in evs if e["source"] == "tdnet"]:
            if classifier_control():
                print("  （TDnetのヒット0件・語の層は陽性対照を通ったので本当に0件）")
            else:
                print("  🔴 TDnetのヒット0件だが、語の層が陽性対照に落ちた＝0件を信じてはいけない")
                notify("人事ウォッチ 語の層がこわれています", "ヒット0本・陽性対照が落ちました。ログを見てください")
                rc = 1
        if not write_section()[1]:
            rc = 1

    if args.weekly:
        if not write_section()[1]:
            rc = 1

    if bad:
        print(f"🔴 TDnetを読めなかった日が {len(bad)}日: "
              + "／".join(f"{b['date']} {b['why']}" for b in bad))
        notify("人事ウォッチ 取れなかった日があります",
               f"TDnetを読めなかった日 {len(bad)}日。ログを見てください（週次の欄にも不完全と出ます）")
        rc = 1
    h = window_health()
    if not h["complete"]:
        print(f"  ⚠️ 直近8日のうち 読めた日{len(h['read'])}日／読めなかった日{len(h['bad'])}日"
              f"／記録の無い日{len(h['unknown'])}日 ＝ 週次の欄は「不完全」と書きます")
    sys.exit(rc)


# ---------- 自己検査（陽性対照つき） ----------

# 本物の開示タイトル。語の層の陽性対照はここから取る（作り話では対照にならない）。
# 出どころ: TDnet 一覧の実測（2026-09-18〜10-17）。
CONTROL_MA = "米国IntraBio社の完全子会社化に関するお知らせ"          # 4507 塩野義製薬 10/05 15:00 page2
CONTROL_NOT_MA = "臨時株主総会招集のための基準日設定に関するお知らせ"  # 7459 メディパルHD 10/13 15:30 page2


def _single_digit_weekend_days(back=39):
    """保管期間の中から、月か日が一桁の土日を返す（0埋めの対照に使う）。
    毎月1〜9日は必ず直近39日のどこかにあり、その9日のうちに土日が必ず入る。"""
    out = []
    for i in range(1, back + 1):
        d = date.today() - timedelta(days=i)
        if d.weekday() >= 5 and (d.month < 10 or d.day < 10):
            out.append(d)
    return out[:2]


def _recent_multipage_day(back=14):
    """直近の平日から遡って、page1 が満杯（＝2ページ目がある）日を1つ返す。
    🟡 自己検査に日付を直書きすると、保管期間（約40日）を過ぎた日で中身のないまま通る。"""
    d = date.today() - timedelta(days=1)
    for _ in range(back):
        if d.weekday() < 5:
            try:
                html = get(TDNET_LIST.format(page=1, ymd=d.strftime("%Y%m%d")))
                n = sum(1 for tr in BeautifulSoup(html, "html.parser").find_all("tr")
                        if len(tr.find_all("td")) >= 4
                        and re.match(r"^[0-9A-Z]{5}$", tr.find_all("td")[1].get_text(strip=True)))
                if n >= 100:
                    return d
            except Exception:
                pass
        d -= timedelta(days=1)
    return None


def _page_cap_control(d):
    """ページ上限に当たったとき、黙って打ち切らず ok=False で返すかを本物で確かめる。
    返り値 = (ok, 理由に「ページが」が入っているか)"""
    import unittest.mock as mock
    with mock.patch(__name__ + ".MAX_PAGES", 1):
        _, info = tdnet_day(d, set())   # 2ページ以上ある日を上限1ページで読む
    return info["ok"], "ページが" in info["why"]


def classifier_control() -> bool:
    """語の層が生きているかを本物の開示1本で確かめる（0件を信じる前に必ず通す）。"""
    topic, _ = classify(CONTROL_MA)
    return topic == "M&A"


def self_test() -> bool:
    ok = [0, 0]

    def eq(label, got, want):
        good = got == want
        ok[0 if good else 1] += 1
        print(("  ✓ " if good else "  ✗ ") + label + ("" if good else f"  得た={got!r} 欲しい={want!r}"))

    print("① 語の拾い方＝核になる名詞で引けているか（左が実物のタイトル）")
    for t, want in [
        # 本物（陽性対照）
        (CONTROL_MA, "M&A"),
        ("株式会社ＰＡＬＴＡＣ株式（証券コード：8283）に対する公開買付けの結果に関するお知らせ", "M&A"),
        ("代表取締役および取締役の異動に関するお知らせ", "人事"),
        # 言い回しが変わっても拾えるか（2026-10-17 まで落ちていた形）
        ("三菱ケミカル物流株式会社株式の取得に関するお知らせ", "M&A"),
        ("当社連結子会社によるユニオン情報企画株式会社の株式の取得に関するお知らせ", "M&A"),
        ("特定子会社の異動並びに非連結決算への移行に関するお知らせ", "M&A"),
        ("連結子会社株式の一部譲渡に関するお知らせ", "M&A"),
        ("リードプラス株式会社との業務提携に関するお知らせ", "M&A"),
        ("事業の譲受に関するお知らせ", "M&A"),
        ("株式移転による持株会社設立に関するお知らせ", "M&A"),
        ("吸収分割契約の締結に関するお知らせ", "M&A"),
        # (語, 文脈) の組で外すもの＝名詞は入っているが M&A ではない
        ("自己株式の取得状況に関するお知らせ", None),
        ("譲渡制限付株式報酬としての自己株式の処分の払込完了に関するお知らせ", None),
        ("ストックオプション（新株予約権）の発行に関するお知らせ", None),
        ("連結子会社による資産（レジデンス7物件）の譲渡のお知らせ", None),
        # 🔴 伏せる範囲が広すぎて本物の株式譲渡をまたいでいた（ミハル監査 10/17 の見本）
        ("不動産子会社の株式譲渡に関するお知らせ", "M&A"),
        ("土地及び建物の譲渡に関するお知らせ", None),
        ("自己株式の取得及び自己株式の公開買付けの予定に関するお知らせ", None),
        ("三沢細谷太陽光事業における匿名組合出資持分の譲渡実行に関するお知らせ", None),
        ("「Bondlido」の米国販売提携並びに上市時期について", None),
        # 伏せ字方式の陽性対照＝人事の語が同じタイトルにあっても本物のM&Aを捨てない
        ("持株会社体制への移行に伴う代表取締役の異動（退任）及びその他役員の人事に関するお知らせ", "M&A"),
        ("当社および連結子会社の執行役員の異動に関するお知らせ", "人事"),
        # 役職の語は入っているが人事ではない（裁判）。役職だけで引いていたら人事に化ける
        ("当社及び当社グループ会社元取締役に対する最高裁判所の判決について", None),
        ("代表取締役社長の交代に関するお知らせ", "人事"),
        ("組織変更および人事異動に関するお知らせ", "人事"),
        # ミハルの指摘（10/13 7459）＝M&Aではない。M&Aに混ぜない
        (CONTROL_NOT_MA, None),
        # 無関係
        ("2027年５月期　第１四半期決算短信〔日本基準〕（連結）", None),
        ("株主優待の内容に関するお知らせ", None),
    ]:
        eq(f"{t[:34]}… → {want}", classify(t)[0], want)

    print("② 直す前の書き方なら落ちることを見せる（陽性対照）")
    old_kw = ("公開買付", "TOB", "MBO", "買収", "経営統合", "合併", "子会社化", "株式取得",
              "株式譲渡", "事業譲渡", "資本業務提携", "資本提携", "持株会社体制", "統合契約",
              "完全子会社", "株式交換", "会社分割", "上場廃止")
    for t in ("三菱ケミカル物流株式会社株式の取得に関するお知らせ",
              "リードプラス株式会社との業務提携に関するお知らせ",
              "特定子会社の異動並びに非連結決算への移行に関するお知らせ"):
        eq(f"旧「言い回し」では落ちる: {t[:24]}…", any(k in t for k in old_kw), False)
        eq(f"いまの核の名詞では拾える: {t[:24]}…", classify(t)[0], "M&A")

    print("③ 「最後のページ」の判断（本物の TDnet を読んで確かめる）")
    # 🟡 日付を固定で書くと、TDnet の保管期間（約40日）を過ぎた日は404になり、
    #    「開示なし」の経路を通らないまま (0, True) で**中身のないまま通る**（ミハル監査 10/17）。
    #    → 日付は今日から数える。why の中身まで照らして、どの経路を通ったかを確かめる。
    try:
        busy = _recent_multipage_day()
        eq("直近でページが2枚以上ある平日が見つかる", busy is not None, True)
        html = get(TDNET_LIST.format(page=1, ymd=busy.strftime("%Y%m%d")))
        soup = BeautifulSoup(html, "html.parser")
        codes = [tr.find_all("td")[1].get_text(strip=True)
                 for tr in soup.find_all("tr") if len(tr.find_all("td")) >= 4]
        all5 = [c for c in codes if re.match(r"^[0-9A-Z]{5}$", c)]
        num5 = [c for c in all5 if re.match(r"^\d{5}$", c)]
        eq(f"{busy} の page1 は開示100件ある", len(all5), 100)
        eq("うち数字5桁だけ数えると100未満＝旧ルールはここで打ち切っていた", len(num5) < 100, True)
        _, info = tdnet_day(busy, set())
        eq("いまは2ページ目以降も読む", info["pages"] >= 2, True)
        eq("開示は100件を超える", info["rows"] > 100, True)
        eq("その日は読めた扱い・理由は空", (info["ok"], info["why"]), (True, ""))
        eq("ページ上限に当たったら ok=False（黙って打ち切らない）",
           _page_cap_control(busy), (False, True))

        print("④ 0件の裏取り（陽性対照・どの経路を通ったかも照らす）")
        sat = date.today() - timedelta(days=(date.today().weekday() - 5) % 7)  # 直近の土曜
        _, e1 = tdnet_day(sat, set())
        eq(f"土曜（{sat}）は0件で読めた扱い", (e1["rows"], e1["ok"]), (0, True))
        # 🔑 「開示された情報はありません」は**失敗の理由にも入る**ので経路を見分けられない
        #    （ミハル監査 10/17）。成功の経路にしか出ない「開示なし（」で照らす。
        eq("しかも『開示なし』の経路を通っている（404の経路でも失敗の経路でもない）",
           (e1["why"].startswith("開示なし（"), "404" in e1["why"]), (True, False))
        eq("一覧にはその日付が0埋めで書かれている",
           sat.strftime("%Y年%m月%d日") in get(TDNET_LIST.format(page=1, ymd=sat.strftime("%Y%m%d"))), True)
        # 🟡 strftime の比較だけでは `f"{d.month}月{d.day}日"` に戻しても緑のままだった
        #    （ミハル監査 10/17）。**保管期間の中にある一桁の月日の土日**を tdnet_day に通す。
        #    毎月1〜9日は必ず直近39日のどこかにあるので、必ず1つは選べる。
        pads = _single_digit_weekend_days()
        eq("一桁の月日の土日が保管期間の中に見つかる", len(pads) >= 1, True)
        for pd in pads:
            _, ep = tdnet_day(pd, set())
            eq(f"{pd}（一桁の月日）も『開示なし』の経路で読めた扱い",
               (ep["rows"], ep["ok"], ep["why"].startswith("開示なし（")), (0, True, True))
            h = get(TDNET_LIST.format(page=1, ymd=pd.strftime("%Y%m%d")))
            eq(f"{pd} の一覧は0埋めで書いてある", pd.strftime("%Y年%m月%d日") in h, True)
            eq(f"{pd} は0埋めしない書き方では一覧に出てこない（戻したら上の対照が落ちる）",
               f"{pd.year}年{pd.month}月{pd.day}日" in h, False)

        fut = date.today() + timedelta(days=90)
        _, e2 = tdnet_day(fut, set())
        eq("まだ先の日付は『一覧なし』で読めた扱い", (e2["rows"], e2["ok"], "先の日付" in e2["why"]), (0, True, True))

        old = date.today() - timedelta(days=120)
        while old.weekday() >= 5:
            old -= timedelta(days=1)
        _, e3 = tdnet_day(old, set())
        eq(f"保管期間を過ぎた平日（{old}）は ok=False（0件にしない）", e3["ok"], False)
        eq("理由が『保管期間』と言う", "保管期間" in e3["why"], True)

        print("⑤ 取れなかった日は黙って0件にしない（陽性対照）")
        import unittest.mock as mock
        with mock.patch(__name__ + ".get", side_effect=urllib.error.URLError("名前が引けません")):
            _, e4 = tdnet_day(busy, set())
        eq("DNSが引けない日は ok=False", (e4["ok"], "URLError" in e4["why"]), (False, True))
        with mock.patch(__name__ + ".get", side_effect=urllib.error.HTTPError("u", 403, "Forbidden", {}, None)):
            _, e5 = tdnet_day(busy, set())
        eq("403 は ok=False（404 と混ぜない）", (e5["ok"], "403" in e5["why"]), (False, True))
        # 2ページ目以降の「200で0件」＝最後のページの次は404のはずなので、終わりと決めない
        real = get
        def one_then_empty(url, **kw):
            return real(url) if "_001_" in url else real(
                TDNET_LIST.format(page=1, ymd=sat.strftime("%Y%m%d")))
        with mock.patch(__name__ + ".get", side_effect=one_then_empty):
            _, e6 = tdnet_day(busy, set())
        eq("2ページ目が200で0件なら ok=False", (e6["ok"], "200で0件" in e6["why"]), (False, True))
    except Exception as e:
        print(f"  ✗ ③〜⑤ は TDnet に届かないので確かめられません: {type(e).__name__} {str(e)[:70]}")
        ok[1] += 1

    print("⑤c 通知の文字コード（**符号化できるところまで**。実際に届くかは手で1回確かめた）")
    try:
        "人事ウォッチ 取れなかった日があります".encode("utf-8").decode("latin-1").encode("latin-1")
        eq("日本語の見出しをヘッダに載せられる", True, True)
    except Exception as e:
        eq("日本語の見出しをヘッダに載せられる", f"{type(e).__name__}", True)

    print("⑥ 週次の欄＝読めていない日があれば『ありませんでした』と書かない（本物の関数を通す）")
    import unittest.mock as mock
    win = [(date.today() - timedelta(days=i)).isoformat() for i in range(7, -1, -1)]
    allok = {d: {"pages": 1, "rows": 100, "hits": 0, "ok": True, "why": ""} for d in win}
    with mock.patch(__name__ + ".load_state", return_value={"tdnet_days": allok}), \
         mock.patch(__name__ + ".load_events", return_value=[]):
        md_ok = build_weekly_section()
    # 🔴 ただの「ありませんでした」で数えない＝厚労省幹部名簿の行（別のソース）も当たってしまう。
    #    TDnet 由来の2文を名指しで数える（[[feedback_grep_by_core_word]] 語だけで持たない）。
    NONE_MA = "M&A・再編関連の開示・発表はありませんでした"
    NONE_JINJI = "人事関連開示はありませんでした"
    eq("全部読めた週は「ありませんでした」と書く", (NONE_MA in md_ok, NONE_JINJI in md_ok), (True, True))
    eq("そのとき「不完全」とは書かない", "不完全" in md_ok, False)

    one_bad = dict(allok)
    one_bad[win[3]] = {"pages": 2, "rows": 150, "hits": 0, "ok": False, "why": "page3 が HTTP 403"}
    with mock.patch(__name__ + ".load_state", return_value={"tdnet_days": one_bad}), \
         mock.patch(__name__ + ".load_events", return_value=[]):
        md_bad = build_weekly_section()
    eq("1日でも取れていなければ「ありませんでした」と書かない",
       (NONE_MA in md_bad, NONE_JINJI in md_bad), (False, False))
    eq("代わりに「不完全です」と書く", "不完全です" in md_bad, True)
    eq("取れなかった日の日付が出る", win[3][5:] in md_bad, True)
    eq("人事欄とM&A欄の両方に出る（出口が2つなら両方）", md_bad.count("不完全です"), 2)

    missing = {d: v for d, v in allok.items() if d != win[5]}
    with mock.patch(__name__ + ".load_state", return_value={"tdnet_days": missing}), \
         mock.patch(__name__ + ".load_events", return_value=[]):
        md_miss = build_weekly_section()
    eq("収集の記録が無い日も「不完全」にする",
       ("不完全です" in md_miss, NONE_MA in md_miss, NONE_JINJI in md_miss), (True, False, False))

    print("⑦ 不完全な週は週次を止める（本物の write_section を通す・書き先は仮の場所）")
    import tempfile
    sent = []
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch(__name__ + ".OUT_DIR", tmp), \
             mock.patch(__name__ + ".BLOCK_FLAG", os.path.join(tmp, "section_blocked.flag")), \
             mock.patch(__name__ + ".notify", side_effect=lambda t, b: sent.append((t, b))), \
             mock.patch(__name__ + ".load_events", return_value=[]):
            with mock.patch(__name__ + ".load_state", return_value={"tdnet_days": one_bad}):
                md_b, okflag_b = write_section()
            eq("不完全な週は ok=False を返す", okflag_b, False)
            eq("止めの印が置かれる", os.path.exists(os.path.join(tmp, "section_blocked.flag")), True)
            eq("知らせが1本出る", len(sent), 1)
            eq("知らせに件数が入る", "取れなかった日 1日" in sent[0][1], True)
            eq("知らせに社名・題名は入らない",
               any(w in sent[0][0] + sent[0][1] for w in ("株式", "お知らせ", "ep", ".json")), False)
            eq("セクション自体は残す（中を見て決められるように）",
               os.path.exists(os.path.join(tmp, "section_latest.md")), True)

            # --- 作成日の印（モリ 2026-10-18）。CREATED_MARK では照らさない（同じ式を2回書くと食い違いが見つからない）。
            #     🔴 向こう（publish_weekly.py）の式と「同じ」ではない（ミハルの指摘 10/18）。向こうは
            #        `\s*<!--\s*jinji-section-created:\s*(…)\s*-->[ \t]*\n?` ＝空白に寛容な緩い形で、
            #        下の式はその**厳しい部分集合**。ここが通れば向こうでも当たるが、逆は言えない。
            #        両側がつながっていることは、本物の欄を向こうの複製に読ませる通しの確かめで見る
            #        （ミハルが 10/18 に実施：「✓ 人事ウォッチ欄を差し込みました（作成日 2026-10-18）」）。
            MARK_RE = r"^<!-- jinji-section-created: (\d{4}-\d{2}-\d{2}) -->$"
            body_b = open(os.path.join(tmp, "section_latest.md"), encoding="utf-8").read()
            m = re.match(MARK_RE, body_b.split("\n")[0])
            eq("1行目が作成日の印になっている", bool(m), True)
            eq("印の日付は今日", m.group(1) if m else None, date.today().isoformat())
            eq("日付入りのファイルにも入る",
               bool(re.match(MARK_RE, open(os.path.join(
                   tmp, "section_" + date.today().strftime("%Y%m%d") + ".md"),
                   encoding="utf-8").read().split("\n")[0])), True)
            eq("印は1本に1回だけ（1行取り除けば残らない）",
               body_b.count("jinji-section-created"), 1)
            stripped = "\n".join(body_b.split("\n")[1:])
            eq("1行取り除くと本文の見出しから始まる",
               (stripped.startswith("## 今週の人事ウォッチ"), "jinji-section-created" in stripped),
               (True, False))
            eq("返す文字列はファイルと同じ形（片方だけ印が無い状態を作らない）", md_b, body_b)
            # 一度落としてみる：印を付けない形（前の版）だと向こうの正規表現は当たらない＝公開は止まる
            with mock.patch(__name__ + ".load_state", return_value={"tdnet_days": one_bad}):
                eq("印が無ければ「作成日が読めない」になる（前の版で落ちることを見る）",
                   bool(re.match(MARK_RE, build_weekly_section().split("\n")[0])), False)
            eq("印に社名・人名・会員メールは入らない（ntfy と同じ線）",
               any(w in body_b.split("\n")[0] for w in ("株式", "@", "/Users/", "ep")), False)
            with mock.patch(__name__ + ".load_state", return_value={"tdnet_days": allok}):
                md_g, okflag_g = write_section()
            eq("全部読めた週は ok=True を返す", okflag_g, True)
            # 止める週（body_b）だけでなく、**全部読めた週の欄にも**印が入ることを照らす。
            # 片方しか見ていないと、印を h["complete"] の分岐の中に移しても緑のままになる（ミハル🟡2 10/18）。
            body_g = open(os.path.join(tmp, "section_latest.md"), encoding="utf-8").read()
            mg = re.match(MARK_RE, body_g.split("\n")[0])
            eq("全部読めた週の欄も1行目が作成日の印", bool(mg), True)
            eq("その日付も今日", mg.group(1) if mg else None, date.today().isoformat())
            eq("全部読めた週も1行外すと本文の見出しから始まる",
               "\n".join(body_g.split("\n")[1:]).startswith("## 今週の人事ウォッチ"), True)
            eq("どちらの週でも印は1回だけ", body_g.count("jinji-section-created"), 1)
            eq("そのとき止めの印は消える", os.path.exists(os.path.join(tmp, "section_blocked.flag")), False)
            eq("知らせは増えない", len(sent), 1)

    print(f"\n{'🟢 全部通りました' if ok[1] == 0 else '🔴 落ちた項目があります'}（通 {ok[0]} / 落 {ok[1]}）")
    return ok[1] == 0


if __name__ == "__main__":
    main()
