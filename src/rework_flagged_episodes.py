#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""医療政策ウォッチャー: 人間チェックで「やり直し」になった回を一括で再生成する自動化（サクマ 2026-07-22）。
Google Driveの試聴フォルダ配下「1回目視聴済_修正点あり」等に入れたmp3を、対応するNotion回に照合し、
Status(Podcast)を「音声化待ち」に戻して mac_audio_pipeline を回す。
＝合成→AI検品→試聴フォルダへ自動コピー→Notion更新 まで、いつもの正規フローに乗せ直す。

読み修正の考え方（翔太さん合意）:
  - 数字・カウンター（人/名/件/組/対/千 複合 等）＝ yomi_preprocess で全回一括（恒久対策・実装済）
  - 難読語（片頭痛=ヘンズツウ 等）＝ 各回のAI検品（Gemini）が自動検出→辞書登録→再合成
  - ＝人が拾った誤読は yomi + AI検品 の二段で潰れる。Notionハイライトは参考情報として一覧表示する。

使い方:
  python3 src/rework_flagged_episodes.py                    # 既定フォルダを照合→Status戻し→合成
  python3 src/rework_flagged_episodes.py --dry-run          # 照合と一覧だけ（Notion/合成に触れない）
  python3 src/rework_flagged_episodes.py --no-synth         # Status戻しまで（合成は次の定期実行 or 手動に任せる）
  python3 src/rework_flagged_episodes.py --folder "パス"    # 対象フォルダを指定
  python3 src/rework_flagged_episodes.py --archive          # 再生成後、旧mp3を _修正済_旧 へ退避
"""
import argparse, glob, os, re, sys, unicodedata, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('WORDPRESS_URL', 'https://unused.invalid')
os.environ.setdefault('WORDPRESS_USERNAME', 'u')
os.environ.setdefault('WORDPRESS_APP_PASSWORD', 'p')

DEFAULT_FOLDER = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-tekutekuradio@gmail.com/マイドライブ/CrossHealth/Podcast試聴/1回目視聴済_修正点あり")
TITLE_PROPS = ["Article＆Script Title", "Title", "Article(Web)"]


def norm(s):
    """全角→半角(NFKC)＋日付プレフィクス除去＋記号除去 でファイル名とNotionタイトルを比較可能に。"""
    s = unicodedata.normalize('NFKC', str(s)).replace('.mp3', '')
    s = re.sub(r'^(修正待ち_)?\d{8}_?', '', s)
    return re.sub(r'[\s　「」、。！？：・（）\(\)\[\]【】~〜\-_,!?.＆&／/、]', '', s)


def match_episodes(nw, mp3s):
    """mp3ファイル名 → Notion回(page)。返り値: matched{mp3:(page,title)}, unmatched[mp3]"""
    mp3norm = {norm(m): m for m in mp3s}
    pages = nw.query_database({})
    matched = {}
    for p in pages:
        for prop in TITLE_PROPS:
            t = nw.get_property_value(p, prop) or ""
            tn = norm(t)
            for mn, mf in list(mp3norm.items()):
                if mf in matched:
                    continue
                if tn and (tn == mn or (len(mn) >= 12 and mn in tn) or (len(tn) >= 12 and tn in mn)):
                    matched[mf] = (p, t)
                    break
    unmatched = [m for m in mp3s if m not in matched]
    return matched, unmatched


def read_highlights(nw, page):
    """台本ページの色付き(ハイライト)テキスト＝人間が修正指摘した箇所。参考表示用。"""
    sc = page.get("properties", {}).get("Script(Podcast)", {})
    url = sc.get("url") if sc.get("type") == "url" else None
    if not url:
        return []
    spid = nw._extract_notion_page_id(url)
    if not spid:
        return []
    try:
        blocks = nw.fetch_page_blocks(spid)
    except Exception:
        return []
    hl = []
    for b in blocks:
        bt = b.get(b.get("type", ""), {})
        for rt in (bt.get("rich_text", []) if isinstance(bt, dict) else []):
            ann = rt.get("annotations", {})
            if ann.get("color", "default") != "default":
                w = (rt.get("plain_text", "") or "").strip()
                if w and w not in [h[0] for h in hl]:
                    hl.append((w, ann.get("color")))
    return hl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default=DEFAULT_FOLDER)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-synth", action="store_true")
    ap.add_argument("--archive", action="store_true")
    args = ap.parse_args()

    from notion_wordpress_uploader import NotionWordPressUploader
    import requests
    nw = NotionWordPressUploader()

    mp3s = sorted(os.path.basename(f) for f in glob.glob(os.path.join(args.folder, "*.mp3")))
    if not mp3s:
        print(f"対象mp3なし: {args.folder}")
        return
    print(f"📁 対象フォルダ: {args.folder}")
    print(f"🎧 やり直し対象: {len(mp3s)}本 → Notion照合中…")

    matched, unmatched = match_episodes(nw, mp3s)
    print(f"\n✅ 照合: {len(matched)}/{len(mp3s)}本マッチ")

    # ハイライト(人間の指摘)を参考表示
    for mf, (p, t) in list(matched.items())[:200]:
        hl = read_highlights(nw, p)
        note = ("  指摘ハイライト: " + ", ".join(f"{w}[{c.replace('_background','')}]" for w, c in hl)) if hl else ""
        st = nw.get_property_value(p, "Status(Podcast)") or ""
        print(f"  ✓ {mf[:50]} [{st}]{note}")
    if unmatched:
        print(f"\n⚠ 未マッチ {len(unmatched)}本（重複回 or タイトル差・手動確認）:")
        for u in unmatched:
            print(f"    ✗ {u}")

    # ユニークなNotion回
    pid_map = {}
    for mf, (p, t) in matched.items():
        pid_map.setdefault(p["id"], []).append(mf)
    print(f"\n対象ユニーク回: {len(pid_map)}件")

    if args.dry_run:
        print("\n[dry-run] Notion/合成には触れていません。")
        return

    # Status(Podcast) → 音声化待ち
    ok = 0
    for pid in pid_map:
        try:
            requests.patch(f"{nw.notion_base}/pages/{pid}", headers=nw.notion_headers,
                           json={"properties": {"Status(Podcast)": {"status": {"name": "音声化待ち"}}}},
                           timeout=30).raise_for_status()
            ok += 1
        except Exception as e:
            print(f"  NG {pid}: {e}")
    print(f"🔄 Status→音声化待ち: {ok}/{len(pid_map)}件")

    if args.archive:
        arc = os.path.join(args.folder, "_修正済_旧")
        os.makedirs(arc, exist_ok=True)
        for mf in matched:
            try:
                shutil.move(os.path.join(args.folder, mf), os.path.join(arc, mf))
            except Exception:
                pass
        print(f"🗄 旧mp3を退避: {arc}")

    if args.no_synth:
        print("\n--no-synth: Status戻しまで。合成は次の定期実行 or `python3 src/mac_audio_pipeline.py` で。")
        return

    # 正規フローで合成（合成→AI検品→試聴フォルダ→Notion更新）
    print("\n🎙 合成パイプライン開始（各回: 合成→AI検品→Google Drive試聴フォルダ→Notion更新）…")
    from mac_audio_pipeline import process_notion
    done = process_notion(dry_run=False)
    print(f"\n🎉 完了: {done}件を再生成し、試聴フォルダへ配置しました。")


if __name__ == "__main__":
    main()
