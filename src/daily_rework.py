#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""毎日の自動やり直し（2026-08-21・翔太さん依頼）。

緑さんがGoogle Driveの試聴フォルダ配下「◯回目視聴済_修正点あり」にmp3を入れておくと、
翌朝それを検出してNotionのStatus(Podcast)を「音声化待ち」に戻す。
実際の合成は既存の毎時:15のパイプライン(com.crosshealth.audio-pipeline)が拾う
＝この処理自体は数十秒で終わり、長時間ロックを握らない。

🔴この仕組みで一番危ないのは「空振り」:
   launchd配下からはCloudStorage(Google Drive)の一覧が取れず、
   しかも glob は例外を投げずに **0件を返す**（2026-08-21に実測で確認）。
   素直に組むと毎日「対象なし」と言い続け、ログ上は正常に見える。
   → 走査の前に必ず assert_drive_readable() で読めることを確かめ、
     読めなければ「対象なし」ではなく**失敗として通知**する。

使い方:
  python3 src/daily_rework.py            # 検出→Status戻し→退避→通知
  python3 src/daily_rework.py --dry-run  # 検出だけ（Notion/ファイルに触れない）
"""
import argparse, fcntl, glob, json, os, shutil, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('WORDPRESS_URL', 'https://unused.invalid')
os.environ.setdefault('WORDPRESS_USERNAME', 'u')
os.environ.setdefault('WORDPRESS_APP_PASSWORD', 'p')

DRIVE_AUDITION = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-tekutekuradio@gmail.com/マイドライブ/CrossHealth/Podcast試聴")
WATCH_GLOB = "*修正点あり"          # 1回目/2回目/3回目… を自動で拾う
ARCHIVE_DIR = "_修正済_旧"
STATE = os.path.expanduser("~/health-policy-watcher/output/rework_done.json")


class DriveUnreadable(RuntimeError):
    pass


def assert_drive_readable():
    """Driveを一覧できるか確かめる。できないなら例外＝「対象0件」と区別する。"""
    try:
        os.listdir(DRIVE_AUDITION)
    except Exception as e:
        raise DriveUnreadable(
            f"試聴フォルダを一覧できません: {e!r}\n"
            "→ macOSのTCCでブロックされています。システム設定 → プライバシーとセキュリティ →\n"
            "  フルディスクアクセス に /usr/bin/python3 が入っているか確認してください。\n"
            "  （この状態では「やり直し対象なし」と表示されても、実際は検出できていません）")


def load_state() -> set:
    try:
        return set(json.load(open(STATE, encoding="utf-8")))
    except Exception:
        return set()


def save_state(done: set):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(sorted(done), open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def scan_folders() -> dict:
    """{フォルダパス: [mp3ファイル名]}。退避先(_修正済_旧)は見ない。"""
    found = {}
    for d in sorted(glob.glob(os.path.join(DRIVE_AUDITION, WATCH_GLOB))):
        if not os.path.isdir(d):
            continue
        mp3s = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "*.mp3")))
        if mp3s:
            found[d] = mp3s
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
    print(f"=== 毎日の自動やり直し {stamp} ===")

    from mac_audio_pipeline import send_mail
    try:
        assert_drive_readable()
    except DriveUnreadable as e:
        print(f"🔴 {e}")
        send_mail("【やり直し自動化が停止】試聴フォルダを読めません", str(e))
        sys.exit(1)

    folders = scan_folders()
    total = sum(len(v) for v in folders.values())
    print(f"監視フォルダ: {len(glob.glob(os.path.join(DRIVE_AUDITION, WATCH_GLOB)))}個 / mp3 {total}本")
    if not total:
        print("やり直し依頼なし（フォルダは読めています）。")
        return

    done = load_state()
    targets = {}                       # {mp3名: フォルダ}
    for d, mp3s in folders.items():
        for m in mp3s:
            if m in done:
                print(f"  ⏭ 処理済み台帳にあり（退避漏れ）: {m[:50]}")
                continue
            targets[m] = d
    if not targets:
        print("新規のやり直し依頼なし（すべて処理済み）。")
        return
    print(f"新規のやり直し依頼: {len(targets)}本")

    from rework_flagged_episodes import match_episodes
    from notion_wordpress_uploader import NotionWordPressUploader
    import requests
    nw = NotionWordPressUploader()
    matched, unmatched = match_episodes(nw, sorted(targets))
    # 同じ回が別日付で二重に入っていることが多い(1回目/2回目フォルダ)。
    # match_episodes は1回につき1ファイルしか結び付けないので、残りを「未マッチ」と
    # 呼ぶと毎朝ノイズになる。中身が同じものは重複として仕分ける。
    from rework_flagged_episodes import norm
    matched_titles = {norm(m) for m in matched}
    dupes = [u for u in unmatched if norm(u) in matched_titles]
    unmatched = [u for u in unmatched if norm(u) not in matched_titles]
    print(f"Notion照合: {len(matched)}/{len(targets)}本"
          + (f"（うち重複 {len(dupes)}本は同じ回として1本にまとめ）" if dupes else ""))
    for u in unmatched:
        print(f"  ✗ 未マッチ（手動確認）: {u[:60]}")

    pages = {}
    for mf, (p, t) in matched.items():
        pages.setdefault(p["id"], []).append(mf)
    print(f"対象ユニーク回: {len(pages)}件")

    if args.dry_run:
        print("[dry-run] Notion/ファイルには触れていません。")
        return

    ok = 0
    for pid in pages:
        try:
            requests.patch(f"{nw.notion_base}/pages/{pid}", headers=nw.notion_headers,
                           json={"properties": {"Status(Podcast)": {"status": {"name": "音声化待ち"}}}},
                           timeout=30).raise_for_status()
            ok += 1
        except Exception as e:
            print(f"  NG {pid}: {e}")
    print(f"🔄 Status→音声化待ち: {ok}/{len(pages)}件")

    # 退避（＝二度と回さない）。移動できなくても台帳に載せるので無限ループしない。
    moved = 0
    for mf in list(matched) + dupes:      # 重複ファイルも一緒に片付ける
        d = targets[mf]
        arc = os.path.join(d, ARCHIVE_DIR)
        try:
            os.makedirs(arc, exist_ok=True)
            shutil.move(os.path.join(d, mf), os.path.join(arc, mf))
            moved += 1
        except Exception as e:
            print(f"  ⚠ 退避できず（台帳で二重処理は防止）: {mf[:40]} {e}")
        done.add(mf)
    save_state(done)
    print(f"🗄 退避: {moved}/{len(matched) + len(dupes)}本 → {ARCHIVE_DIR}/")

    body = (f"やり直し依頼 {len(targets)}本を検出し、{ok}回分を「音声化待ち」に戻しました。\n"
            f"合成は次の毎時15分のパイプラインが自動で行い、終わり次第いつもの試聴依頼メールが届きます。\n\n"
            + "".join(f"・{m}\n" for m in sorted(matched))
            + (("\n未マッチ（タイトル差・手動確認）:\n" + "".join(f"・{u}\n" for u in unmatched)) if unmatched else ""))
    send_mail(f"【やり直し受付】{len(pages)}回を再生成キューに入れました", body)
    print("✉️ 通知メール送信")


if __name__ == "__main__":
    lock = open("/tmp/crosshealth_daily_rework.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("別の実行が進行中のためスキップ")
        sys.exit(0)
    main()
