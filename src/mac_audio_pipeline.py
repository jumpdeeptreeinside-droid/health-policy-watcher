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

# CLIは日本語ナレーター名を受け付けない(iconv問題)ため英語名で指定する
NARRATOR = "Miyamai Moca"
PITCH = "-35"          # GUIの-0.35に相当（セント）
SPEED_TALK = "100"     # OP/タイトル/ED
SPEED_BODY = "120"     # 本文（現行.vppの実測値）
CHUNK_LIMIT = 120      # CLIの1回あたり文字数上限（140の安全側）
SYNTH_TIMEOUT = 180    # 1チャンクの合成タイムアウト（秒）

DRIVE_AUDITION = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-tekutekuradio@gmail.com/マイドライブ/CrossHealth/Podcast試聴")

# 読みの修正ルール（正規表現, 置換）。誤読が見つかったらここに追記（2026-07-07 木内さん指摘: 数字+人=にん）
READING_FIXES = [
    (r"([0-9０-９]+)人", r"\1にん"),
]


def apply_reading_fixes(text: str) -> str:
    for pat, rep in READING_FIXES:
        text = re.sub(pat, rep, text)
    return text


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
    text = apply_reading_fixes(text)
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
    # 提供ジングル（サクの提供コール+BGM birds_01・固定アセット 2026-07-07 木内さん発案）
    sponsor = os.path.expanduser("~/obsidian-brain/_podcast/assets/sponsor.wav")
    if os.path.exists(sponsor):
        wavs.append(sponsor)
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
        if not w.endswith("assets/sponsor.wav"):
            os.remove(w)
    if r.returncode != 0:
        print(f"  ✗ mp3変換失敗: {r.stderr[-200:]}")
        return False
    return True


SITE_REPO = os.path.expanduser("~/crosshealthjp")


