#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mac の launchd から、決まった時刻に GitHub Actions を workflow_dispatch で起こす（2026-11-20）。

なぜ：GitHub の schedule（cron）は、このリポジトリでは実測で5〜10時間遅れて起動していた
（site_uploader の予定 9:00/14:00/19:30 JST、daily_digest の予定 19:05 JST）。
手動起動（workflow_dispatch）はすぐに動く。cron は予備として残す。
二重に走っても壊れないことは workflow 側の concurrency と各スクリプトで担保している
（site_uploader＝「投稿待ち」だけを拾い push 後に「完了」・公開済みの印があれば書き戻しだけ、
  daily_digest＝直近の JST 12:00 以降に作っていれば作らない・同じ日付の題は「その2」）。
dispatch の失敗（gh のログイン切れ・timeout 等）はログに ❌❌ DISPATCH FAILED と残し、
既存の ntfy（mac_audio_pipeline._ntfy）に workflow 名と種類だけを1行で知らせる。

使い方:
  python3 src/mac_dispatch.py site_uploader
  python3 src/mac_dispatch.py daily_digest          # 送る前に状態進めを1回（国際系を音声化待ちに）
  python3 src/mac_dispatch.py daily_digest --dry-run # 送らない（状態進めも dry-run）
  python3 src/mac_dispatch.py --self-test

