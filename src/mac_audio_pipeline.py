#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mac音声パイプライン（オペ改訂③・2026-07-06）

Notionで Status(Podcast)=「音声化待ち」の記事を検出し、Script(Podcast) リンク先の台本を
VOICEPEAK CLI（Mac・宮舞モカ）で自動音声化して、完成mp3を出力する。
これまでWindows+GUI手作業だった工程の置き換え。残る手作業はSpotify for Creatorsへのアップのみ。

エピソード構成（既存の.vppプロジェクトから抽出した現行仕様を再現）:
  OP（固定文言・話速100）→ タイトル読み（話速100）→ 本文（話速120）→ ED（固定文言・話速100）
  全ブロック: 宮舞モカ / ピッチ-35セント。ユーザー辞書はVOICEPEAK側（dic.json・261語）が効く。

使い方:
  python3 src/mac_audio_pipeline.py            # 音声化待ちを全部処理
  python3 src/mac_audio_pipeline.py --dry-run  # 検出だけして合成しない
  python3 src/mac_audio_pipeline.py --text ファイル.md --title "タイトル"  # Notionを使わず単発合成（テスト用）

設定: src/config.py に NOTION_API_KEY / NOTION_DATABASE_ID（無ければ環境変数）
出力: ~/obsidian-brain/_podcast/02_編集後/YYYYMMDD_タイトル.mp3
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# WordPress設定必須のモジュールを再利用するためのダミー（WP APIは呼ばない）
os.environ.setdefault('WORDPRESS_URL', 'https://unused.invalid')
os.environ.setdefault('WORDPRESS_USERNAME', 'unused')
os.environ.setdefault('WORDPRESS_APP_PASSWORD', 'unused')

VOICEPEAK = "/Applications/voicepeak.app/Contents/MacOS/voicepeak"
OUT_DIR = os.path.expanduser("~/obsidian-brain/_podcast/02_編集後")
WORK_DIR = os.path.expanduser("~/obsidian-brain/_podcast/_Output/tts")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # 任意: 完成通知

NARRATOR = "宮舞モカ"
PITCH = "-35"          # GUIの-0.35に相当（セント）
SPEED_TALK = "100"     # OP/タイトル/ED
SPEED_BODY = "120"     # 本文（現行.vppの実測値）
CHUNK_LIMIT = 120      # CLIの1回あたり文字数上限（140の安全側）
SYNTH_TIMEOUT = 180    # 1チャンクの合成タイムアウト（秒）

OP_TEXT = "こんにちは。メインパーソナリティーの、ばんじょうサクです。"
ED_TEXT = (
    "ニュースは以上となります。"
    "お相手はメインパーソナリティーのばんじょうさくでした。"
    "難解な医療政策を、もっと身近に。もっと手軽に。医療政策ウォッチャー。"
)


def md_to_plain(md: str) -> str:
    """台本Markdown→読み上げテキスト（見出し記号・装飾・URLを除去）"""
    t = re.sub(r"^#.*$", "", md, flags=re.M)          # 見出し行（タイトルは別で読む）
    t = re.sub(r"\*\*?|__|`+", "", t)                  # 強調・コード
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)   # リンク→アンカー文字
    t = re.sub(r"https?://\S+", "", t)                 # 生URL
    t = re.sub(r"^[-*+]\s+", "", t, flags=re.M)       # 箇条書き記号
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def chunk_sentences(text: str, limit: int = CHUNK_LIMIT) -> list:
    """句点で区切って limit 文字以内のチャンクにまとめる"""
    sentences = [s.strip() for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    chunks, cur = [], ""
    for s in sentences:
        if len(s) > limit:  # 異常に長い一文は読点で強制分割
            parts = [p for p in re.split(r"(?<=、)", s) if p]
            for p in parts:
                if len(cur) + len(p) > limit and cur:
                    chunks.append(cur); cur = ""
                cur += p
            continue
        if len(cur) + len(s) > limit and cur:
            chunks.append(cur); cur = ""
        cur += s
    if cur:
        chunks.append(cur)
    return chunks


def synth(text: str, out_wav: str, speed: str) -> bool:
    """VOICEPEAK CLIで1チャンク合成（タイムアウト・1回リトライ付き）"""
    cmd = [VOICEPEAK, "-s", text, "--narrator", NARRATOR,
           "--speed", speed, "--pitch", PITCH, "-o", out_wav]
    for attempt in (1, 2):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=SYNTH_TIMEOUT)
            if os.path.exists(out_wav) and os.path.getsize(out_wav) > 1000:
                return True
            print(f"  ⚠ 合成出力なし (試行{attempt}): {r.stderr[-120:] if r.stderr else ''}")
        except subprocess.TimeoutExpired:
            print(f"  ⚠ 合成タイムアウト (試行{attempt})")
        time.sleep(3)
    return False


