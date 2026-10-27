#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kigyo_collector の自己テスト（2026-10-27）。

`python3 src/kigyo_selftest.py` で走る。EDINETは叩かない（応答は作り物）。

走査の取り直し（A〜C）＋訂正有報の重ね方・期の結び付け・「これが最新の有報」の線。

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
import zipfile

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


# ---------- 「これが最新の有報」の線（export_site が使う） ----------
# 陽性＝次の有報が出ているはずなのに出ていない社／陰性＝まだ出る時期ではない社。
# 実在の期末で書く（作りやすい形で書くと本番の形だけ素通りする）。
TODAY = _dt.date(2026, 10, 27)
LATEST_YUHO = [
    # (期末, こうなるはず, なぜ)
    ("2025-03-31", True,  "3月期・次の期(2026-03-31)の有報は2026-06-30が期限＝過ぎている"),
    ("2025-02-28", True,  "2月期・次の期(2026-02-28)の有報は2026-05-28が期限＝過ぎている"),
    ("2026-05-20", False, "5月期・次の期の有報は2027-08-20ごろまで出ない"),
    ("2026-02-28", False, "2月期・次の期の有報は2027-05-28が期限"),
    ("2025-12-31", False, "12月期・次の期の有報は2027-03-31が期限"),
    ("2026-03-31", False, "🔴 今年ぶんを出したばかりの3月期。ここで印が付くと33社に誤って付く"),
    ("2026-04-30", False, "4月期"),
    ("", False, "期末が空（半期しか無い社）＝判断しない"),
    ("2026-13-45", False, "読めない日付＝判断しない"),
    # 線のきわ（1日ずらすと答えが変わるところ）
    ((TODAY - _dt.timedelta(days=K.NEXT_YUHO_DAYS)).isoformat(), False, "ちょうど期限＝まだ印を付けない"),
    ((TODAY - _dt.timedelta(days=K.NEXT_YUHO_DAYS + 1)).isoformat(), True, "期限の1日後＝印を付ける"),
]

# 🔴 猶予の日数そのものを確かめる。6月期の社は期末から約3か月遅れで有報を出すので、
#    「期末＋1年」で線を引くと、次の有報が出るまでの約3か月ぶん誤って印が付く。
#    実在の出し方で書く（ゲンキーは2026-06-20期の有報を2026-09-09に提出）。
LATEST_YUHO_DATED = [
    ("2026-06-20", _dt.date(2027, 7, 1), False,
     "期末＋1年は過ぎたが、次の有報は2027-09-20が期限＝まだ出ていなくて当たり前"),
    ("2026-06-20", _dt.date(2027, 10, 21), True,
     "次の有報の期限(2027-09-20)から1か月と1日＝印を付ける。2027-10-20 はちょうど線の上で False"),
    ("2026-06-20", _dt.date(2027, 6, 1), False, "まだ次の期末も来ていない"),
]


def check_latest_yuho():
    ng = []
    for pe, want, why in LATEST_YUHO:
        got = K.no_newer_yuho(pe, today=TODAY)
        if got != want:
            ng.append(f"これが最新の有報: {pe or '(空)'} が {got}・{want} のはず（{why}）")
    for pe, day, want, why in LATEST_YUHO_DATED:
        got = K.no_newer_yuho(pe, today=day)
        if got != want:
            ng.append(f"これが最新の有報: {pe} を {day} に見ると {got}・{want} のはず（{why}）")
    return ng


# ---------- 取れなかった日の数（ページに「確認できていない日がN日」と出す元） ----------
MISSED = [
    # (名前, 取り直し待ちの中身, 期待する数)
    ("当日ぶんだけ＝0", ["TODAY"], 0),
    ("前日ぶんは猶予の内＝0", ["TODAY", "YDAY"], 0),
    ("2日前が残っている＝1", ["D2"], 1),
    ("古い日が3つ＝3", ["D2", "D3", "D4"], 3),
    ("空＝0", [], 0),
]


def check_missed_days():
    ng = []
    t = _dt.date.today()
    sub = {"TODAY": t.isoformat(), "YDAY": (t - _dt.timedelta(days=1)).isoformat(),
           "D2": (t - _dt.timedelta(days=2)).isoformat(),
           "D3": (t - _dt.timedelta(days=3)).isoformat(),
           "D4": (t - _dt.timedelta(days=4)).isoformat()}
    tmp = tempfile.mkdtemp()
    keep = K.PENDING
    try:
        K.PENDING = os.path.join(tmp, "pending_dates.json")
        for name, days, want in MISSED:
            K._save_json(K.PENDING, {"pending": [sub[d] for d in days], "notified": -1})
            got = K.missed_days()
            if got != want:
                ng.append(f"取れなかった日: {name} が {got}・{want} のはず")
    finally:
        K.PENDING = keep
        shutil.rmtree(tmp)
    return ng


