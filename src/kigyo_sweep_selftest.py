#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kigyo_collector.sweep_index の自己テスト（2026-10-27）。

`python3 src/kigyo_sweep_selftest.py` で走る。EDINETは叩かない（応答は作り物）。

なぜ要るか：2026-08〜10 に「走査済みの日に取りこぼしが残り、誰にも気づかれない」事故が
2回起きた。ページの数字が2か月古いままだったのに、ログは毎朝「新規0件」で正常に見えていた。
検査は「合格する」だけでは意味がないので、
  ・直す前の形（old_sweep）を陰性対照として置き、再生がちゃんと事故を再現することを確かめる
  ・入ったかどうかだけでなく「いつ入ったか」と「取り直し待ちが残っていないか」も見る
の2つを入れている。守りを1つずつ壊すと7通りすべてでこの検査が落ちる（2026-10-27 実測）。
"""
import contextlib
import datetime as _dt
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kigyo_collector as K  # noqa: E402

# EDINETが返す形だけ真似たもの（社名は入れない。secCodeはウォッチリストの3社）
YUHO = {"docID": "S100TEST1", "secCode": "35490", "docTypeCode": "120",
        "docDescription": "有価証券報告書－第28期(2025/05/21－2026/05/20)", "periodEnd": "2026-05-20"}
HANKI = {"docID": "S100TEST2", "secCode": "76490", "docTypeCode": "160",
         "docDescription": "半期報告書－第45期(2026/03/01－2027/02/28)", "periodEnd": "2027-02-28"}
HANKI2 = {"docID": "S100TEST3", "secCode": "33910", "docTypeCode": "160",
          "docDescription": "半期報告書－第65期(2026/03/01－2027/02/28)", "periodEnd": "2027-02-28"}

# 名前: 回す日 / 丸ごと落ちる(走る日,取る日) / 載った書類{載った日:[doc]} / 探すID /
#       いつ入れば正しいか / 1回の走査の中で何回だけ落ちるか{(走る日,取る日): 回数}
SCEN = {
    # A その日の走査には間に合わず、翌日の再チェックが落ちた形（2026-08-19 に起きた）
    "A_翌日の再チェックが落ちる": (
        ["2026-08-19", "2026-08-20", "2026-08-21", "2026-08-22"],
        {("2026-08-20", "2026-08-19"), ("2026-08-20", "2026-08-20")},
        {"2026-08-19": [YUHO]}, "S100TEST1", "2026-08-21", {}),
    # B 403が3日続いて直近2日の窓から外れた形（2026-10-14 に起きた）
    "B_403が3日続く": (
        ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19"],
        {(r, "2026-10-14") for r in ("2026-10-14", "2026-10-15", "2026-10-16")}
        | {("2026-10-14", "2026-10-13")},
        {"2026-10-14": [HANKI]}, "S100TEST2", "2026-10-17", {}),
    # C 403はまとまって出る。1回の走査の中で2回弾かれて3回目で通る形
    "C_走査の中で403が2回": (
        ["2026-10-14", "2026-10-15"], set(), {"2026-10-14": [HANKI2]},
        "S100TEST3", "2026-10-15", {("2026-10-15", "2026-10-14"): 2}),
}


def run(scen, use_old=False):
    days, fail, posted, want, _want_day, flaky = SCEN[scen]
    left = dict(flaky)
    tmp = tempfile.mkdtemp()
    keep = (K.DATA, K.FILINGS, K.PENDING, K.WAIT, K.api_get, K.notify, K.date)
    K.DATA, K.WAIT = tmp, 0
    K.FILINGS = os.path.join(tmp, "filings.jsonl")
    K.PENDING = os.path.join(tmp, "pending_dates.json")
    K.notify = lambda *a, **kw: None
    cur = {"today": None}

    def fake(path, params, binary=False):
        d = params["date"]
        if (cur["today"], d) in fail:
            raise OSError("HTTP Error 403: Forbidden")
        if left.get((cur["today"], d), 0) > 0:
            left[(cur["today"], d)] -= 1
            raise OSError("HTTP Error 403: Forbidden")
        # その日の日中に出た書類は、翌日以降の走査でしか見えない
        return {"metadata": {"status": "200"},
                "results": posted.get(d, []) if d < cur["today"] else []}
    K.api_get = fake

    class FakeDate(_dt.date):
        @classmethod
        def today(cls):
            return _dt.date(*map(int, cur["today"].split("-")))
    K.date = FakeDate

    out = []
    try:
        for day in days:
            cur["today"] = day
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                (old_sweep if use_old else K.sweep_index)(2)
            got = ([json.loads(x)["docID"] for x in open(K.FILINGS, encoding="utf-8")]
                   if os.path.exists(K.FILINGS) else [])
            out.append({"day": day, "has": want in got, "log": buf.getvalue(),
                        "pending": [] if use_old else sorted(K._load_pending()[0])})
    finally:
        (K.DATA, K.FILINGS, K.PENDING, K.WAIT, K.api_get, K.notify, K.date) = keep
        shutil.rmtree(tmp)
    return out


def old_sweep(days):
    """直す前の形（陰性対照）。これで取りこぼさないなら、再生が事故を再現できていない。"""
    state_f = os.path.join(K.DATA, "swept_dates.json")
    swept = set(json.load(open(state_f))) if os.path.exists(state_f) else set()
    have = {f["docID"] for f in K.load_filings()}
    t = K.date.today()
    for i in range(days, -1, -1):
        d = (t - _dt.timedelta(days=i)).isoformat()
        if d in swept and i > 1:
            continue
        try:
            r = K.api_get("documents.json", {"date": d, "type": 2})
        except Exception:
            continue
        for doc in r.get("results", []):
            if doc["docID"] in have:
                continue
            with open(K.FILINGS, "a", encoding="utf-8") as f:
                f.write(json.dumps({"docID": doc["docID"], "date": d}) + "\n")
            have.add(doc["docID"])
        swept.add(d)
    json.dump(sorted(swept), open(state_f, "w"))


def check(verbose=True):
    ng = []
    for scen in SCEN:
        if any(r["has"] for r in run(scen, use_old=True)):
            ng.append(f"{scen}: 直す前の形で取りこぼさない＝再生が事故を再現できていない")
        rs = run(scen)
        if not rs[-1]["has"]:
            ng.append(f"{scen}: 最後まで取り直せていない")
        got_day = next((r["day"] for r in rs if r["has"]), None)
        if got_day != SCEN[scen][4]:
            ng.append(f"{scen}: 入った日が {got_day}・{SCEN[scen][4]} のはず")
        # 残っていていいのは当日ぶんだけ（まだ増えるので翌日もう一度見る）
        if rs[-1]["pending"] != [rs[-1]["day"]]:
            ng.append(f"{scen}: 取り直し待ちが当日ぶんだけになっていない {rs[-1]['pending']}")
        if "取れなかった日0" not in rs[-1]["log"]:
            ng.append(f"{scen}: 最後の回が「取れなかった日0」と言っていない")
        if verbose:
            print(f"  {scen}")
            for r in rs:
                print(f"    {r['day']} 入った:{'🟢' if r['has'] else '🔴'} "
                      f"取り直し待ち:{r['pending'] or 'なし'}")
    # 403が続いたら「取れなかった日」が出ること（出ないと誰も気づけない）
    if not any("🔴 取れなかった日" in r["log"] for r in run("B_403が3日続く")):
        ng.append("B: 403が続いても「取れなかった日」が出ない")
    # 翌日の1回で取り直せた形では騒がない（毎日の通知を薄めない）
    if any("🔴 取れなかった日" in r["log"] for r in run("A_翌日の再チェックが落ちる")):
        ng.append("A: 翌日取り直せた形で「取れなかった日」を出している")
    return ng


if __name__ == "__main__":
    bad = check(verbose="-q" not in sys.argv)
    for b in bad:
        print("  🔴", b)
    print(("🔴 NG %d件" % len(bad)) if bad else "🟢 合格（3つの形・取りこぼし0）")
    sys.exit(1 if bad else 0)
