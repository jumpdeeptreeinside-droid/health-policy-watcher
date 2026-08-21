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
import fcntl
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

# 読みの修正ルール（正規表現, 置換）。
# 旧: (r"([0-9０-９]+)人", r"\1にん") … 2026-07-07に「〇〇人→ひと」誤読の対策として入れたが、
# 数字をかなに開くのをやめた今は「1人」が「いちにん」になってしまう（正しくは「ひとり」）。
# 音声長で実測するとVOICEPEAKは 1人=ひとり と自前で正しく読むので削除した（2026-08-21）。
READING_FIXES = []


# 緑さんの「読み間違いメモ」スプレッドシート（Drive・毎回の生成で最新を自動取得）
# 翔太さんがシートを「リンクを知る全員：閲覧可」にすると export?format=csv が認証なしで取れる。
READING_SHEET_ID = "1adDCmMJ3bgFZHAOJjGzkNuRDPlZDqLtvD-APsZjl9ic"
READING_SHEET_CSV = f"https://docs.google.com/spreadsheets/d/{READING_SHEET_ID}/export?format=csv"
_OVERRIDES_CACHE = os.path.expanduser("~/health-policy-watcher/output/reading_overrides.json")
_READING_OVERRIDES = {}  # {誤読語: 正しい読み(かな)} 生成開始時に load_reading_overrides() で満たす


# シートの語を素のreplaceで当てるのは危険だった（2026-08-21・2回目の試聴やり直しの主因）。
#   「は」→「わ」  … 文中の全ての は を置換 → はち→わち / はじめに→わじめに / 半月板→わんげつばん
#   「有」→「ゆうしせたい」… 有料老人ホーム→ゆうしせたい料老人ホーム
#   「8年」→「はちねん」… 18年→1はちねん（数字の途中で当たる）
# ＝1文字語・数字を含む語・読みが極端に長い語は置換に使わず、コード側(yomi_preprocess)で扱う。
_KANA1 = set("あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん"
             "がぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽぁぃぅぇぉっゃゅょー")


def vet_overrides(pairs: dict):
    """単純置換して安全な指摘だけを残す。危険なものは理由付きで弾く（＝黙って通さない）。"""
    from yomi_preprocess import HARDWORDS
    safe, rejected = {}, []
    for w, r in pairs.items():
        if w in HARDWORDS or w.strip("〇～〜") in HARDWORDS:
            rejected.append((w, "コード側(yomi_preprocess.HARDWORDS)で対応済み"))
        elif len(w) == 1 and (w in _KANA1 or w.isdigit()):
            rejected.append((w, "1文字の かな/数字 は文中の別語まで壊す（例 は→わ で はち→わち）"))
        elif any(c.isdigit() for c in w):
            rejected.append((w, "数字を含む語は桁の途中で当たる（例 8年→はちねん が 18年→1はちねん）"))
        elif len(w) == 1:
            # 円→えん とすると「円滑」が「えん滑」に、園→えん で「公園」が「公えん」になる。
            # 1文字語は助数詞(N床/N円)か難読語としてコード側で扱う。
            rejected.append((w, "1文字の語は複合語を壊す（円滑→えん滑）。助数詞/難読語としてコード側で対応"))
        elif "〇" in w or not w.strip("～〜"):
            rejected.append((w, "〇〇 や ～ を含む雛形はそのままでは一致しない"))
        else:
            safe[w.strip("～〜")] = r.strip("～〜")
    return safe, rejected


