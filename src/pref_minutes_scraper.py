#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""都道府県の地域医療議事録アーカイブ（2026-07-07夜 着手・47都道府県DBの第一歩）。

各都道府県の「地域医療構想調整会議」「医療審議会」等の議事録/議事概要PDFを収集する。
構造は県ごとにバラバラなため、設定駆動の2段クロール:
  seed（ハブページ）→ 会議系リンクを辿る（1段）→ 開催結果ページ（2段）→ 議事録PDFを取得
収集先: ~/pref_minutes_archive/<pref>/*.json（本文はpdftotextで抽出）

使い方: python3 src/pref_minutes_scraper.py chiba [osaka ...]（無指定=全設定県）
公的資料・1.5秒間隔。再実行時は取得済みPDFをスキップ（差分だけ取る）。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from bs4 import BeautifulSoup

OUT_ROOT = os.path.expanduser("~/pref_minutes_archive")
UA = {"User-Agent": "Mozilla/5.0 (CrossHealth research; contact: jump.deep.tree.inside@gmail.com)"}
WAIT = 1.5
MAX_PAGES = 400          # 県ごとの最大ページ走査数（暴走防止）
LINK_KW = ("調整会議", "協議会", "連携会議", "医療審議会", "構想", "懇話会", "開催",
           "議事", "会議録", "部会")
MINUTES_KW = ("議事録", "議事概要", "会議録", "議事要旨", "議事メモ", "結果概要", "審議概要", "議事(", "議事（")

PREFS = {
    "chiba": {
        "name": "千葉県",
        "seeds": [
            # 地域保健医療連携・地域医療構想調整会議ハブ（9医療圏の入口）
            "https://www.pref.chiba.lg.jp/kenfuku/keikaku/kenkoufukushi/chiikiiryoukousou.html",
            # 千葉県医療審議会（総会＋部会は審議会indexから辿る）
            "https://www.pref.chiba.lg.jp/kenfuku/shingikai/iryou/index.html",
        ],
    },
    "osaka": {
        "name": "大阪府",
        "seeds": [
            # 大阪府保健医療協議会（＝法定の地域医療構想調整会議）
            "https://www.pref.osaka.lg.jp/o100020/iryo/keikaku/hokeniryoukyougikai.html",
            # 大阪府医療審議会
            "https://www.pref.osaka.lg.jp/ijikango/iryoushinngikai/",
        ],
    },
    "kyoto": {
        "name": "京都府",
        "seeds": [
            # 京都市域 調整会議（審議概要はHTMLページ）
            "https://www.pref.kyoto.jp/iryo/block4/index.html",
            # 京都府医療審議会
            "https://www.pref.kyoto.jp/shingikai/iryo-03/",
            # 圏域（山城南・山城北。他圏域は振興局ページが散在＝順次追加）
            "https://www.pref.kyoto.jp/yamashiro/ho-minami/chiikiiryo.html",
            "https://www.pref.kyoto.jp/y-ho-kita/iryokyougikai-kousoucyousei.html",
        ],
    },
    "hyogo": {
        "name": "兵庫県",
        "seeds": [
            "https://web.pref.hyogo.lg.jp/kf15/chouseikaigi.html",
            "https://web.pref.hyogo.lg.jp/kf15/iryoukousou.html",
            # 医療審議会 保健医療計画部会
            "https://web.pref.hyogo.lg.jp/kf15/keikaku27-.html",
            "https://web.pref.hyogo.lg.jp/kf15/r3keikakubukai.html",
        ],
    },
    "nara": {
        "name": "奈良県",
        "seeds": [
            # 奈良県地域医療構想（5構想区域の調整会議へのハブ）
            "https://www.pref.nara.lg.jp/n081/41029.html",
            # 医療審議会（旧ドメイン側）
            "https://www.pref.nara.jp/9293.htm",
        ],
    },
    "shiga": {
        "name": "滋賀県",
        "seeds": [
            # 医療トップ（各圏域の調整会議ページへのハブ）
            "https://www.pref.shiga.lg.jp/ippan/kenkouiryouhukushi/iryo/",
        ],
    },
    "wakayama": {
        "name": "和歌山県",
        "seeds": [
            # 地域医療構想調整会議（協議の場）開催状況
            "https://www.pref.wakayama.lg.jp/prefg/050100/imuka/kyouginoba.html",
            "https://www.pref.wakayama.lg.jp/prefg/050100/imuka/chikiiryokoso.html",
        ],
    },
}

WAREKI = {"令和": 2018, "平成": 1988}


def get(url: str, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    if binary:
        return raw
    for enc in ("utf-8", "shift_jis", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def norm(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, href.split("#")[0])


def parse_meta(blob: str) -> tuple:
    """『令和6年度第2回』等から (年度, 回) を推定"""
    z = str.maketrans("０１２３４５６７８９", "0123456789")
    blob = blob.translate(z)
    year = None
    m = re.search(r"(令和|平成)(\d+|元)年度?", blob)
    if m:
        y = 1 if m.group(2) == "元" else int(m.group(2))
        year = WAREKI[m.group(1)] + y
    kai = None
    m = re.search(r"第(\d+)回", blob)
    if m:
        kai = int(m.group(1))
    return year, kai


def pdf_to_text(raw: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(raw)
        pdf = f.name
    txt = pdf + ".txt"
    try:
        subprocess.run(["pdftotext", "-layout", pdf, txt], check=True,
                       capture_output=True, timeout=120)
        return open(txt, encoding="utf-8", errors="ignore").read()
    finally:
        for p in (pdf, txt):
            if os.path.exists(p):
                os.unlink(p)


def scrape_pref(key: str, conf: dict):
    out = os.path.join(OUT_ROOT, key)
    os.makedirs(out, exist_ok=True)
    have = {json.load(open(os.path.join(out, f), encoding="utf-8"))["url"]
            for f in os.listdir(out) if f.endswith(".json")}
    domains = {urllib.parse.urlparse(u).netloc for u in conf["seeds"]}
    print(f"== {conf['name']} ({key})  既存{len(have)}件")

    visited, queue = set(), [(u, 0) for u in conf["seeds"]]
    pdf_targets = {}   # url -> (link_text, page_title)
    html_minutes = 0   # 議事概要がHTML本文の県（京都など）はページ自体を保存
    pages = 0
    while queue and pages < MAX_PAGES:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            html = get(url)
        except Exception as e:
            print(f"  ✗ page {url[-50:]}: {type(e).__name__}")
            continue
        pages += 1
        soup = BeautifulSoup(html, "html.parser")
        ptitle = soup.title.get_text(strip=True).split("／")[0].split("|")[0][:80] if soup.title else ""
        if depth > 0 and url not in have and any(k in ptitle for k in MINUTES_KW):
            # ページ本文＝議事概要そのもの（PDFでなくHTML掲載の県）
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
            if len(text) > 800:
                year, kai = parse_meta(ptitle + " " + text[:500])
                slug = re.sub(r"[^\w]", "_", "_".join(url.split("/")[-2:]))[:120].replace(".html", "")
                rec = {"pref": conf["name"], "council": ptitle, "label": ptitle,
                       "year": year, "kai": kai, "url": url, "chars": len(text), "text": text}
                json.dump(rec, open(os.path.join(out, f"{slug}.json"), "w", encoding="utf-8"),
                          ensure_ascii=False)
                have.add(url)
                html_minutes += 1
        for a in soup.find_all("a", href=True):
            t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            u = norm(url, a["href"])
            if urllib.parse.urlparse(u).netloc not in domains:
                continue
            low = u.lower()
            if low.endswith(".pdf"):
                # 議事録系PDFだけ拾う（資料PDFは対象外＝まず発言・議論の本文を資産化）
                if any(k in t for k in MINUTES_KW):
                    pdf_targets.setdefault(u, (t[:100], ptitle))
            elif depth < 2 and (low.endswith((".html", ".htm")) or low.endswith("/")):
                if any(k in t for k in LINK_KW):
                    if u not in visited:
                        queue.append((u, depth + 1))
        time.sleep(WAIT)
    print(f"  走査{pages}ページ → PDF候補{len(pdf_targets)}件・HTML議事{html_minutes}件")

    done = skip = fail = 0
    for u, (ltext, ptitle) in pdf_targets.items():
        if u in have:
            skip += 1
            continue
        slug = re.sub(r"[^\w]", "_", "_".join(u.split("/")[-2:]))[:120].replace(".pdf", "")
        path = os.path.join(out, f"{slug}.json")
        if os.path.exists(path):
            skip += 1
            continue
        try:
            raw = get(u, binary=True)
            text = pdf_to_text(raw)
            year, kai = parse_meta(ptitle + " " + ltext + " " + text[:500])
            rec = {"pref": conf["name"], "council": ptitle, "label": ltext,
                   "year": year, "kai": kai, "url": u, "chars": len(text), "text": text}
            json.dump(rec, open(path, "w", encoding="utf-8"), ensure_ascii=False)
            done += 1
            if done % 20 == 0:
                print(f"  進捗: 取得{done} / スキップ{skip} / 失敗{fail}")
        except Exception as e:
            fail += 1
            print(f"  ✗ pdf {u[-50:]}: {type(e).__name__} {str(e)[:40]}")
        time.sleep(WAIT)
    total_chars = sum(json.load(open(os.path.join(out, f), encoding="utf-8"))["chars"]
                      for f in os.listdir(out) if f.endswith(".json"))
    print(f"  完了: 取得{done} / スキップ{skip} / 失敗{fail} → 計{len(os.listdir(out))}件・{total_chars//10000}万字")


def main():
    keys = [k for k in sys.argv[1:] if k in PREFS] or list(PREFS)
    for k in keys:
        scrape_pref(k, PREFS[k])


if __name__ == "__main__":
    main()