# ---------- 訂正有価証券報告書(130)の重ね方 ----------
Y = {"docID": "D120", "date": "2026-06-19", "code": "4568", "company": "X", "docType": "120",
     "desc": "有価証券報告書－第21期(2025/04/01－2026/03/31)", "periodEnd": "2026-03-31"}
T_AFTER = {"docID": "D130", "date": "2026-08-28", "code": "4568", "company": "X", "docType": "130",
           "desc": "訂正有価証券報告書－第21期(2025/04/01－2026/03/31)", "periodEnd": ""}
T_BEFORE = dict(T_AFTER, docID="D130b", date="2026-06-01")
T_OTHERKI = dict(T_AFTER, docID="D130c",
                 desc="訂正有価証券報告書－第20期(2024/04/01－2025/03/31)")
T_NOKI = dict(T_AFTER, docID="D130d", desc="訂正有価証券報告書")

# 120は全項目・130は直した項目だけ（訂正有報は直した部分だけの書類）
M120 = {"revenue": 100.0, "ordinary_income": 20.0, "avg_age": 43.7, "employees": 4206}
M130 = {"ordinary_income": 29.0, "avg_age": 44.6}

CORR = [
    # (名前, filings, こうなるはず metrics, 訂正の印)
    ("後に出た同じ期の130は重なる", [Y, T_AFTER],
     {"revenue": 100.0, "ordinary_income": 29.0, "avg_age": 44.6, "employees": 4206}, "D130"),
    ("🔴 130に無い項目は有報の値が残る（消さない）", [Y, T_AFTER],
     {"revenue": 100.0, "ordinary_income": 29.0, "avg_age": 44.6, "employees": 4206}, "D130"),
    ("有報より前に出た130は重ねない", [Y, T_BEFORE], M120, None),
    ("期が違う130は重ねない", [Y, T_OTHERKI], M120, None),
    ("題に期が無い130は重ねない", [Y, T_NOKI], M120, None),
    ("130が無ければ有報のまま", [Y], M120, None),
]


def check_corrected():
    ng = []
    # パースの版を上げたら、同じ書類でももう一度パースすること
    # （上げ忘れると、新しく拾うようにした項目が古い行に入らない）
    ng += _check_reparse_on_version()
    for name, filings, want, want_corr in CORR:
        tmp = tempfile.mkdtemp()
        keep = (K.DATA, K.FILINGS, K.COMPANIES, K.XBRL_DIR, K.load_filings,
                K.parse_xbrl, K._download, K.WATCHLIST_FULL)
        K.DATA = K.XBRL_DIR = tmp
        K.COMPANIES = os.path.join(tmp, "companies.json")
        K.load_filings = lambda: filings
        K._download = lambda doc_id, nm: doc_id          # 落とさない。IDをそのまま渡す
        K.parse_xbrl = lambda z: dict(M130 if z.startswith("D130") else M120)
        K.WATCHLIST_FULL = {"4568": ("X", "製薬")}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                K.fetch_and_parse()
            got = json.load(open(K.COMPANIES, encoding="utf-8"))["4568"]
        finally:
            (K.DATA, K.FILINGS, K.COMPANIES, K.XBRL_DIR, K.load_filings,
             K.parse_xbrl, K._download, K.WATCHLIST_FULL) = keep
            shutil.rmtree(tmp)
        if got["metrics"] != want:
            ng.append(f"訂正有報: {name} → {got['metrics']}・{want} のはず")
        if (got.get("corrected") or {}).get("docID") != want_corr:
            ng.append(f"訂正有報: {name} の印が {got.get('corrected')}・{want_corr} のはず")
        if got["docID"] != "D120":
            ng.append(f"訂正有報: {name} で有報のdocIDが {got['docID']} になっている")
    return ng


# ---------- 提出会社の従業員数（平均年収の母集団） ----------
# 本物の parse_xbrl を通す。中身だけ作り物にして、連結と提出会社を取り違えないか見る。
def _fake_zip(path, xbrl):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("XBRL/PublicDoc/jpcrp030000-asr-001.xbrl",
                   '<?xml version="1.0"?><xbrli:xbrl xmlns:jpcrp_cor="j">' + xbrl + "</xbrli:xbrl>")


