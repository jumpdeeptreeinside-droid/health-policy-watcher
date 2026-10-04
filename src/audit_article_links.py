#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""記事リンクの監査（2026-10-04 新設）。

🔴 なぜ要るのか：2026-09-23 15:08 の実行で push 段が落ち、
   **Notionは「完了」でURL(Web)も入っているのに記事がrepoに無い**行が3つ残った。
   次の実行は「完了」を見てやり直さないので恒久の穴になり、さらに
   消えた番号(pid)が後の記事に**再利用され**、配信済みの説明欄が別の記事を指した。
   404になるのは1件だけで、残り2件は**200を返すのに中身が別**＝気づけない。
   → 404を数えるだけでは足りない。**番号の重複**も数える。

検査する4つ:
  A Notion の URL(Web) が指す記事が実在するか
  B 同じ記事番号を2行以上が名乗っていないか（＝番号の再利用）
  C 配信済みエピソードの説明欄が指す記事が実在するか
  D 日本語の題に英単語の前置詞が残っていないか
  E 本文に文字化け（キリル文字）が無いか

🔴 対象が0件なら「異常なし」ではなく**検査が届いていない**として落とす
   （[[feedback_checks_can_silently_no_op]]）。
🔴 --self-test で、わざと壊した材料を流して検査が本当に捕まえるかを確かめる（陽性対照）。

使い方:
  python src/audit_article_links.py                # 監査（異常があれば終了コード1）
  python src/audit_article_links.py --self-test    # 検査そのものを試す
環境変数:
  SITE_ARTICLES_DIR  省略時 ~/crosshealthjp/src/articles-data
  SITE_EPISODES_JSON 省略時 ~/crosshealthjp/public/podcast/episodes.json
  NOTION_API_KEY / NOTION_DATABASE_ID （無ければ A・B を飛ばして C・D だけ）