def publish_episode(mp3_path: str, title: str, description: str = "") -> str:
    """完成mp3を公式サイトのPodcastフィードに公開する（Spotify手動アップの置き換え）。
    mp3を public/podcast/audio/ へ配置 → episodes.json に追記 → feed.xml 再生成 → git push。
    Apple/Spotify等はフィード更新を自動で拾う。戻り値=公開URL（失敗時は空文字）"""
    import shutil
    from email.utils import format_datetime
    from datetime import timezone

    audio_dir = os.path.join(SITE_REPO, "public/podcast/audio")
    os.makedirs(audio_dir, exist_ok=True)
    fname = os.path.basename(mp3_path)
    dst = os.path.join(audio_dir, fname)
    shutil.copy(mp3_path, dst)

    # duration（秒）とバイト数
    dur = ""
    pr = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                         "-of", "csv=p=0", dst], capture_output=True, text=True)
    if pr.returncode == 0 and pr.stdout.strip():
        total = int(float(pr.stdout.strip()))
        dur = f"{total // 60}:{total % 60:02d}"

    url = f"https://www.crosshealthjp.org/podcast/audio/{fname}"
    ep = {
        "title": title,
        "pubDate": format_datetime(datetime.now(timezone.utc)),
        "guid": f"chj-{fname}",
        "url": url,
        "length": str(os.path.getsize(dst)),
        "duration": dur,
        "description": description or title,
    }
    manifest = os.path.join(SITE_REPO, "public/podcast/episodes.json")
    eps = json.load(open(manifest, encoding="utf-8"))
    eps.insert(0, ep)
    json.dump(eps, open(manifest, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    r = subprocess.run(["python3", "scripts/build_podcast_feed.py"], cwd=SITE_REPO,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ✗ feed生成失敗: {r.stderr[-200:]}")
        return ""

    for cmd in (["git", "pull", "--rebase", "--quiet"],
                ["git", "add", "public/podcast"],
                ["git", "commit", "-q", "-m", f"podcast: {title[:50]}"],
                ["git", "push", "-q"]):
        r = subprocess.run(cmd, cwd=SITE_REPO, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ✗ {' '.join(cmd[:2])} 失敗: {r.stderr[-200:]}")
            return ""
    print(f"  ✅ Podcast公開: {url}（数分でフィード反映→各プラットフォームが自動取得）")
    return url


DIC_PATH = os.path.expanduser(
    "~/Library/Application Support/Dreamtonics/Voicepeak/settings/dic.json")
QC_MODEL = "gemini-flash-latest"


def qc_audio(mp3_path: str, script_text: str) -> dict:
    """AI検品（2026-07-06 木内さん発案）: 生成音声をGeminiに聴かせ、台本と突き合わせる。
    戻り値: {"ok": bool, "issues": [{"word","heard","reading"}], "note": str}
    検出対象=明確な誤読・脱落・数字の読み崩れ。アクセントの好みは対象外（人の耳の仕事）。"""
    try:
        import google.generativeai as genai
        import config
        genai.configure(api_key=config.GEMINI_API_KEY)
        audio = genai.upload_file(mp3_path)
        prompt = (
            "あなたは日本語ポッドキャストの検品担当です。添付音声を聴き、以下の台本と突き合わせてください。\n"
            "指摘するのは【明確な読み間違い】【文の脱落・途切れ】【数字や英略語の読み崩れ】のみ。\n"
            "イントネーションや間の好みは指摘しないでください。\n\n"
            "JSONのみで回答:\n"
            '{"ok": true/false, "issues": [{"word": "台本上の表記", "heard": "実際に聞こえた読み", '
            '"reading": "正しい読み（カタカナ）"}], "note": "一言メモ"}\n\n'
            f"# 台本\n{script_text[:8000]}"
        )
        model = genai.GenerativeModel(QC_MODEL)
        resp = model.generate_content([prompt, audio])
        genai.delete_file(audio.name)
        raw = resp.text.strip().strip("`").removeprefix("json").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  ⚠ AI検品エラー（検品スキップ・人のチェックへ）: {e}")
        return {"ok": True, "issues": [], "note": f"検品スキップ: {e}"}


def add_dictionary_entries(issues: list) -> int:
    """誤読語をVOICEPEAKユーザー辞書へ自動登録（既存エントリは上書きしない）"""
    try:
        d = json.load(open(DIC_PATH, encoding="utf-8"))
    except Exception:
        return 0
    added = 0
    for i in issues:
        word, reading = i.get("word", ""), i.get("reading", "")
        if not word or not reading or len(word) > 20:
            continue
        if any(e.get("sur") == word for e in d):
            continue
        kata = re.sub(r"[^ァ-ヶー]", "", reading)
        if not kata:
            continue
        d.append({"sur": word, "pron": kata, "pos": "Japanese_Futsuu_meishi",
                  "priority": 9, "accentType": 0})
        added += 1
        print(f"  📖 辞書自動登録: {word} → {kata}")
    if added:
        json.dump(d, open(DIC_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return added


def send_mail(subject: str, body: str) -> None:
    """完成/公開の通知メール（config.pyのGMAIL設定があれば送信・無ければスキップ）"""
    try:
        import config
        addr = getattr(config, "GMAIL_ADDRESS", "")
        pw = getattr(config, "GMAIL_APP_PASSWORD", "")
    except ImportError:
        addr = pw = ""
    if not addr or not pw:
        print(f"  （メール未設定のため通知スキップ: {subject}）")
        return
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = "jump.deep.tree.inside@gmail.com"
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(addr, pw)
            srv.send_message(msg)
    except Exception as e:
        print(f"  ⚠ メール送信失敗: {e}")


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

        # 国際系は個別音声化しない＝毎日19:05の「海外ヘッドライン」まとめに束ねる（2026-07-07）
        cat_prop = page.get("properties", {}).get("Category", {}).get("select") or {}
        if cat_prop.get("name") in ("国際・日本関連", "国際・その他"):
            print(f"  ⏭ 国際系→夕方のまとめ対象（個別音声化しない）")
            continue

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
            send_mail(f"【音声化失敗】{title[:50]}", "エピソード生成に失敗しました。ログを確認してください。")
            continue

        # ── AI検品（誤読検出→辞書自動登録→1回だけ再合成） ──
        qc = qc_audio(out_mp3, f"{OP_TEXT}\n{title}\n{body}\n{ED_TEXT}")
        qc_note = qc.get("note", "")
        if not qc.get("ok", True) and qc.get("issues"):
            print(f"  🔍 AI検品: 指摘{len(qc['issues'])}件 → 辞書登録して再合成")
            if add_dictionary_entries(qc["issues"]):
                if build_episode(title, body, out_mp3):
                    qc2 = qc_audio(out_mp3, f"{OP_TEXT}\n{title}\n{body}\n{ED_TEXT}")
                    qc_note = f"自動修正{len(qc['issues'])}件→再検品: {qc2.get('note','')}"
                else:
                    qc_note = "再合成失敗（初版を試聴へ）"
        else:
            print(f"  🔍 AI検品: 合格（{qc_note}）")

        dur = subprocess.run(["afinfo", out_mp3], capture_output=True, text=True)
        m = re.search(r"estimated duration: ([\d.]+)", dur.stdout)
        length = f"{float(m.group(1))/60:.1f}分" if m else "?"
        print(f"  ✅ 完成: {out_mp3}（{length}）")

        # Google Drive試聴フォルダへコピー（スマホから試聴できる）
        try:
            import shutil
            os.makedirs(DRIVE_AUDITION, exist_ok=True)
            shutil.copy(out_mp3, os.path.join(DRIVE_AUDITION, os.path.basename(out_mp3)))
            print("  ☁️ Drive試聴フォルダへコピー")
        except Exception as e:
            print(f"  ⚠ Driveコピー失敗: {e}")

        # 公開はしない＝翔太さんの試聴チェックを挟む（二段階制 2026-07-06）
        # mp3パスをNotionに記録し、Status(Podcast)を「試聴待ち」へ。
        # 試聴してOKなら手動で「公開待ち」に変更→次回実行時に自動公開。
        import requests as _rq2
        try:
            _rq2.patch(f"{nw.notion_base}/pages/{page_id}",
                       headers=nw.notion_headers,
                       json={"properties": {"AudioPath": {"rich_text": [{"text": {"content": out_mp3}}]}}},
                       timeout=30).raise_for_status()
        except Exception as e:
            print(f"  ⚠ AudioPath記録失敗: {e}")

        import requests
        try:
            resp = requests.patch(f"{nw.notion_base}/pages/{page_id}",
                                  headers=nw.notion_headers,
                                  json={"properties": {"Status(Podcast)": {"status": {"name": "試聴待ち"}}}},
                                  timeout=30)
            resp.raise_for_status()
            print("  ✅ Notion Status(Podcast) → 試聴待ち")
        except Exception as e:
            print(f"  ⚠ ステータス更新失敗: {e}（Notion側に「試聴待ち」オプションが必要）")

        send_mail(f"【試聴依頼】{title[:50]}（{length}）",
                  f"音声が完成しました。試聴してください。\n\n"
                  f"スマホ: Google Drive → CrossHealth → Podcast試聴\n"
                  f"Mac: {out_mp3}\n"
                  f"AI検品: {qc_note or '合格'}\n\n"
                  f"OKなら Notion の Status(Podcast) を「公開待ち」に変更\n"
                  f"→ 次の定時実行（毎時）で自動的にPodcastフィードへ公開されます。")
        done += 1

    # ── 第2段階: 「公開待ち」→ フィード公開 ─────────────
    pages2 = nw.query_database({"property": "Status(Podcast)", "status": {"equals": "公開待ち"}})
    print(f"\n公開待ち: {len(pages2)}件")
    for page in pages2:
        page_id = page.get("id")
        title = nw.get_property_value(page, "Title") or "タイトルなし"
        audio = nw.get_property_value(page, "AudioPath") or ""
        if not audio or not os.path.exists(audio):
            print(f"  ⏭ スキップ({title[:40]}): AudioPathのmp3が見つかりません: {audio}")
            continue
        if dry_run:
            print(f"  [dry-run] 公開予定: {title[:50]}")
            continue
        desc = nw.get_property_value(page, "PodcastDescription") or ""
        url = publish_episode(audio, title, desc)
        if not url:
            continue
        import requests
        try:
            requests.patch(f"{nw.notion_base}/pages/{page_id}",
                           headers=nw.notion_headers,
                           json={"properties": {"Status(Podcast)": {"status": {"name": "完了"}}}},
                           timeout=30).raise_for_status()
            print("  ✅ Notion Status(Podcast) → 完了")
        except Exception as e:
            print(f"  ⚠ ステータス更新失敗: {e}")
        send_mail(f"【公開完了】{title[:50]}", f"Podcastフィードに公開しました。\n{url}\n各プラットフォームには数時間内に反映されます。")
        done += 1
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--text", help="Notionを使わず、テキスト/mdファイルから単発合成（テスト用）")
    ap.add_argument("--title", default="テストエピソード")
    ap.add_argument("--publish", action="store_true", help="--textの結果をPodcastフィードに公開まで行う")
    args = ap.parse_args()

    if args.text:
        body = md_to_plain(open(os.path.expanduser(args.text), encoding="utf-8").read())
        out = os.path.join(OUT_DIR, f"{datetime.now().strftime('%Y%m%d_%H%M')}_{sanitize(args.title)}.mp3")
        ok = build_episode(args.title, body, out)
        print(("✅ 完成: " + out) if ok else "✗ 失敗")
        if ok and args.publish:
            publish_episode(out, args.title)
        sys.exit(0 if ok else 1)

    n = process_notion(dry_run=args.dry_run)
    print(f"\n処理完了: {n}件")


if __name__ == "__main__":
    main()
