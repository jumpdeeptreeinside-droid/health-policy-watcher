# -*- coding: utf-8 -*-
"""告知回（新しいページ・レポートを知らせる回）をNotionに1件入れる。

なぜ要るか（2026-10-03 翔太さん方針）:
  これから新しいものはPodcastで知らせる。流れは通常のウォッチャー業務フローと同じ
  （音声化待ち→自動で音声化→試聴待ち→翔太さんの試聴が承認→公開待ち→配信）。
  ただし告知回はニュース収集から生まれないので、Notionに入れる手段が無かった。

🔴 通常回と違う点（ここを間違えると他のジョブが拾ってしまう）
  Status(Web)        = "完了" … 解説記事は作らせない。🔴 "-" は自動化が「投稿待ち」に戻すので禁止
  Status(コンテンツ作成) = "完了" … ファクトチェック待ちに入れない（元記事URLが無いので検証不能）
  Category           = 国内系 … 国際系にすると「夕方のまとめ対象」でスキップされる
  PodcastDescription = 手で入れる
     🔴 通常回は update_podcast_description() がリンク4本の定型を書くが、
        それは解説記事を投稿したときだけ走る（Status(Web)が「投稿待ち」でなければ走らない）。
        告知回の決まりは「リンク1本だけ」なので、定型を使わず手で入れる。

使い方:
  python3 src/add_announce_episode.py --title "…" --script 原稿.md --desc 説明欄.txt --dry-run
  python3 src/add_announce_episode.py --title "…" --script 原稿.md --desc 説明欄.txt
"""
import argparse, json, os, re, sys, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import datetime as _dt

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
    body = "".join(paras)

    # 🔴 使ってはいけない言い方。原典で確かめられない強さの表現は止める。
    #    「徒歩10分」の根拠は、国の都市評価が徒歩圏を半径800mとしていることと、
    #    不動産広告の換算（道路距離80mで1分）を当てた目安であって、
    #    国が基準として定めたものではない（2026-10-03 原典確認）。
    NG_WORDS = ["広く使われている", "基準として定められ", "国が定めた基準",
                "一般的な基準", "法律で決まって"]
    bad = [w for w in NG_WORDS if w in body]
    if bad:
        sys.exit(f"NG: 原典で確かめられない言い方があります: {bad}")
    # 🔴 3〜5分が決まり（告知回の決まり③）。ただし「なぜ」を入れる回は6分まで許す
    #    （2026-10-03 翔太さんの指示で大阪の回を5.4分にした）。
    #    1分あたり約330字で換算。6分=約1,980字。
    if not 900 <= chars <= 1980:
        sys.exit(f"NG: 原稿{chars}字。3〜6分（約990〜1,980字）から外れている")
    if chars > 1700:
        print(f"  ⚠️ 原稿{chars}字＝約{(chars+103)/330:.1f}分。決まりの3〜5分を超えています"
              f"（6分までは翔太さんの指示があるときのみ）")
    if any(x in "".join(paras) for x in ("木内翔太", "木内")):
        sys.exit("NG: 原稿に本人の名乗りがある。自動音声の回に入れない")
    if any(len(p) > MAX_BLOCK for p in paras):
        sys.exit("NG: 2,000字を超える段落がある（Notionのブロック上限）")

    # 🔴 読みが崩れた実例（2026-10-03・大阪の回）を次から出さないための注意書き。
    #    止めずに警告だけ出す（正しく読める場合もあるので、判断は人に残す）。
    #    「◯分」: VOICEPEAKのユーザー辞書に 10→ジュウ と 十分→ジュウブン があり、
    #             「徒歩10分」が「とほじゅうぶん」と読まれた。かなで「じゅっぷん」と書けば直る。
    #    単独の「数」: 「かず」でなく「すう」と読まれた。「数字」「数え方」は正しく読める。
    body = "".join(paras)


    warn = []
    for m in re.finditer(r"[0-9０-９]+分", body):
        warn.append(f"「{m.group(0)}」→ かなで書かないと『ぶん』と読まれることがある（例 じゅっぷん）")
    for m in re.finditer(r"(?<![字え学量値件回人日者点個軒])数(?![字え学量値件回人日者点個軒])", body):
        i = m.start()
        warn.append(f"単独の「数」…{body[max(0, i-10):i+10]}… → 『すう』と読まれる。かなで「かず」と書く")
    if warn:
        print("  ⚠️ 読みが崩れやすい箇所があります（止めません。音声で確かめてください）")
        for w in dict.fromkeys(warn):
            print(f"     ・{w}")

    print(f"  検査通過: 原稿{chars}字・{len(paras)}段落／説明欄のリンク1本 {links[0]}")

    if o.dry_run:
        print("\n--dry-run: 何も作りません。入れる内容:")
        print(f"   Title                 {o.title}")
        print(f"   Category              {o.category}")
        print(f"   Status(Podcast)       音声化待ち")
        print("   Status(Web)           完了     （解説記事は作らせない。ハイフンは自動化が戻す）")
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
            # 🔴 "-" にしてはいけない（2026-10-03 実害が出る寸前だった）。
            #    notion_status_automation.py が毎時、
            #    「Status(コンテンツ作成)=完了 かつ Status(Web)="-"」の行を
            #    Status(Web)→「投稿待ち」に書き換える。告知回は常に
            #    Status(コンテンツ作成)=完了 なので、必ず対象になる。
            #    「投稿待ち」になると解説記事が作られ、さらに
            #    update_podcast_description() が説明欄をリンク4本の定型に上書きする
            #    ＝告知回の「リンク1本だけ」が壊れる。
            #    記事を作るジョブは「投稿待ち」しか見ないので「完了」なら拾われない。
            "Status(Web)": {"status": {"name": "完了"}},
            "Status(コンテンツ作成)": {"status": {"name": "完了"}},
        }})
    print(f"  ② DBの行 {row['url']}")
    # 🔴 2026-10-04: 15:15:00 に入れたら、同じ分に走った音声パイプラインが
    #    Notionを読む瞬間に行が見えず、1時間待つことになった（失敗ではない）。
    #    「分」単位で動くジョブと同じ分に物を入れない。
    _m = _dt.datetime.now().minute          # このMacの時計はJST
    if 14 <= _m <= 16:
        print(f"  ⚠️ いま:{_m:02d}分です。音声パイプラインは毎時:15に走るので、"
              "この行が見えず**次の回（1時間後）まで待つ**ことがあります。")
        print("     急ぐときは :17 以降に入れ直すか、1時間待ってください。")
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
    # 🔴 末尾の32桁がページID。先頭から32桁切ると桁がずれる（2026-10-03 実際に404）。
    #    app.notion.com は "/p/<スラッグ>-<ID>" の形で、スラッグに数字が入ると
    #    ハイフンを外した文字列の先頭がスラッグの数字から始まる。
    #    例 "/p/1-67-3ee8…87d" → "1673ee8…87d"(35桁) の先頭32桁は別物。
    runs = re.findall(r"[0-9a-f]{32,}", u.replace("-", ""))
    if not runs:
        sys.exit(f"NG: 台本URLからページIDを取れない: {u}")
    p = api("GET", f"/pages/{runs[-1][-32:]}")
    par = p.get("parent", {})
    if par.get("type") != "page_id":
        sys.exit(f"NG: 既存台本の親が page_id でない（{par.get('type')}）。置き場所を手で決めること")
    return par["page_id"]


if __name__ == "__main__":
    sys.exit(main())
