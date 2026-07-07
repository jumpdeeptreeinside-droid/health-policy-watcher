#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中医協（中央社会保険医療協議会・総会）議事録の全アーカイブ取得（2026-07-07 木内さん発案）。

MHLWの総会ページ＋過去アーカイブ5ページから議事録リンクを全て収集し、
本文テキストを ~/chuikyo_archive/ にJSONで保存する。Phase2（構造化DB）の原材料。
公的資料・1.5秒間隔の丁寧なクロール。再実行時は取得済みをスキップ（差分だけ取る）。
"""
import json
import os
import re
import time
import urllib.request

from bs4 import BeautifulSoup

BASE = "https://www.mhlw.go.jp"
INDEXES = [
    "/stf/shingi/shingi-chuo_128154.html",       # 現行（第620回〜）
    "/stf/shingi/shingi-chuo_128154old3.html",   # 〜第619回
    "/stf/shingi/shingi-chuo_128154_00015.html", # 〜第569回
    "/stf/shingi/shingi-chuo_128154old2.html",   # 〜第520回
    "/stf/shingi/shingi-chuo_128154old1.html",   # 〜第481回
    "/stf/shingi/shingi-chuo_128154old.html",    # 〜第440回
]
OUT = os.path.expanduser("~/chuikyo_archive")

# 部会アーカイブ（2026-07-08未明 Phase2拡張）。総会と同じ書式・別ディレクトリに保存。
BODIES = {
    "yakka": {
        "name": "薬価専門部会",
        "out": os.path.expanduser("~/chuikyo_bukai_archive/yakka"),
        "indexes": [
            "/stf/shingi/shingi-chuo_128157.html",        # 第212回〜
            "/stf/shingi/shingi-chuo_128157_00008.html",  # 第162〜211回
            "/stf/shingi/shingi-chuo_128157old.html",     # 第65〜161回
        ],
    },
}
UA = {"User-Agent": "Mozilla/5.0 (CrossHealth research; contact: jump.deep.tree.inside@gmail.com)"}
WAIT = 1.5


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


def collect_minute_links() -> list:
    links = []
    for idx in INDEXES:
        try:
            html = get(BASE + idx)
        except Exception as e:
            print(f"✗ index取得失敗 {idx}: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if a.get_text(strip=True) != "議事録":
                continue
            href = a["href"]
            url = href if href.startswith("http") else BASE + href
            # 行のテキストから回数・日付のヒントを拾う（無ければ本文ページから補完）
            row = a.find_parent("tr")
            ctx = row.get_text(" ", strip=True)[:120] if row else ""
            links.append({"url": url, "ctx": ctx})
        print(f"  {idx}: 累計{len(links)}件")
        time.sleep(WAIT)
    # URL重複除去
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


def main():
    import sys
    body = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in BODIES else None
    global OUT, INDEXES
    if body:
        OUT = BODIES[body]["out"]
        INDEXES = BODIES[body]["indexes"]
        print(f"== 中医協 {BODIES[body]['name']} ==")
    os.makedirs(OUT, exist_ok=True)
    links = collect_minute_links()
    print(f"議事録リンク合計: {len(links)}件")
    done = skip = fail = 0
    for i, l in enumerate(links):
        slug = re.sub(r"[^\w]", "_", l["url"].split("/")[-1].replace(".html", ""))
        path = os.path.join(OUT, f"{slug}.json")
        if os.path.exists(path):
            skip += 1
            continue
        try:
            html = get(l["url"])
            title, text = extract_text(html)
            m = re.search(r"第(\d+)回", title + " " + l["ctx"])
            rec = {
                "kai": int(m.group(1)) if m else None,
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
    print(f"\n完了: 取得{done} / 既存スキップ{skip} / 失敗{fail} → {OUT}")


if __name__ == "__main__":
    main()