def load_reading_overrides() -> dict:
    """緑さんのシートから 語→読み を取得。失敗時はキャッシュ→空でフェイルセーフ（生成は止めない）。"""
    import csv, io
    pairs = {}
    try:
        cp = subprocess.run(["curl", "-sL", "--max-time", "30", READING_SHEET_CSV],
                            capture_output=True, text=True, timeout=40)
        text = cp.stdout
        if not text or "<html" in text[:200].lower():
            raise RuntimeError("CSV取得失敗（HTML応答＝未公開の可能性）")
        rows = list(csv.reader(io.StringIO(text)))
        for row in rows[1:]:  # 1行目=ヘッダ
            if len(row) < 4:
                continue
            date, word, reading = (row[0] or ""), row[2].strip(), row[3].strip()
            if not word or not reading:
                continue
            if "（例）" in date or "例）" in date:  # 見本行はスキップ
                continue
            pairs[word] = reading
        pairs, rejected = vet_overrides(pairs)
        json.dump(pairs, open(_OVERRIDES_CACHE, "w"), ensure_ascii=False, indent=1)
        print(f"  📖 読み間違いメモ: {len(pairs)}語を反映（緑さんシート）")
        if rejected:
            print(f"  ⚠ 単純置換に使えない指摘{len(rejected)}件＝コード側(yomi_preprocess)で対応する語:")
            for w, why in rejected:
                print(f"      {w}: {why}")
    except Exception as e:
        try:
            pairs = json.load(open(_OVERRIDES_CACHE))
            print(f"  📖 読み間違いメモ: シート取得失敗→キャッシュ{len(pairs)}語で継続（{e}）")
        except Exception:
            print(f"  📖 読み間違いメモ: 取得もキャッシュも無し・組込辞書のみで継続（{e}）")
    return pairs


def apply_reading_fixes(text: str) -> str:
    # 数字・日付・頻出略語を決定論的に読みへ変換（2026-07-10・A/B/C検証で抑揚劣化なしを確認）。
    # 英語の固有名詞は原稿生成側で日本語化済み想定（notion_content_generatorのプロンプト）。
    try:
        from yomi_preprocess import to_yomi
        text = to_yomi(text)
    except Exception:
        pass
    for pat, rep in READING_FIXES:
        text = re.sub(pat, rep, text)
    # 緑さんの読み間違いメモを最終適用（語を正しい読みへ単純置換・長い語優先）
    for word in sorted(_READING_OVERRIDES, key=len, reverse=True):
        text = text.replace(word, _READING_OVERRIDES[word])
    # 読みに落ちなかった英字語＝台本側で直すべき箇所。黙って素読みさせず必ず出す。
    try:
        from yomi_preprocess import residual_latin
        left = residual_latin(text)
        if left:
            print(f"  ⚠ 読みが未定義の英字語{len(left)}件（素読みされます・台本側で日本語化を）: {', '.join(left[:15])}")
    except Exception:
        pass
    return text


OP_TEXT = "こんにちは。メインパーソナリティーの、ばんじょうサクです。"
ED_TEXT = (
    "ニュースは以上となります。"
    "お相手はメインパーソナリティーのばんじょうさくでした。"
    "難解な医療政策を、もっと身近に。もっと手軽に。医療政策ウォッチャー。"
)


# 台本ページに人が書き込んだレビューメモ（例:「（以下、音声内にこのスクリプトに記載のない文章がある）」）。
# 読み上げ対象ではないのに素通りして音声に乗っていた（2026-08-04・療養病床の回が2回やり直しになった一因）。
REVIEW_NOTE = re.compile(r"^[（(].*(音声|スクリプト|台本|収録|修正|確認|要チェック|※).*[)）]$")


def md_to_plain(md: str) -> str:
    """台本Markdown→読み上げテキスト（見出し記号・装飾・URL・レビューメモを除去）"""
    kept = []
    for line in md.split("\n"):
        if REVIEW_NOTE.match(line.strip()):
            print(f"  ✂ レビューメモを除去（読み上げない）: {line.strip()[:50]}")
            continue
        kept.append(line)
    md = "\n".join(kept)
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


def wav_seconds(path: str) -> float:
    try:
        import wave
        with wave.open(path) as f:
            return f.getnframes() / float(f.getframerate())
    except Exception:
        return 0.0