"""
import argparse
import collections
import glob
import json
import os
import re
import sys
import time
import urllib.request

ART_DIR = os.environ.get("SITE_ARTICLES_DIR") or os.path.expanduser(
    "~/crosshealthjp/src/articles-data")
EPISODES = os.environ.get("SITE_EPISODES_JSON") or os.path.expanduser(
    "~/crosshealthjp/public/podcast/episodes.json")
PID_RE = re.compile(r"/articles/([0-9]+)/")
EN_PREP = re.compile(r"(?<![A-Za-z])(of|in|on|for|with|to|from|by|at|and|the|as|through)"
                     r"(?![A-Za-z])", re.I)
JP_CH = r"぀-ヿ一-鿿！-｠"


def title_warning(title: str):
    """site_uploader._title_warnings と同じ規則（日本語に隣接する前置詞だけ拾う）。"""
    hits = []
    for m in EN_PREP.finditer(title or ""):
        left, right = (title[:m.start()].rstrip(), title[m.end():].lstrip())
        if re.search(f"[{JP_CH}]$", left) or re.match(f"[{JP_CH}]", right):
            hits.append(m.group(0).lower())
    return sorted(set(hits))


MOJI = re.compile(r"[\u0400-\u04ff\u0500-\u052f]")


def load_articles(d: str) -> dict:
    """返りは pid → (題, 本文)。本文は E（文字化け）の検査に使う。"""
    out = {}
    for p in glob.glob(os.path.join(d, "*.json")):
        try:
            with open(p, encoding="utf-8") as f:
                rec = json.load(f)
            out[os.path.basename(p)[:-5]] = (rec.get("title") or "",
                                             (rec.get("html") or "") + (rec.get("summary") or ""))
        except Exception as e:
            print(f"  ⚠️ 読めない記事ファイル {os.path.basename(p)}: {e}")
    return out


def notion_rows():
    tok = os.environ.get("NOTION_API_KEY")
    db = os.environ.get("NOTION_DATABASE_ID")
    if not (tok and db):
        try:                                    # 手元では config.py から読む
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import config
            tok, db = config.NOTION_API_KEY, config.NOTION_DATABASE_ID
        except Exception:
            return None
    rows, cur = [], None
    while True:
        body = {"page_size": 100}
        if cur:
            body["start_cursor"] = cur
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{db}/query", method="POST",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {tok}", "Notion-Version": "2022-06-28",
                     "Content-Type": "application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
        rows += d["results"]
        if not d.get("has_more"):
            return rows
        cur = d["next_cursor"]
        time.sleep(0.15)


def _title_of(props: dict) -> str:
    for v in props.values():
        if v.get("type") == "title":
            return "".join(x["plain_text"] for x in v["title"])
    return ""


def checkout_staleness(art_dir: str) -> list:
    """🔴 監査は**手元のクローン**を読む。自動デプロイは別のクローン
    （~/crosshealthjp_deploy）を使うので、ここは勝手に新しくならない。
    古い材料で「異常あり/なし」を言うと嘘になる（2026-10-04 実際に5コミット遅れていて、
    本番では直っている題を「まだ of が残っている」と報告しかけた）。"""
    import subprocess
    repo = os.path.abspath(os.path.join(art_dir, "..", ".."))
    if not os.path.isdir(os.path.join(repo, ".git")):
        return [("検査", "記事の置き場がgitの中にない＝新しさを確かめられない", repo)]
    def g(*a):
        r = subprocess.run(["git"] + list(a), cwd=repo, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
        return r.returncode, (r.stdout or "").strip()
    g("fetch", "--quiet")
    rc, br = g("branch", "--show-current")
    rc, n = g("rev-list", "--count", f"HEAD..origin/{br}")
    behind = int(n) if n.isdigit() else -1
    print(f"  材料の新しさ: {repo} は origin/{br} より {behind}コミット遅れ")
    if behind > 0:
        return [("検査", f"手元のクローンが{behind}コミット遅れている＝この結果は信用できない",
                 f"{repo} で git pull してから測り直す")]
    return []


def audit(articles: dict, episodes: list, rows, strict=True) -> list:
    bad = []
    # ── A/B: Notion 側
    if rows is None:
        print("  ⚠️ Notion を見られないので A（URL(Web)）と B（番号の重複）は飛ばします")
    else:
        claims = collections.defaultdict(list)
        for p in rows:
            u = (p.get("properties", {}).get("URL(Web)") or {}).get("url") or ""
            m = PID_RE.search(u)
            if m:
                claims[m.group(1)].append(_title_of(p["properties"]))
        if strict and not claims:
            bad.append(("検査", "Notion に URL(Web) を持つ行が0件＝検査が届いていない", ""))
        print(f"  A/B 対象: Notion {len(rows)}行 / 記事番号を名乗る {sum(len(v) for v in claims.values())}行")
        for pid, who in sorted(claims.items()):
            if pid not in articles:
                bad.append(("A 記事が無い", pid, who[0][:60]))
            if len(who) > 1:
                bad.append(("B 番号の重複", pid, " ／ ".join(w[:34] for w in who)))
    # ── C: 配信済みの説明欄
    print(f"  C 対象: エピソード {len(episodes)}本 / 記事 {len(articles)}本")
    if strict and not episodes:
        bad.append(("検査", "エピソードが0件＝検査が届いていない", ""))
    for e in episodes:
        for pid in sorted(set(PID_RE.findall(e.get("description") or ""))):
            if pid not in articles:
                bad.append(("C 説明欄が死んでいる", pid, (e.get("title") or "")[:60]))
    # ── D: 題
    if strict and not articles:
        bad.append(("検査", "記事が0件＝検査が届いていない", ""))
    for pid, v in sorted(articles.items()):
        t, body = v if isinstance(v, tuple) else (v, "")
        w = title_warning(t)
        if w:
            bad.append(("D 題に英単語", pid, f"{w} {t[:50]}"))
        n = len(MOJI.findall(body))
        if n:
            bad.append(("E 本文が文字化け", pid, f"キリル文字{n}個  {t[:40]}"))
    return bad


def self_test() -> int:
    """わざと壊した材料を流して、検査が本当に捕まえるかを見る（陽性対照）。"""
    print("=== 陽性対照：わざと壊した材料で検査が落ちるか ===")
    articles = {"2026010101": ("正しい題です", "きれいな本文"),
                "2026010102": ("日本語の題に of 英単語", "きれいな本文"),
                "2026010103": ("正しい題です", "化けた本文 (з—…йҷўгҒ®)")}
    episodes = [{"title": "生きている回", "description": "/articles/2026010101/"},
                {"title": "死んだ回", "description": "/articles/9999999999/"}]
    rows = [
        {"properties": {"Name": {"type": "title", "title": [{"plain_text": "行1"}]},
                        "URL(Web)": {"type": "url", "url": "https://x/articles/2026010101/"}}},
        {"properties": {"Name": {"type": "title", "title": [{"plain_text": "行2（重複）"}]},
                        "URL(Web)": {"type": "url", "url": "https://x/articles/2026010101/"}}},
        {"properties": {"Name": {"type": "title", "title": [{"plain_text": "行3（記事なし）"}]},
                        "URL(Web)": {"type": "url", "url": "https://x/articles/8888888888/"}}},
    ]
    bad = audit(articles, episodes, rows)
    kinds = {b[0].split()[0] for b in bad}
    for b in bad:
        print(f"    捕まえた  {b[0]:<20} {b[1]}  {b[2]}")
    want = {"A", "B", "C", "D", "E"}
    missing = want - kinds
    print(f"\n  5種のうち捕まえた: {sorted(kinds)}")
    if missing:
        print(f"  🔴 捕まえられない種類がある: {sorted(missing)}")
        return 1
    # 陰性対照：きれいな材料なら0件か
    clean = audit({"2026010101": ("正しい題です", "きれいな本文")},
                  [{"title": "回", "description": "/articles/2026010101/"}],
                  [{"properties": {"Name": {"type": "title", "title": [{"plain_text": "行"}]},
                                   "URL(Web)": {"type": "url",
                                                "url": "https://x/articles/2026010101/"}}}])
    print(f"  陰性対照（きれいな材料）: {len(clean)}件 {'✅' if not clean else '🔴 誤検知'}")
    return 0 if not clean else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true", help="検査そのものを試す（陽性対照）")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    print("=== 記事リンクの監査 ===")
    if not os.path.isdir(ART_DIR):
        print(f"🔴 記事の置き場が無い: {ART_DIR}")
        return 1
    articles = load_articles(ART_DIR)
    episodes = []
    if os.path.exists(EPISODES):
        with open(EPISODES, encoding="utf-8") as f:
            episodes = json.load(f)
    else:
        print(f"  ⚠️ エピソードの一覧が無い: {EPISODES}")
    try:
        rows = notion_rows()
    except Exception as e:
        print(f"  ⚠️ Notion を読めない: {e}")
        rows = None

    bad = checkout_staleness(ART_DIR) + audit(articles, episodes, rows)
    print()
    if not bad:
        print("✅ 異常なし")
        return 0
    print(f"🔴 {len(bad)}件の異常")
    for kind, pid, who in bad:
        # 🔴 行頭の "NG\t" が機械の読む印。進行の行（対象:…）と混ざらないようにする
        #    （印が無かったら朝の見張りが進行の行まで数えて「10件」と誤報した・2026-10-04）
        print(f"NG\t{kind}\t{pid}\t{who}")
    print("\n直し方の目安")
    print("  A 記事が無い            … Notion の Status(Web) を「投稿待ち」に戻して作り直す")
    print("  B 番号の重複            … 古い行の URL(Web) を消して作り直す（配信済みの説明欄は人が直す）")
    print("  C 説明欄が死んでいる    … episodes.json の説明欄を直す（crosshealthjp 側・人が判断）")
    print("  D 題に英単語            … Notion の題を直し、記事ファイルの title も直す")
    print("  E 本文が文字化け        … 原文（Notion）の時点で化けている。原典でPDF名を確かめて直す")
    return 1


if __name__ == "__main__":
    sys.exit(main())
