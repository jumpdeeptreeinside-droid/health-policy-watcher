#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""国の審議会・検討会 議事録アーカイブ取得（2026-07-07 ロードマップ1・中医協クローラーの横展開）。

社保審（医療部会・医療保険部会）＋医療計画・地域医療構想系の検討会/WGの議事録を
~/shingikai_archive/<council_key>/ にJSONで保存する。Phase2（構造化DB）の原材料。
公的資料・1.5秒間隔の丁寧なクロール。再実行時は取得済みをスキップ（差分だけ取る）。

使い方: python3 src/shingikai_archive_scraper.py [council_key ...]（無指定=全審議会）
"""
import json
import os
import re
import sys
import time
import urllib.request

from bs4 import BeautifulSoup

BASE = "https://www.mhlw.go.jp"
OUT_ROOT = os.path.expanduser("~/shingikai_archive")
UA = {"User-Agent": "Mozilla/5.0 (CrossHealth research; contact: jump.deep.tree.inside@gmail.com)"}
WAIT = 1.5

# 各審議会のインデックスページ（現行＋過去アーカイブ）。2026-07-07に全URL実地検証済み。
COUNCILS = {
    "iryo_bukai": {
        "name": "社会保障審議会 医療部会",
        "indexes": [
            "/stf/shingi/shingi-hosho_126719.html",        # 現行（第84回〜）
            "/stf/shingi/shingi-hosho_126719_00005.html",  # 第12回〜第83回（2010〜）
        ],
    },
    "hoken_bukai": {
        "name": "社会保障審議会 医療保険部会",
        "indexes": [
            "/stf/newpage_28708.html",                       # 現行（第186回〜）
            "/stf/shingi/shingi-hosho_126706_old2_00002.html",  # 第164〜185回
            "/stf/shingi/shingi-hosho_126706_old2.html",     # 第118〜163回
            "/stf/shingi/shingi-hosho_126706old.html",       # 〜第117回
        ],
    },
    "shin_chiiki_kento": {
        "name": "新たな地域医療構想等に関する検討会",
        "indexes": ["/stf/shingi/other-isei_436723_00010.html"],  # 第1〜15回（2024）
    },
    "chiiki_keikaku_kento": {
        "name": "地域医療構想及び医療計画等に関する検討会",
        "indexes": ["/stf/shingi/other-isei_436723_00015.html"],  # 第1回〜（2025〜・開催中）
    },
    "chiiki_wg": {
        "name": "地域医療構想に関するワーキンググループ",
        "indexes": ["/stf/shingi/other-isei_368422.html"],  # 第1〜31回（2016〜2021）
    },
    "ishi_kakuho_wg": {
        "name": "地域医療構想及び医師確保計画に関するワーキンググループ",
        "indexes": ["/stf/shingi/other-isei_436723_00004.html"],  # 第1〜15回（2021〜2024）
    },
    "dai8ji_keikaku": {
        "name": "第８次医療計画等に関する検討会",
        "indexes": ["/stf/shingi/other-isei_127276_00005.html"],  # 第1〜23回（2021〜2023）
    },
    "iryo_keikaku_minaoshi": {
        "name": "医療計画の見直し等に関する検討会",
        "indexes": ["/stf/shingi/other-isei_127276.html"],  # 2005〜2020（第7次・見直し）
    },
}


def get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read()
    for enc in ("utf-8", "shift_jis", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def collect_minute_links(indexes: list) -> list:
    links = []
    for idx in indexes:
        try:
            html = get(BASE + idx if idx.startswith("/") else idx)
        except Exception as e:
            print(f"✗ index取得失敗 {idx}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            # 部会は「議事録」、古い検討会は「議事要旨」しか無い回がある
            if a.get_text(strip=True) not in ("議事録", "議事要旨"):
                continue
            href = a["href"]
            url = href if href.startswith("http") else BASE + href
            row = a.find_parent("tr")
            ctx = row.get_text(" ", strip=True)[:120] if row else ""
            links.append({"url": url, "ctx": ctx})
        print(f"  {idx}: 累計{len(links)}件")
        time.sleep(WAIT)
    seen, uniq = set(), []
    for l in links:
        if l["url"] in seen:
            continue
        seen.add(l["url"])
        uniq.append(l)
    return uniq


def extract_text(html: str) -> tuple:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return title, text


def parse_meta(title: str, ctx: str) -> tuple:
    """回数と開催日をタイトル・行テキストから拾う（漢数字回にも対応：第十六回）"""
    blob = title + " " + ctx
    m = re.search(r"第(\d+)回", blob)
    kai = int(m.group(1)) if m else None
    if kai is None:
        m = re.search(r"第([一二三四五六七八九十]+)回", blob)
        if m:
            KAN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                   "六": 6, "七": 7, "八": 8, "九": 9}
            s, kai = m.group(1), 0
            if "十" in s:
                a, _, b = s.partition("十")
                kai = (KAN.get(a, 1) if a else 1) * 10 + (KAN.get(b, 0) if b else 0)
            else:
                kai = KAN.get(s)
    d = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", ctx)
    date = f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else None
    return kai, date


def scrape_council(key: str, conf: dict):
    out = os.path.join(OUT_ROOT, key)
    os.makedirs(out, exist_ok=True)
    print(f"== {conf['name']} ({key})")
    links = collect_minute_links(conf["indexes"])
    print(f"  議事録リンク合計: {len(links)}件")
    done = skip = fail = 0
    for i, l in enumerate(links):
        slug = re.sub(r"[^\w]", "_", l["url"].split("/")[-1].replace(".html", ""))
        path = os.path.join(out, f"{slug}.json")
        if os.path.exists(path):
            skip += 1
            continue
        try:
            html = get(l["url"])
            title, text = extract_text(html)
            kai, date = parse_meta(title, l["ctx"])
            if not title:  # 古い回はtitleタグが無いページがある
                title = f"第{kai}回 {conf['name']}：議事録" if kai else f"{conf['name']}：議事録"
            rec = {
                "council": key,
                "council_name": conf["name"],
                "kai": kai,
                "date": date,
                "title": title[:200],
                "url": l["url"],
                "ctx": l["ctx"],
                "chars": len(text),
                "text": text,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
            done += 1
            if done % 25 == 0:
                print(f"  進捗: 取得{done} / スキップ{skip} / 失敗{fail}（{i+1}/{len(links)}）")
        except Exception as e:
            fail += 1
            print(f"  ✗ {l['url'][-40:]}: {e}")
        time.sleep(WAIT)
    print(f"  完了: 取得{done} / 既存スキップ{skip} / 失敗{fail}")
    return done, skip, fail


def main():
    keys = [k for k in sys.argv[1:] if k in COUNCILS] or list(COUNCILS)
    totals = [0, 0, 0]
    for key in keys:
        d, s, f = scrape_council(key, COUNCILS[key])
        totals = [totals[0] + d, totals[1] + s, totals[2] + f]
    print(f"\n全体: 取得{totals[0]} / スキップ{totals[1]} / 失敗{totals[2]} → {OUT_ROOT}")


if __name__ == "__main__":
    main()