def synth_long(text: str, out_wav: str, speed: str, tag: str) -> bool:
    """長文をチャンク合成して連結"""
    os.makedirs(WORK_DIR, exist_ok=True)
    chunks = chunk_sentences(text)
    print(f"  {tag}: {len(text)}字 → {len(chunks)}チャンク")
    parts = []
    for i, c in enumerate(chunks):
        part = os.path.join(WORK_DIR, f"{tag}_{i:03d}.wav")
        if not synth(c, part, speed):
            print(f"  ✗ チャンク{i}合成失敗: {c[:40]}")
            return False
        parts.append(part)
    # ffmpegで連結
    lst = os.path.join(WORK_DIR, f"{tag}_list.txt")
    with open(lst, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", out_wav], capture_output=True, text=True)
    for p in parts + [lst]:
        os.remove(p)
    return r.returncode == 0


def build_episode(title: str, body_text: str, out_mp3: str) -> bool:
    """OP→タイトル→本文→ED を合成してmp3に仕上げる"""
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    seq = [
        ("op", OP_TEXT, SPEED_TALK),
        ("title", title, SPEED_TALK),
        ("body", body_text, SPEED_BODY),
        ("ed", ED_TEXT, SPEED_TALK),
    ]
    wavs = []
    for tag, text, speed in seq:
        w = os.path.join(WORK_DIR, f"ep_{tag}.wav")
        ok = synth_long(text, w, speed, tag) if len(text) > CHUNK_LIMIT else synth(text, w, speed)
        if not ok:
            return False
        wavs.append(w)
    lst = os.path.join(WORK_DIR, "ep_list.txt")
    with open(lst, "w") as f:
        for w in wavs:
            f.write(f"file '{w}'\n")
    r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-codec:a", "libmp3lame", "-b:a", "128k", out_mp3],
                       capture_output=True, text=True)
    for w in wavs + [lst]:
        os.remove(w)
    if r.returncode != 0:
        print(f"  ✗ mp3変換失敗: {r.stderr[-200:]}")
        return False
    return True


def notify(msg: str) -> None:
    if not NTFY_TOPIC:
        return
    try:
        import urllib.request
        req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                     data=msg.encode(), method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", name)[:60].strip() or "episode"


def process_notion(dry_run: bool = False) -> int:
    """Notionの「音声化待ち」を処理"""
    from notion_wordpress_uploader import NotionWordPressUploader

    class Reader(NotionWordPressUploader):
        pass

    nw = Reader()
    pages = nw.query_database({"property": "Status(Podcast)", "status": {"equals": "音声化待ち"}})
    print(f"音声化待ち: {len(pages)}件")
    done = 0
    for page in pages:
        page_id = page.get("id")
        db_title = nw.get_property_value(page, "Title") or "タイトルなし"
        print(f"\n=== {db_title[:60]}")

        # Script(Podcast) プロパティ → リンク先Notionページ → 台本
        prop = page.get("properties", {}).get("Script(Podcast)", {})
        script_page_id = None
        if prop.get("type") == "url" and prop.get("url"):
            script_page_id = nw._extract_notion_page_id(prop["url"])
        if not script_page_id:
            print("  ⏭ スキップ: Script(Podcast) にNotionページリンクがありません")
            continue

        title = nw.fetch_page_title(script_page_id) or db_title
        blocks = nw.fetch_page_blocks(script_page_id)
        md = nw.converter.convert(blocks)
        body = md_to_plain(md)
        if len(body) < 30:
            print("  ⏭ スキップ: 台本が短すぎます")
            continue
        if dry_run:
            print(f"  [dry-run] {title[:50]} / 本文{len(body)}字")
            continue

        stamp = datetime.now().strftime("%Y%m%d")
        out_mp3 = os.path.join(OUT_DIR, f"{stamp}_{sanitize(title)}.mp3")
        print(f"  合成開始 → {os.path.basename(out_mp3)}")
        if not build_episode(title, body, out_mp3):
            print("  ✗ エピソード生成失敗")
            notify(f"[音声化失敗] {title[:50]}")
            continue

        dur = subprocess.run(["afinfo", out_mp3], capture_output=True, text=True)
        m = re.search(r"estimated duration: ([\d.]+)", dur.stdout)
        length = f"{float(m.group(1))/60:.1f}分" if m else "?"
        print(f"  ✅ 完成: {out_mp3}（{length}）")

        # Status(Podcast) → 完了（従来の緑さん手作業に相当）
        import requests
        try:
            resp = requests.patch(f"{nw.notion_base}/pages/{page_id}",
                                  headers=nw.notion_headers,
                                  json={"properties": {"Status(Podcast)": {"status": {"name": "完了"}}}},
                                  timeout=30)
            resp.raise_for_status()
            print("  ✅ Notion Status(Podcast) → 完了")
        except Exception as e:
            print(f"  ⚠ ステータス更新失敗: {e}")

        notify(f"[音声化完了] {title[:50]}（{length}）→ Spotifyへアップしてください")
        done += 1
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--text", help="Notionを使わず、テキスト/mdファイルから単発合成（テスト用）")
    ap.add_argument("--title", default="テストエピソード")
    args = ap.parse_args()

    if args.text:
        body = md_to_plain(open(os.path.expanduser(args.text), encoding="utf-8").read())
        out = os.path.join(OUT_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M')}_{sanitize(args.title)}.mp3")
        ok = build_episode(args.title, body, out)
        print(("✅ 完成: " + out) if ok else "✗ 失敗")
        sys.exit(0 if ok else 1)

    n = process_notion(dry_run=args.dry_run)
    print(f"\n処理完了: {n}件")


if __name__ == "__main__":
    main()
