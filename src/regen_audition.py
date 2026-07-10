#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""試聴待ちを新製造ラインで再生成（2026-07-10）。
- 英語を含む台本のみGeminiで日本語化(それ以外は再合成のみ)→build_episodeで新合成(yomi_preprocess+緑さんシート適用)
- 出力はDrive試聴フォルダ直下のみ(obsidianバックアップなし・翔太さん指示)
- Notion状態は試聴待ちのまま(再チェック用)・AudioPathを新ファイルに更新
使い方: python3 regen_audition.py --limit N   (N件だけ試す) / 引数なしで全件
"""
import os, sys, re, json, argparse, subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('WORDPRESS_URL','https://unused.invalid')
os.environ.setdefault('WORDPRESS_USERNAME','u'); os.environ.setdefault('WORDPRESS_APP_PASSWORD','p')

import mac_audio_pipeline as m
from notion_wordpress_uploader import NotionWordPressUploader

# 再生成専用のtts作業ディレクトリ(launchd本番と衝突回避)
m.WORK_DIR = os.path.expanduser("~/health-policy-watcher/output/tts_regen")

def japanize_english(script: str) -> str:
    """台本中の英語表記だけを日本語/カタカナに。英語が無ければそのまま返す。"""
    if not re.search(r"[A-Za-z]{3,}", script):
        return script
    import google.generativeai as genai, config
    genai.configure(api_key=config.GEMINI_API_KEY)
    prompt = (
        "次の日本語ポッドキャスト台本の中の【英語表記だけ】を、意味を変えずに日本語またはカタカナに直してください。\n"
        "規則:\n"
        "- 機関名・薬剤名・固有名詞・略語を日本語かカタカナに（例: WHO→世界保健機関、AMR→薬剤耐性、"
        "RUXOLITINIB→ルキソリチニブ、Policy Update→政策アップデート）。\n"
        "- 英語以外（日本語の文章・数字・句読点・改行）は一字一句変えないでください。文章を書き換えたり要約したりしないこと。\n"
        "- 台本テキストのみを出力（前置き・コードブロック不要）。\n\n"
        + script
    )
    model = genai.GenerativeModel(m.QC_MODEL)
    return model.generate_content(prompt).text.strip().strip("`").removeprefix("markdown").strip()

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true"); args = ap.parse_args()

    nw = NotionWordPressUploader()
    m._READING_OVERRIDES = m.load_reading_overrides()  # 緑さんシート
    os.makedirs(m.DRIVE_AUDITION, exist_ok=True)

    pages = nw.query_database({'property':'Status(Podcast)','status':{'equals':'試聴待ち'}})
    if args.limit: pages = pages[:args.limit]
    print(f"対象 {len(pages)} 件\n", flush=True)

    done=0
    for pg in pages:
        page_id = pg["id"]
        db_title = nw.get_property_value(pg,"Title") or "無題"
        prop = pg.get("properties",{}).get("Script(Podcast)",{})
        spid = nw._extract_notion_page_id(prop["url"]) if prop.get("type")=="url" and prop.get("url") else None
        if not spid:
            print(f"⏭ {db_title[:40]} ... Script(Podcast)リンク無し", flush=True); continue
        title = nw.fetch_page_title(spid) or db_title
        blocks = nw.fetch_page_blocks(spid)
        body = m.md_to_plain(nw.converter.convert(blocks))
        if len(body) < 30:
            print(f"⏭ {title[:40]} ... 台本が短い", flush=True); continue
        had_en = bool(re.search(r"[A-Za-z]{3,}", body))
        body = japanize_english(body)
        print(f"▶ {title[:42]} {'(英語→日本語化)' if had_en else '(再合成のみ)'}", flush=True)
        if args.dry:
            print("   [dry] 生成スキップ", flush=True); continue
        from datetime import datetime
        out_mp3 = os.path.join(m.DRIVE_AUDITION, f"{datetime.now().strftime('%Y%m%d')}_{m.sanitize(title)}.mp3")
        if os.path.exists(out_mp3) and os.path.getsize(out_mp3) > 100000:
            print("   ⏭ 既に生成済み・スキップ", flush=True); continue
        if not m.build_episode(title, body, out_mp3):
            print("   ✗ 合成失敗", flush=True); continue
        dur = subprocess.run(["afinfo", out_mp3], capture_output=True, text=True)
        mm = re.search(r"estimated duration: ([\d.]+)", dur.stdout)
        print(f"   ✅ {os.path.basename(out_mp3)}（{float(mm.group(1))/60:.1f}分）" if mm else "   ✅ 完成", flush=True)
        try:
            import requests
            requests.patch(f"{nw.notion_base}/pages/{page_id}", headers=nw.notion_headers,
                json={"properties":{"AudioPath":{"rich_text":[{"text":{"content":out_mp3}}]}}}, timeout=30)
        except Exception as e:
            print(f"   ⚠ AudioPath更新失敗: {e}", flush=True)
        done+=1
    print(f"\n完了: {done} 件生成", flush=True)

if __name__=="__main__":
    main()
