# -*- coding: utf-8 -*-
"""告知回（新しいページ・レポートを知らせる回）をNotionに1件入れる。

なぜ要るか（2026-10-03 翔太さん方針）:
  これから新しいものはPodcastで知らせる。流れは通常のウォッチャー業務フローと同じ
  （音声化待ち→自動で音声化→試聴待ち→翔太さんの試聴が承認→公開待ち→配信）。
  ただし告知回はニュース収集から生まれないので、Notionに入れる手段が無かった。

🔴 通常回と違う点（ここを間違えると他のジョブが拾ってしまう）
  Status(Web)        = "-"   … 解説記事は作らないので、WordPress/サイトに投稿させない
  Status(コンテンツ作成) = "完了" … ファクトチェック待ちに入れない（元記事URLが無いので検証不能）
  Category           = 国内系 … 国際系にすると「夕方のまとめ対象」でスキップされる
  PodcastDescription = 手で入れる
     🔴 通常回は update_podcast_description() がリンク4本の定型を書くが、
        それは解説記事を投稿したときだけ走る（Status(Web)="-" なら走らない）。
        告知回の決まりは「リンク1本だけ」なので、定型を使わず手で入れる。

使い方:
  python3 src/add_announce_episode.py --title "…" --script 原稿.md --desc 説明欄.txt --dry-run
  python3 src/add_announce_episode.py --title "…" --script 原稿.md --desc 説明欄.txt
"""
import argparse, json, os, re, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

BASE = "https://api.notion.com/v1"
H = {"Authorization": f"Bearer {config.NOTION_API_KEY}",
     "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
MAX_BLOCK = 2000          # Notionのテキストブロックは2,000字まで


def api(method, path, body=None):
    req = urllib.request.Request(f"{BASE}{path}", headers=H, method=method,
                                 data=json.dumps(body).encode() if body else None)
    return json.load(urllib.request.urlopen(req, timeout=60))


def md_body(path):
    """frontmatterと見出し記号を落として、段落の配列にする。"""
    t = open(os.path.expanduser(path), encoding="utf-8").read()
    t = re.sub(r"^---.*?^---\n", "", t, flags=re.S | re.M)
    t = re.sub(r"^#\s*", "", t, flags=re.M)
    return [p.strip() for p in t.split("\n") if p.strip()]


def main() -> int:
    a = argparse.ArgumentParser()
    a.add_argument("--title", required=True)
    a.add_argument("--script", required=True, help="原稿のmdファイル")
    a.add_argument("--desc", required=True, help="説明欄のテキストファイル")
    a.add_argument("--category", default="国内・保健")
    a.add_argument("--dry-run", action="store_true")
    o = a.parse_args()

    paras = md_body(o.script)
    desc = open(os.path.expanduser(o.desc), encoding="utf-8").read().strip()
    chars = sum(len(p) for p in paras)

    # 🔴 入れる前に止める検査。ここで弾けば本番のキューを汚さない
    links = re.findall(r"https?://\S+", desc)
    if len(links) != 1:
        sys.exit(f"NG: 説明欄のリンクが{len(links)}本（告知回は1本だけ）: {links}")
    if not 900 <= chars <= 1700:
        sys.exit(f"NG: 原稿{chars}字。3〜5分（約990〜1,650字）から外れている")
    if any(x in "".join(paras) for x in ("木内翔太", "木内")):
        sys.exit("NG: 原稿に本人の名乗りがある。自動音声の回に入れない")
    if any(len(p) > MAX_BLOCK for p in paras):
        sys.exit("NG: 2,000字を超える段落がある（Notionのブロック上限）")
    print(f"  検査通過: 原稿{chars}字・{len(paras)}段落／説明欄のリンク1本 {links[0]}")

    if o.dry_run:
        print("\n--dry-run: 何も作りません。入れる内容:")
        print(f"   Title                 {o.title}")
        print(f"   Category              {o.category}")
        print(f"   Status(Podcast)       音声化待ち")
        print(f"   Status(Web)           -       （解説記事は作らない）")
        print(f"   Status(コンテンツ作成)   完了     （ファクトチェックに入れない）")
        print(f"   PodcastDescription    {desc[:60]}…")
        print(f"   台本ページ              {len(paras)}段落")
        return 0

    # ① 台本ページを作る（Script(Podcast) はNotionページURLでなければならない）
    page = api("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": _parent_page()},
        "properties": {"title": [{"text": {"content": f"台本 {o.title}"}}]},
        "children": [{"object": "block", "type": "paragraph",
                      "paragraph": {"rich_text": [{"type": "text", "text": {"content": p}}]}}
                     for p in paras[:95]]})
    script_url = page["url"]
    print(f"  ① 台本ページ {script_url}")

    # ② DBに行を作る
    row = api("POST", "/pages", {
        "parent": {"database_id": config.NOTION_DATABASE_ID},
        "properties": {
            "Title": {"title": [{"text": {"content": o.title}}]},
            "Article＆Script Title": {"rich_text": [{"text": {"content": o.title}}]},
            "Script(Podcast)": {"url": script_url},
            "PodcastDescription": {"rich_text": [{"text": {"content": desc[:MAX_BLOCK]}}]},
            "Category": {"select": {"name": o.category}},
            "Status(Podcast)": {"status": {"name": "音声化待ち"}},
            "Status(Web)": {"status": {"name": "-"}},
            "Status(コンテンツ作成)": {"status": {"name": "完了"}},
        }})
    print(f"  ② DBの行 {row['url']}")
    print("\n  次に起きること: 毎時:15の音声パイプラインが音声化し、**試聴待ち**で止まります。")
    print("  配信は、人が「公開待ち」に変えてから。")
    return 0


def _parent_page():
    """台本ページの置き場所＝既存の台本ページと同じ親にする。"""
    r = api("POST", f"/databases/{config.NOTION_DATABASE_ID}/query",
            {"filter": {"property": "Script(Podcast)", "url": {"is_not_empty": True}},
             "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
             "page_size": 1})
    if not r["results"]:
        sys.exit("NG: 既存の台本ページが見つからないので、置き場所を決められない")
    u = r["results"][0]["properties"]["Script(Podcast)"]["url"]
    pid = re.search(r"([0-9a-f]{32})", u.replace("-", ""))
    if not pid:
        sys.exit(f"NG: 台本URLからページIDを取れない: {u}")
    p = api("GET", f"/pages/{pid.group(1)}")
    par = p.get("parent", {})
    if par.get("type") != "page_id":
        sys.exit(f"NG: 既存台本の親が page_id でない（{par.get('type')}）。置き場所を手で決めること")
    return par["page_id"]


if __name__ == "__main__":
    sys.exit(main())
