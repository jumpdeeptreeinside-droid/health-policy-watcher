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

# 上場ドラッグ・調剤・医薬品卸のウォッチリスト（証券コード4桁→表示名）。足すだけで監視対象が増える。
WATCHLIST = {
    # 調剤
    "9627": "アインHD", "3341": "日本調剤", "3034": "クオールHD",
    "4350": "メディカルシステムネットワーク", "2796": "ファーマライズHD",
    # ドラッグストア
    "3141": "ウエルシアHD", "3391": "ツルハHD", "3088": "マツキヨココカラ&カンパニー",
    "3349": "コスモス薬品", "9989": "サンドラッグ", "7649": "スギHD",
    "3148": "クリエイトSDHD", "2664": "カワチ薬品", "7679": "薬王堂HD",
    "9267": "Genky DrugStores", "3549": "クスリのアオキHD", "3544": "サツドラHD",
    # 医薬品卸
    "7459": "メディパルHD", "2784": "アルフレッサHD", "9987": "スズケン",
    "8129": "東邦HD", "3151": "バイタルケーエスケーHD",
}
JINJI_KW = ("人事", "役員", "代表取締役", "社長", "機構改革", "組織変更", "組織再編", "異動")

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
            if not any(k in title for k in JINJI_KW):
                continue
            a = tds[3].find("a", href=True)
            url = (TDNET_BASE + a["href"]) if a else ""
            key = f"{ymd}|{code4}|{title}"
            if key in seen:
                continue
            seen.add(key)
            evs.append({"source": "tdnet", "date": d.isoformat(), "time": tm,
                        "code": code4, "company": WATCHLIST[code4], "title": title, "url": url,
                        "collected": datetime.now().isoformat(timespec="seconds")})
        # 次ページ有無（「次へ」リンクの活性）で判断できないため、100件未満なら終了
        if got < 100:
            break
        page += 1
        time.sleep(WAIT)
    return evs


# ---------- ウィークリー用セクション ----------

def build_weekly_section(days=7) -> str:
    since = (date.today() - timedelta(days=days)).isoformat()
    evs = [e for e in load_events() if e["date"] >= since]
    mhlw = [e for e in evs if e["source"] == "mhlw"]
    tdnet = sorted([e for e in evs if e["source"] == "tdnet"], key=lambda e: (e["date"], e["time"]))
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
    lines.append("### 企業（上場ドラッグ・調剤薬局・医薬品卸）")
    if tdnet:
        for e in tdnet:
            md = f"{int(e['date'][5:7])}/{int(e['date'][8:10])}"
            t = f"[{e['title']}]({e['url']})" if e["url"] else e["title"]
            lines.append(f"- {md}　**{e['company']}**　{t}")
    else:
        lines.append("今週、ウォッチ対象企業の人事関連開示はありませんでした。")
    lines += ["", "（出典：厚生労働省 幹部名簿／TDnet適時開示。毎日自動収集）", ""]
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
        append_events(evs)
        print(f"✓ daily: 新規イベント{len(evs)}件")
        write_section()

    if args.weekly:
        write_section()


if __name__ == "__main__":
    main()