def synth(text: str, out_wav: str, speed: str) -> bool:
    """VOICEPEAK CLIで1チャンク合成（タイムアウト・1回リトライ付き）。
    ※読みの変換(apply_reading_fixes)はチャンク分割前に呼び出し側で済ませる（分割長の計算を正しくするため）。"""
    cmd = [VOICEPEAK, "-s", text, "--narrator", NARRATOR,
           "--speed", speed, "--pitch", PITCH, "-o", out_wav]
    # VOICEPEAK CLIは呼び出し元の環境変数(DYLD/locale)干渉でiconv_open失敗することがある。
    # HOME/PATHのみのクリーン環境で起動して安定化（本番launchdの最小環境と同等・無影響）。
    _clean_env = {"HOME": os.path.expanduser("~"), "PATH": "/opt/homebrew/bin:/usr/bin:/bin"}
    # 前回の試行が残した中途半端なwavを「成功」と誤認しないよう、毎回消してから合成する。
    # タイムアウトで殺されたVOICEPEAKが書きかけのwavを残し、次の試行が失敗しても
    # サイズ判定だけで True を返していた＝本文が一文まるごと音声から欠落していた（2026-08-21）。
    min_sec = 0.08 * len(text)          # 実測0.20秒/字。半分以下なら途中で切れている。
    for attempt in (1, 2, 3):
        if os.path.exists(out_wav):
            os.remove(out_wav)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=SYNTH_TIMEOUT, env=_clean_env)
            if r.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 1000:
                sec = wav_seconds(out_wav)
                if sec >= min_sec:
                    return True
                print(f"  ⚠ 音声が短すぎる (試行{attempt}): {sec:.1f}秒 < 期待{min_sec:.1f}秒 → 再合成")
            else:
                print(f"  ⚠ 合成出力なし (試行{attempt}): rc={r.returncode} {r.stderr[-100:] if r.stderr else ''}")
        except subprocess.TimeoutExpired:
            print(f"  ⚠ 合成タイムアウト (試行{attempt})")
        time.sleep(3 * attempt)
    return False


