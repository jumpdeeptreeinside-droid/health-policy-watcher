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
        # 🔴 乾いた回し＝**本番と同じ組み立て関数**から値を出す。
        #    別に書き写すと、本番の形だけが検査から外れる。
        rp = row_properties(o, desc)
        sp = script_page_body(o, paras, "（①で出来た行の id が入る）")
        print("\n--dry-run: 何も作りません。送る中身（本番と同じ組み立て）:")
        print(f"   ① 本DB {config.NOTION_DATABASE_ID} に行を1つ。書く列は{len(rp)}つだけ:")
        for k, v in rp.items():
            t = v.get("title") or v.get("rich_text")
            s = (t[0]["text"]["content"] if t
                 else (v.get("select") or v.get("status") or {}).get("name", ""))
            print(f"      {k:<22}{s[:66]}{'…' if len(s) > 66 else ''}")
        print(f"      {'(Script(Podcast))':<22}ここでは入れない（②のあと③で入れる）")
        print(f"   ② その行の子ページ1つ（親の型 {sp['parent']['type']}）"
              f"／題「{sp['properties']['title'][0]['text']['content'][:34]}…」"
              f"／{len(sp['children'])}段落")
        print("   ③ その行の Script(Podcast) に ②のURL を入れる")
        print("   ④ 読み直して、URL・段落数・音声パイプラインと同じ取り出し方のIDを確かめる")
        print("   ほかの行・ほかのデータベース・ここに無い列は触らない")
        return 0

    # ① DBに行を作る（Script(Podcast) はまだ空。台本はこの行の子にする）
    row = api("POST", "/pages", {"parent": {"database_id": config.NOTION_DATABASE_ID},
                                 "properties": row_properties(o, desc)})
    print(f"  ① DBの行 {row['url']}")
    try:
        # ② 台本ページを、その行の**子ページ**として作る（通常回と同じ形）
        page = api("POST", "/pages", script_page_body(o, paras, row["id"]))
        print(f"  ② 台本ページ（この行の子） {page['url']}")
        # ③ 行の Script(Podcast) に台本のURLを入れる
        api("PATCH", f"/pages/{row['id']}",
            {"properties": {"Script(Podcast)": {"url": page["url"]}}})
        print("  ③ Script(Podcast) に台本のURLを入れました")
    except BaseException:
        print(f"\n🔴 途中で止まりました。半端な行が残っています: {row['url']}")
        print("   手で消さないこと。消す前にまとめ役へ一声")
        raise

    # ④ 読み直して確かめる（書いたつもりを信じない）
    back = api("GET", f"/pages/{row['id']}")
    url_back = (back["properties"].get("Script(Podcast)") or {}).get("url")
    kids = api("GET", f"/blocks/{page['id']}/children?page_size=100")["results"]
    ng = []
    if url_back != page["url"]:
        ng.append(f"Script(Podcast) が台本のURLと違う（{url_back}）")
    if len(kids) != len(paras[:95]):
        ng.append(f"台本の段落が{len(kids)}（渡したのは{len(paras[:95])}）")
    seen = _pipeline_page_id(url_back) if url_back else None
    if seen is None:
        print("  ⚠️ IDの取り出しは見られませんでした（音声パイプラインの道具を読めない）")
    elif seen.replace("-", "") != page["id"].replace("-", ""):
        ng.append(f"音声パイプラインと同じ取り出し方だと別のIDになる（{seen}）")
    if ng:
        print("\n🔴 確かめで合いませんでした:")
        for x in ng:
            print(f"   ・{x}")
        print(f"   行 {row['url']}\n   台本 {page['url']}")
        print("   手で消さないこと。消す前にまとめ役へ一声")
        return 1
    print(f"  ④ 確かめ: URL一致・{len(kids)}段落・IDも音声パイプラインと同じ取り出し方で一致 ✅")
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


def row_properties(o, desc):
    """本DBの行に書く列。🔴 ここに無い列は書かない。
    Script(Podcast) は入れない＝台本（その行の子ページ）を作ってから③で入れる。"""
    return {
        "Title": {"title": [{"text": {"content": o.title}}]},
        "Article＆Script Title": {"rich_text": [{"text": {"content": o.title}}]},
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
    }


def script_page_body(o, paras, parent_row_id):
    """台本ページ＝**その回の行の子ページ**（通常回と同じ形）。

    🔴 2026-10-22 に直した所（サクマ決定）:
       以前は「Script(Podcast) が入っている行のうち、いちばん最近編集されたものの
       台本の親」を置き場所にしていた（_parent_page）。海外ヘッドラインの行は
       Script(Podcast) が**自分の行**を指すので、その親はデータベース本体になり
       「既存台本の親が page_id でない（database_id）」で止まった（10/22 10:56）。
       ほかの行を見るのをやめ、通常回と同じ「自分の行の子」にした。
       （前の告知回2本の台本が関係ない行の下に居るのは、この決め方の名残り。触らない）"""
    return {"parent": {"type": "page_id", "page_id": parent_row_id},
            "properties": {"title": [{"text": {"content": f"台本 {o.title}"}}]},
            "children": [{"object": "block", "type": "paragraph",
                          "paragraph": {"rich_text": [{"type": "text", "text": {"content": p}}]}}
                         for p in paras[:95]]}


def _pipeline_page_id(url):
    """音声パイプラインが使うのと**同じ関数**でURLからページIDを取る（写しを書かない）。
    読めなければ None＝「見られなかった」と出す（通ったことにしない）。"""
    try:
        import importlib.util
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "notion_wordpress_uploader.py")
        spec = importlib.util.spec_from_file_location("nwu_for_check", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        c = m.NotionWordPressUploader
        return c._extract_notion_page_id(c.__new__(c), url)
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