def _emp(con, tan):
    x = ""
    if con is not None:
        x += f'<jpcrp_cor:NumberOfEmployees contextRef="CurrentYearInstant">{con}</jpcrp_cor:NumberOfEmployees>'
    if tan is not None:
        x += ('<jpcrp_cor:NumberOfEmployees contextRef="CurrentYearInstant_NonConsolidatedMember">'
              f'{tan}</jpcrp_cor:NumberOfEmployees>')
    return x


EMP = [
    # (名前, 連結, 提出会社, 期待する employees, 期待する employees_filer)
    ("持株会社（本社10人）", "5478", "10", 5478, 10),
    ("事業会社（ほぼ同じ）", "5984", "5977", 5984, 5977),
    ("🔴 提出会社の要素が無い（純粋持株会社で記載省略）", "1094", None, 1094, None),
    ("🔴 提出会社の値が空", "1973", " ", 1973, None),
]


def check_employees_filer():
    ng = []
    tmp = tempfile.mkdtemp()
    try:
        for name, con, tan, w_con, w_tan in EMP:
            z = os.path.join(tmp, "t.zip")
            _fake_zip(z, _emp(con, tan))
            m = K.parse_xbrl(z)
            if m.get("employees") != w_con:
                ng.append(f"提出会社の人数: {name} の連結が {m.get('employees')}・{w_con} のはず")
            if m.get("employees_filer") != w_tan:
                ng.append(f"提出会社の人数: {name} が {m.get('employees_filer')}・{w_tan} のはず")
    finally:
        shutil.rmtree(tmp)
    return ng


# ---------- --daily は新着が無い日も fetch/export を回す ----------
def check_daily_runs_anyway():
    ng = []
    for found, label in ((0, "新着が無い日"), (2, "新着がある日")):
        ran = []
        keep = (K.sweep_index, K.fetch_and_parse, K.export_site, sys.argv)
        K.sweep_index = lambda n: (ran.append("sweep"), found)[1]
        K.fetch_and_parse = lambda: ran.append("fetch")
        K.export_site = lambda: ran.append("export")
        sys.argv = ["kigyo_collector.py", "--daily"]
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                K.main()
        finally:
            (K.sweep_index, K.fetch_and_parse, K.export_site, sys.argv) = keep
        if ran != ["sweep", "fetch", "export"]:
            ng.append(f"--daily: {label} に回ったのは {ran}・sweep/fetch/export のはず")
    return ng


def _check_reparse_on_version():
    """版が古い行は、書類が同じでもパースし直す。"""
    ng = []
    tmp = tempfile.mkdtemp()
    keep = (K.DATA, K.COMPANIES, K.XBRL_DIR, K.load_filings, K.parse_xbrl,
            K._download, K.WATCHLIST_FULL)
    K.DATA = K.XBRL_DIR = tmp
    K.COMPANIES = os.path.join(tmp, "companies.json")
    K.load_filings = lambda: [Y]
    K._download = lambda doc_id, nm: doc_id
    K.parse_xbrl = lambda z: dict(M120)
    K.WATCHLIST_FULL = {"4568": ("X", "製薬")}
    try:
        for label, stored_v, want in (("版が古い", K.PARSE_VERSION - 1, True),
                                      ("版が同じ", K.PARSE_VERSION, False)):
            K._save_json(K.COMPANIES, {"4568": {
                "name": "X", "category": "製薬", "docID": "D120", "docDate": Y["date"],
                "docDesc": Y["desc"], "periodEnd": Y["periodEnd"], "parsed_v": stored_v,
                "metrics": {"revenue": 1.0}, "updated": "2026-01-01T00:00:00"}})
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                K.fetch_and_parse()
            got = json.load(open(K.COMPANIES, encoding="utf-8"))["4568"]
            again = got["metrics"] != {"revenue": 1.0}
            if again != want:
                ng.append(f"パースの版: {label} のときパースし直したか {again}・{want} のはず")
    finally:
        (K.DATA, K.COMPANIES, K.XBRL_DIR, K.load_filings, K.parse_xbrl,
         K._download, K.WATCHLIST_FULL) = keep
        shutil.rmtree(tmp)
    return ng


if __name__ == "__main__":
    bad = check(verbose="-q" not in sys.argv)
    bad += check_latest_yuho()
    bad += check_corrected()
    bad += check_employees_filer()
    bad += check_missed_days()
    bad += check_daily_runs_anyway()
    for b in bad:
        print("  🔴", b)
    print(("🔴 NG %d件" % len(bad)) if bad
          else "🟢 合格（走査3・最新有報の線14・訂正有報6・提出会社の人数4・取れなかった日5・--daily 2）")
    sys.exit(1 if bad else 0)
