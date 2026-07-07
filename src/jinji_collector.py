#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人事ウォッチ収集（2026-07-07 ロードマップ2・ウィークリー自動欄の原材料）。

2つの公開ソースから人事イベントを集め、ウィークリー用のMarkdownセクションを自動生成する:
  1. 厚労省幹部名簿PDF（更新検知→前回名簿との差分＝誰がどの官職に就いたか）
  2. TDnet適時開示（上場ドラッグ/調剤/医薬品卸のウォッチリスト×人事系キーワード）

使い方:
  python3 src/jinji_collector.py --daily        # 毎日実行（launchd）: 名簿チェック+TDnet収集+セクション再生成
  python3 src/jinji_collector.py --tdnet-backfill 7   # TDnetを過去N日ぶん遡って収集
  python3 src/jinji_collector.py --weekly       # セクションだけ再生成（直近7日）

データ: ~/jinji_data/（events.jsonl・mhlw_rosters/・state.json）
出力:   output/jinji/section_latest.md（publish_weekly.pyが自動で差し込む）
"""
import argparse
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

DATA = os.path.expanduser("~/jinji_data")
ROSTERS = os.path.join(DATA, "mhlw_rosters")
EVENTS = os.path.join(DATA, "events.jsonl")
STATE = os.path.join(DATA, "state.json")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output", "jinji")
UA = {"User-Agent": "Mozilla/5.0 (CrossHealth research; contact: jump.deep.tree.inside@gmail.com)"}
WAIT = 1.5

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
JINJI_KW = ("人事", "役員", "代表取締役", "社長", "機構改革", "組織変更", "組織再編", "異動", "就任", "退任", "CEO")
# M&A・再編ウォッチ（2026-07-08未明 木内さん発案）。承継事業のレーダーを兼ねる。
MA_KW = ("公開買付", "TOB", "MBO", "買収", "経営統合", "合併", "子会社化", "株式取得",
         "株式譲渡", "事業譲渡", "資本業務提携", "資本提携", "持株会社体制", "統合契約",
         "完全子会社", "株式交換", "会社分割", "上場廃止")

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


def get(url: str, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read()
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

def tdnet_day(d: date, seen: set) -> list:
    ymd = d.strftime("%Y%m%d")
    evs, page = [], 1
    while True:
        try:
            html = get(TDNET_LIST.format(page=page, ymd=ymd))
        except Exception:
            break  # その日のページなし（土日祝 or 範囲外）
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.find_all("tr")
        got = 0
        for tr in rows:
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            tm, code, company, title = (td.get_text(strip=True) for td in tds[:4])
            if not re.match(r"^\d{5}$", code):
                continue
            got += 1
            code4 = code[:4]
            if code4 not in WATCHLIST:
                continue
            ma = any(k in title for k in MA_KW)
            jinji = any(k in title for k in JINJI_KW)
            if not (ma or jinji):
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
                        "topic": "M&A" if ma else "人事",
                        "collected": datetime.now().isoformat(timespec="seconds")})
        # 次ページ有無（「次へ」リンクの活性）で判断できないため、100件未満なら終了
        if got < 100:
            break
        page += 1
        time.sleep(WAIT)
    return evs


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
                ma = any(k in t for k in MA_KW)
                jinji = any(k in t for k in JINJI_KW)
                if ma or jinji:
                    hit += 1
                    evs.append({"source": "newsroom", "date": date.today().isoformat(),
                                "company": name, "category": cat, "title": t, "url": u,
                                "topic": "M&A" if ma else "人事",
                                "collected": datetime.now().isoformat(timespec="seconds")})
        seen.update(items)
        seen_map[key] = sorted(seen)[-800:]  # 肥大化防止
        print(f"  newsroom {name}: リンク{len(items)}件 新着{len(new)}件"
              + ("（初回＝ベースライン）" if first else f" ヒット{hit}件"))
        time.sleep(WAIT)
    return evs


# ---------- ウィークリー用セクション ----------

def build_weekly_section(days=7) -> str:
    since = (date.today() - timedelta(days=days)).isoformat()
    evs = [e for e in load_events() if e["date"] >= since]
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
    else:
        lines.append("今週、ウォッチ対象企業の人事関連開示はありませんでした。")
    # M&A・再編ウォッチ（承継・業界再編のレーダー）
    lines += ["", "## 今週のM&A・再編ウォッチ", ""]
    if ma:
        for e in ma:
            md = f"{int(e['date'][5:7])}/{int(e['date'][8:10])}"
            t = f"[{e['title']}]({e['url']})" if e.get("url") else e["title"]
            lines.append(f"- {md}　**{e['company']}**（{e.get('category','')}）　{t}")
    else:
        lines.append("今週、ウォッチ対象企業（調剤・ドラッグストア・医薬品卸・製薬・CRO 52社）のM&A・再編関連の開示・発表はありませんでした。")
    lines += ["", "（出典：厚生労働省 幹部名簿／TDnet適時開示／各社ニュースルーム。毎日自動収集）", ""]
    return "\n".join(lines)


def write_section(days=7):
    os.makedirs(OUT_DIR, exist_ok=True)
    md = build_weekly_section(days)
    dated = os.path.join(OUT_DIR, f"section_{date.today().strftime('%Y%m%d')}.md")
    latest = os.path.join(OUT_DIR, "section_latest.md")
    open(dated, "w", encoding="utf-8").write(md)
    open(latest, "w", encoding="utf-8").write(md)
    print(f"✓ セクション生成 → {latest}")
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--tdnet-backfill", type=int, metavar="N")
    ap.add_argument("--weekly", action="store_true")
    args = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    st = load_state()
    seen = {f"{e['date'].replace('-', '')}|{e['code']}|{e['title']}"
            for e in load_events() if e["source"] == "tdnet"}

    if args.tdnet_backfill:
        evs = []
        for i in range(args.tdnet_backfill, -1, -1):
            d = date.today() - timedelta(days=i)
            got = tdnet_day(d, seen)
            evs += got
            print(f"  TDnet {d}: ヒット{len(got)}件")
            time.sleep(WAIT)
        append_events(evs)
        print(f"✓ TDnet backfill: {len(evs)}件")

    if args.daily:
        evs = check_mhlw(st)
        save_state(st)
        for i in (1, 0):  # 昨日ぶん＋当日ぶん（dedupあり）
            d = date.today() - timedelta(days=i)
            evs += tdnet_day(d, seen)
            time.sleep(WAIT)
        evs += scan_newsrooms(st)
        save_state(st)
        append_events(evs)
        print(f"✓ daily: 新規イベント{len(evs)}件")
        write_section()

    if args.weekly:
        write_section()


if __name__ == "__main__":
    main()