🔴 鍵はこのファイルにも plist にも置かない。gh CLI の既存のログイン（gh 自身の保管）をそのまま使う。
🔴 gh には必ず timeout（subprocess の timeout）。固まって launchd の次の起動を止めない。
🔴 失敗しても他のジョブには触らない（このプロセスが exit 1 で終わるだけ）。CloudStorage は読み書きしない。
"""
import os
import shutil
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = "jumpdeeptreeinside-droid/health-policy-watcher"
REF = "master"
GH_TIMEOUT = 60            # 秒
WORKFLOWS = {              # 名前 → workflow ファイル（ここに無い名前は送らない）
    "site_uploader": "site_uploader.yml",
    "daily_digest": "daily_digest.yml",
}


def gh_path() -> str:
    return (shutil.which("gh")
            or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), ""))


def dispatch_cmd(name: str, gh: str = "gh") -> list:
    if name not in WORKFLOWS:
        raise ValueError(f"知らない workflow です: {name!r}（{sorted(WORKFLOWS)} のどれか）")
    return [gh, "workflow", "run", WORKFLOWS[name], "-R", REPO, "--ref", REF]


def advance_before_digest(dry_run: bool) -> None:
    """daily_digest の前に状態進めを1回（その日の国際系を「音声化待ち」にしてからまとめる）。
    失敗しても dispatch は送る（予定どおりまとめを作る方を優先）。"""
    try:
        import logging
        import notion_status_automation as nsa
        lg = nsa.logger
        if not lg.handlers:
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(logging.Formatter("  %(message)s"))
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
            lg.propagate = False
        r = nsa.NotionAutomation().process_content_complete_status(dry_run=dry_run)
        print(f"状態進め{'[dry-run]' if dry_run else ''}: 候補{r['found']}件 / "
              f"{'進めるはず' + str(r['planned']) if dry_run else '進めた' + str(r['advanced'])}件"
              f" / 失敗{r['failed']}件")
    except (Exception, SystemExit) as e:
        print(f"  ⚠ 状態進めが失敗（dispatch は送る）: {type(e).__name__}: {e}")


AUTH_HINTS = ("gh auth login", "HTTP 401", "Bad credentials", "authentication", "not logged")


def notify_failure(text: str) -> bool:
    """既存の ntfy（mac_audio_pipeline._ntfy・話題名は ~/sakuma/.env）に1行だけ知らせる。
    🔴 新しい通知先は作らない。🔴 文に記事の題・アカウント名・gh の出力を入れない（名前と種類だけ）。"""
    try:
        from mac_audio_pipeline import _ntfy
        return bool(_ntfy(text))
    except Exception:
        return False


def fail_out(name: str, kind: str, notifier) -> int:
    """失敗をログにはっきり残し、ntfy に1行。🔴 黙って成功に見せない（exit 1）。"""
    print(f"  ❌❌ DISPATCH FAILED [{name}] {kind} — Actions は起きていません（cron の遅れた起動だけが残る）")
    sent = notifier(f"watcher dispatch 失敗: {name}（{kind}）")
    print(f"  ntfy: {'送った' if sent else '送れなかった/設定なし'}")
    return 1


def run(name: str, dry_run: bool = False, runner=subprocess.run, notifier=None) -> int:
    notifier = notifier or notify_failure
    print(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} dispatch {name}{' [dry-run]' if dry_run else ''}")
    gh = gh_path()
    if not gh:
        print("  ❌ gh が見つかりません（PATH を確認）")
        return fail_out(name, "gh が無い", notifier)
    cmd = dispatch_cmd(name, gh)
    if name == "daily_digest":
        advance_before_digest(dry_run)
    if dry_run:
        print("  [dry-run] 送るはずのコマンド: " + " ".join(cmd))
        return 0
    try:
        r = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"  ❌ gh が {GH_TIMEOUT} 秒で返りませんでした（送れたかは Actions で確認）")
        return fail_out(name, "timeout", notifier)
    except Exception as e:
        print(f"  ❌ gh を起動できません: {type(e).__name__}: {e}")
        return fail_out(name, "gh を起動できない", notifier)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        print(f"  ❌ dispatch 失敗 rc={r.returncode}: {out[:300]}")
        auth = any(h.lower() in out.lower() for h in AUTH_HINTS)
        return fail_out(name, "gh のログイン切れの疑い" if auth else f"rc={r.returncode}", notifier)
    print(f"  ✅ dispatch 送信: {WORKFLOWS[name]}（{out[:200]}）")
    return 0


def self_test() -> int:
    ok = fail = 0
    _quiet = lambda text: False   # 🔴 self-test で本物の ntfy を鳴らさない

    def check(n, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1; print(f"  ok  {n}")
        else:
            fail += 1; print(f"  NG  {n}: got={got!r} want={want!r}")

    check("site_uploader のコマンド", dispatch_cmd("site_uploader", "gh"),
          ["gh", "workflow", "run", "site_uploader.yml", "-R",
           "jumpdeeptreeinside-droid/health-policy-watcher", "--ref", "master"])
    check("daily_digest のコマンド", dispatch_cmd("daily_digest", "gh")[3], "daily_digest.yml")
    try:
        dispatch_cmd("fetch_news"); check("一覧に無い名前は送らない", "通った", "拒否")
    except ValueError:
        check("一覧に無い名前は送らない", "拒否", "拒否")

    calls = []

    class R:
        def __init__(self, rc): self.returncode, self.stdout, self.stderr = rc, "", "HTTP 422"

    check("dry-run は gh を呼ばない",
          (run("site_uploader", dry_run=True, runner=lambda *a, **k: calls.append(a) or R(0), notifier=_quiet), calls),
          (0, []))
    check("gh の失敗は exit 1", run("site_uploader", runner=lambda *a, **k: R(1), notifier=_quiet), 1)

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))
    check("gh が固まっても timeout で返る（exit 1）", run("site_uploader", runner=hang, notifier=_quiet), 1)
    seen = {}
    run("site_uploader", runner=lambda *a, **k: seen.update(k) or R(0), notifier=_quiet)
    check("timeout を必ず渡す", seen.get("timeout"), GH_TIMEOUT)
    # 失敗はログと ntfy（題・出力を入れない）
    sent = []

    class RA:
        returncode, stdout = 1, ""
        stderr = "To get started with GitHub CLI, please run:  gh auth login  (user: someone)"
    rc = run("site_uploader", runner=lambda *a, **k: RA(), notifier=lambda t: sent.append(t) or True)
    check("ログイン切れは exit 1", rc, 1)
    check("ntfy に1行（名前と種類だけ）", sent, ["watcher dispatch 失敗: site_uploader（gh のログイン切れの疑い）"])
    sent.clear()
    run("site_uploader", runner=hang, notifier=lambda t: sent.append(t) or True)
    check("timeout も ntfy", sent, ["watcher dispatch 失敗: site_uploader（timeout）"])
    sent.clear()
    run("site_uploader", runner=lambda *a, **k: R(0), notifier=lambda t: sent.append(t) or True)
    check("成功では知らせない", sent, [])
    print(f"\nself-test: {ok}件合格 / {fail}件不合格")
    return 0 if fail == 0 else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    try:
        sys.exit(run(args[0], dry_run="--dry-run" in sys.argv))
    except ValueError as e:
        print(f"  ❌ {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