def synth_long(text: str, out_wav: str, speed: str, tag: str) -> bool:
    """長文をチャンク合成して連結"""
    os.makedirs(WORK_DIR, exist_ok=True)
    text = apply_reading_fixes(text)  # 数字・読みを先に展開→その長さで分割
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
        text_expanded = apply_reading_fixes(text)  # 展開後の長さで分岐判定
        ok = (synth_long(text, w, speed, tag) if len(text_expanded) > CHUNK_LIMIT
              else synth(text_expanded, w, speed))
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

    # 先にrepoを最新化（ファイルを変更した後にpullすると詰まるため・2026-07-07修正）
    r = subprocess.run(["git", "pull", "--rebase", "--autostash", "--quiet"],
                       cwd=SITE_REPO, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ✗ git pull 失敗: {r.stderr[-200:]}")
        return ""

    audio_dir = os.path.join(SITE_REPO, "public/podcast/audio")
    os.makedirs(audio_dir, exist_ok=True)
    # 公開URLはASCII安全なスラッグに（日本語ファイル名はApple等で取得失敗し得る）
    import hashlib
    stem = datetime.now(timezone.utc).strftime("%Y%m%d") + "_" + hashlib.md5(title.encode()).hexdigest()[:8]
    fname = f"{stem}.mp3"
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
    # 同タイトルの既存エントリはリトライ由来の重複として置き換える
    eps = [e for e in eps if e.get("title") != title]
    eps.insert(0, ep)
    json.dump(eps, open(manifest, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    r = subprocess.run(["python3", "scripts/build_podcast_feed.py"], cwd=SITE_REPO,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ✗ feed生成失敗: {r.stderr[-200:]}")
        return ""

    for cmd in (["git", "add", "public/podcast"],
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


QC_TRANSCRIPTS = os.path.expanduser("~/health-policy-watcher/output/qc_transcripts")


def qc_episode(mp3_path: str, spoken_text: str, source_text: str, title: str) -> dict:
    """検品（2026-08-21にWhisper方式へ切替）。

    旧: Geminiにmp3を聴かせる（qc_audio）。数字の聞き逃しが多く、指摘の質が安定しなかった。
    新: ローカルWhisperが「耳」、数値照合は決定論のPython、表現ゆらぎだけGeminiのテキスト比較。

    数値の照合は「実際に読み上げたテキスト(spoken_text)」と突き合わせる。
    台本(source_text)と比べると、前処理で意図的に直した表記（1012.2千人→101万2200人）が
    毎回“欠落”として出てしまうため。前処理による表記変更は別枠で報告する。

    🔴number_mismatchesは「誤読」ではなく「要確認」。今日の実測では、指摘された数値を
    単文で合成し直すと全て正しく読めており（100668施設→10万668 等）、Whisperの
    書き起こし癖だった。ここを誤読として辞書に流すと雑エントリの温床になるので流さない。
    """
    try:
        from whisper_qc import qc as _wqc, extract_numbers
    except Exception as e:
        print(f"  ⚠ Whisper検品を読み込めずGemini方式にフォールバック: {e}")
        return qc_audio(mp3_path, source_text)
    try:
        r = _wqc(mp3_path, spoken_text)
    except Exception as e:
        print(f"  ⚠ Whisper検品エラーのためGemini方式にフォールバック: {e}")
        return qc_audio(mp3_path, source_text)

    # 書き起こしを残す（あとで単文検証するときの元データ）
    try:
        os.makedirs(QC_TRANSCRIPTS, exist_ok=True)
        fn = f"{datetime.now():%Y%m%d_%H%M}_{sanitize(title)}.txt"
        open(os.path.join(QC_TRANSCRIPTS, fn), "w", encoding="utf-8").write(r.get("transcript", ""))
    except Exception:
        pass

    # 前処理で表記が変わった数値（意図した変換のはず）を分けて出す
    src_n, spk_n = extract_numbers(source_text), extract_numbers(spoken_text)
    pool = list(spk_n)
    converted = []
    for v in src_n:
        if v in pool:
            pool.remove(v)
        else:
            converted.append(v)
    r["converted_numbers"] = converted
    return r


PENDING_DICT = os.path.expanduser("~/health-policy-watcher/output/dict_pending.json")

# ユーザー辞書に入れてよい語の条件（2026-08-04）。自動登録が壊した実例:
#   1文字 `日→ニチ` `人→ヒト` … 日本人→ニホンジン が登録済みでも「にちほんにん」に分断された
#   文節  `抗菌薬の` `平均在院日数は91.7日で` … そこで区切り・間・アクセントが消える
#   数字  `2026年7月30日` … 合成前に yomi_preprocess がかな化するので発火すらしない死蔵
_NG_TAIL = r"(の|は|が|を|に|へ|と|や|も|から|まで|より|など|ため|こと|って|ます|です|ください)$"


def dict_entry_ok(word: str, reading: str) -> tuple:
    """(可否, 理由)。人が承認する前の足切り。"""
    if not word or not reading:
        return False, "語または読みが空"
    if len(word) < 2:
        return False, "1文字（複合語を分断する）"
    if len(word) > 12:
        return False, "長すぎ（句・文の登録）"
    if re.search(r"[0-9０-９]", word):
        return False, "数字を含む（合成前にかな化され発火しない）"
    if re.search(_NG_TAIL, word):
        return False, "助詞・活用形で終わる（文節の登録）"
    if not re.sub(r"[^ァ-ヶー]", "", reading):
        return False, "読みがカタカナでない"
    return True, ""


def queue_dictionary_entries(issues: list, title: str = "") -> int:
    """検品が拾った誤読を『承認待ち』として貯める。辞書には書き込まない（2026-08-04）。

    以前はここで dic.json へ直接 priority=9 / accentType=0 で自動登録していたが、
    検品の合格率が5/78＝ほぼ常に何かを指摘する状態で、雑なエントリが積み上がり
    抑揚と複合語を壊していた。人が承認したものだけを入れる運用に変える。
    """
    try:
        pending = json.load(open(PENDING_DICT, encoding="utf-8"))
    except Exception:
        pending = []
    known = {(p.get("word"), p.get("reading")) for p in pending}
    try:
        existing = {e.get("sur") for e in json.load(open(DIC_PATH, encoding="utf-8"))}
    except Exception:
        existing = set()

    added = 0
    for i in issues:
        word = (i.get("word") or "").strip()
        reading = (i.get("reading") or "").strip()
        if not word or not reading:
            continue            # 検品が語を特定できなかった指摘（空行を貯めない）
        if (word, reading) in known or word in existing:
            continue
        ok, why = dict_entry_ok(word, reading)
        pending.append({"date": datetime.now().strftime("%Y-%m-%d"), "episode": title[:60],
                        "word": word, "reading": reading, "heard": i.get("heard", ""),
                        "登録可": ok, "却下理由": why, "承認": ""})
        known.add((word, reading))
        added += 1
        print(f"  📝 承認待ちへ: {word} → {reading}" + ("" if ok else f"（要注意: {why}）"))
    if added:
        os.makedirs(os.path.dirname(PENDING_DICT), exist_ok=True)
        json.dump(pending, open(PENDING_DICT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
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
    global _READING_OVERRIDES
    _READING_OVERRIDES = load_reading_overrides()  # 毎回の生成で緑さんシートの最新を取得
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

        # Podcastのタイトルは記事タイトル(Article＆Script Title)を使う。
        # 台本ページのタイトルは原資料名（「令和７(2025)年簡易生命表の概況」等）になっており、
        # 番組名として配信されてしまっていた（翔太さん指摘 2026-08-21）。
        title = (nw.get_property_value(page, "Article＆Script Title")
                 or nw.fetch_page_title(script_page_id) or db_title)
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
        # AI検品は「報告」だけにする（2026-08-04・翔太さん合意）。
        # 自動辞書登録＋再合成は廃止＝雑エントリで抑揚と複合語を壊していたため。
        # 指摘は output/dict_pending.json に貯め、人が承認したものだけ辞書へ入れる。
        source_text = f"{OP_TEXT}\n{title}\n{body}\n{ED_TEXT}"
        spoken_text = apply_reading_fixes(source_text)
        qc = qc_episode(out_mp3, spoken_text, source_text, title)
        qc_note = qc.get("note", "")
        miss = qc.get("number_mismatches", [])
        conv = qc.get("converted_numbers", [])
        issues = qc.get("issues", [])
        if conv:
            print(f"  ↩ 前処理で表記変換: {len(conv)}件 {conv[:6]}")
        if miss:
            # 誤読とは限らない（Whisperの書き起こし癖が多い）。人の耳で確かめる材料として出す。
            print(f"  🔍 検品: 聞き取れなかった数値{len(miss)}件（要確認・誤読とは限らない）: {miss[:8]}")
        if issues:
            n = queue_dictionary_entries(issues, title)
            print(f"  🔍 検品: 指摘{len(issues)}件 → 承認待ちに{n}件（辞書は変更せず）")
        if not miss and not issues:
            print(f"  🔍 検品: 合格（{qc_note}）")
        else:
            qc_note = f"数値要確認{len(miss)}件 / 指摘{len(issues)}件: {qc_note}"

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
        # launchd（バックグラウンド）は Google Drive(CloudStorage) を読めない（macOS TCC）。
        # AudioPath が Drive 等を指していても、ローカル正本(OUT_DIR)に同名mp3があればそちらを使う。
        # （2026-07-15: これが無いと自動公開が PermissionError で毎回全滅していた）
        if audio:
            local = os.path.join(OUT_DIR, os.path.basename(audio))
            if os.path.exists(local):
                audio = local
        if not audio or not os.path.exists(audio):
            print(f"  ⏭ スキップ({title[:40]}): mp3が見つかりません: {audio}")
            continue
        if dry_run:
            print(f"  [dry-run] 公開予定: {title[:50]}")
            continue
        # 1件の失敗で全体を止めない（各回を try/except で独立させる・2026-07-15）
        try:
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
            # 公開成功 → ステージング(ローカル正本＋Drive試聴)を自動削除。
            # 恒久保存はフィード側(crosshealthjp/public/podcast/audio・git管理)にあるため不要（2026-07-15）。
            # ※launchdはDriveを消せない(権限)ので、Drive分はbest-effort（失敗しても無視）。
            for stale in (audio, os.path.join(DRIVE_AUDITION, os.path.basename(audio))):
                try:
                    if stale and os.path.exists(stale):
                        os.remove(stale)
                        print(f"  🗑 ステージング削除: {os.path.basename(stale)}")
                except Exception as e:
                    print(f"  ⚠ 削除スキップ({os.path.basename(stale)}): {e}")
        except Exception as e:
            print(f"  ✗ 公開失敗({title[:40]}): {e}")
            continue
    return done


def main():
    # 多重起動ガード：毎時実行と長時間合成の重なりで同じ記事を二重処理しない（2026-07-07）
    _lock = open("/tmp/crosshealth_audio_pipeline.lock", "w")
    try:
        fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("別のパイプラインが実行中のためスキップ")
        sys.exit(0)

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
