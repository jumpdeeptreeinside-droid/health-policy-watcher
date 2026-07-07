#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企業情報収集（2026-07-07 ロードマップ3・EDINET API v2）。

ウォッチリスト企業（jinji_collector.WATCHLIST_FULL＝調剤/ドラッグ/卸/製薬/CRO）の
有価証券報告書・半期報告書をEDINETから取得し、決算＋キャリア軸データを構造化する:
  売上高・営業利益・純利益／従業員数・平均年間給与・平均年齢・平均勤続年数／発行済株式数

使い方:
  python3 src/kigyo_collector.py --index 400   # 過去N日の書類インデックス構築（初回）
  python3 src/kigyo_collector.py --fetch       # 各社の最新有報XBRLを取得・パース
  python3 src/kigyo_collector.py --daily       # 毎日: 直近2日をインデックス→新着あれば取得

データ: ~/kigyo_data/（filings.jsonl・xbrl/・companies.json）
出力のcompanies.jsonをPro（Streamlit）の企業情報画面が読む。
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
from jinji_collector import WATCHLIST_FULL  # noqa: E402

DATA = os.path.expanduser("~/kigyo_data")
FILINGS = os.path.join(DATA, "filings.jsonl")
XBRL_DIR = os.path.join(DATA, "xbrl")
COMPANIES = os.path.join(DATA, "companies.json")
API = "https://api.edinet-fsa.go.jp/api/v2"
WAIT = 1.0
# EDINETのsecCodeは5桁（4桁コード+0）
SEC2CODE = {k + "0": k for k in WATCHLIST_FULL}
DOCTYPES = {"120": "有価証券報告書", "130": "訂正有価証券報告書",
            "140": "四半期報告書", "160": "半期報告書"}


def api_get(path: str, params: dict, binary=False):
    params = dict(params, **{"Subscription-Key": config.EDINET_API_KEY})
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as r:
        raw = r.read()
    return raw if binary else json.loads(raw)


def load_filings() -> list:
    if not os.path.exists(FILINGS):
        return []
    return [json.loads(l) for l in open(FILINGS, encoding="utf-8") if l.strip()]


def sweep_index(days: int):
    """過去days日の提出書類一覧からウォッチ企業の報告書を拾う（済みの日はスキップ）"""
    os.makedirs(DATA, exist_ok=True)
    state_f = os.path.join(DATA, "swept_dates.json")
    swept = set(json.load(open(state_f))) if os.path.exists(state_f) else set()
    have = {f["docID"] for f in load_filings()}
    found = 0
    for i in range(days, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        if d in swept and i > 1:  # 直近2日は再チェック（当日追加分）
            continue
        try:
            r = api_get("documents.json", {"date": d, "type": 2})
        except Exception as e:
            print(f"  ✗ {d}: {e}")
            time.sleep(WAIT)
            continue
        for doc in r.get("results", []):
            sec = doc.get("secCode") or ""
            if sec not in SEC2CODE or doc.get("docTypeCode") not in DOCTYPES:
                continue
            if doc["docID"] in have:
                continue
            rec = {"docID": doc["docID"], "date": d, "code": SEC2CODE[sec],
                   "company": WATCHLIST_FULL[SEC2CODE[sec]][0],
                   "docType": doc.get("docTypeCode"),
                   "desc": (doc.get("docDescription") or "")[:80],
                   "periodEnd": doc.get("periodEnd") or ""}
            with open(FILINGS, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            have.add(doc["docID"])
            found += 1
            print(f"  + {d} {rec['company']} {DOCTYPES[rec['docType']]} {rec['desc'][:40]}")
        swept.add(d)
        if i % 50 == 0:
            json.dump(sorted(swept), open(state_f, "w"))
            print(f"  …{d} まで走査（新規{found}件）")
        time.sleep(WAIT)
    json.dump(sorted(swept), open(state_f, "w"))
    print(f"✓ index: 新規{found}件（累計{len(have)}件）")
    return found


# ---------- XBRLパース ----------
# 有報XBRL（jpcrp）から拾う要素。業種により売上の要素名が違うため候補順に探す。
REVENUE_TAGS = [
    "NetSalesSummaryOfBusinessResults",
    "RevenueIFRSSummaryOfBusinessResults",
    "RevenuesUSGAAPSummaryOfBusinessResults",
    "OperatingRevenue1SummaryOfBusinessResults",
    "GrossOperatingRevenueSummaryOfBusinessResults",
]
METRIC_TAGS = {
    # 有報サマリーに営業利益は無い→経常利益(JGAAP)/税引前利益(IFRS/USGAAP)を「利益」として拾う
    "ordinary_income": ["OrdinaryIncomeLossSummaryOfBusinessResults",
                        "ProfitLossBeforeTaxIFRSSummaryOfBusinessResults",
                        "IncomeBeforeIncomeTaxesUSGAAPSummaryOfBusinessResults"],
    "net_income": ["ProfitLossAttributableToOwnersOfParentSummaryOfBusinessResults",
                   "NetIncomeLossSummaryOfBusinessResults"],
    "employees": ["NumberOfEmployees"],
    "avg_salary": ["AverageAnnualSalaryInformationAboutReportingCompanyInformationAboutEmployees"],
    "avg_age": ["AverageAgeYearsInformationAboutReportingCompanyInformationAboutEmployees",
                "AverageAgeInformationAboutReportingCompanyInformationAboutEmployees"],
    "avg_tenure": ["AverageLengthOfServiceYearsInformationAboutReportingCompanyInformationAboutEmployees",
                   "AverageLengthOfServiceInformationAboutReportingCompanyInformationAboutEmployees"],
    "shares_issued": ["TotalNumberOfIssuedSharesSummaryOfBusinessResults"],
}


def _pick(xml: str, tags: list, ctx_pref: list) -> tuple:
    """コンテキスト優先→タグ候補の順で最初に見つかった数値を返す (値, タグ, ctx)。

    コンテキストを外側で回すのが重要：連結(CurrentYearDuration)を全タグで探し切ってから
    単体にフォールバックする。タグ外側だと、IFRS企業でNetSales(単体のみ)が
    RevenueIFRS(連結)より先にヒットして単体値を拾ってしまう。
    """
    for pref in ctx_pref:
        for tag in tags:
            for m in re.finditer(
                    r"<jpcrp_cor:%s\b[^>]*contextRef=\"([^\"]+)\"[^>]*>([^<]+)<" % re.escape(tag), xml):
                ctx, val = m.group(1), m.group(2).replace(",", "").strip()
                if re.fullmatch(pref, ctx):
                    try:
                        return float(val), tag, ctx
                    except ValueError:
                        pass
    return None, None, None


def parse_xbrl(path: str) -> dict:
    zf = zipfile.ZipFile(path)
    name = next((n for n in zf.namelist()
                 if n.startswith("XBRL/PublicDoc/") and n.endswith(".xbrl")), None)
    if not name:
        return {}
    xml = zf.read(name).decode("utf-8", errors="ignore")
    out = {}
    # 連結優先→単体。当期＝CurrentYear。
    dur_con = [r"CurrentYearDuration", r"CurrentYearDuration_NonConsolidatedMember"]
    ins_con = [r"CurrentYearInstant", r"CurrentYearInstant_NonConsolidatedMember"]
    v, tag, ctx = _pick(xml, REVENUE_TAGS, dur_con)
    if v is not None:
        out["revenue"] = v
        out["revenue_consolidated"] = "NonConsolidated" not in (ctx or "")
    v, _, _ = _pick(xml, METRIC_TAGS["ordinary_income"], dur_con)
    if v is not None:
        out["ordinary_income"] = v
    v, _, _ = _pick(xml, METRIC_TAGS["net_income"], dur_con)
    if v is not None:
        out["net_income"] = v
    # 前期比較用
    v, _, _ = _pick(xml, REVENUE_TAGS, [r"Prior1YearDuration", r"Prior1YearDuration_NonConsolidatedMember"])
    if v is not None:
        out["revenue_prior"] = v
    # 従業員・給与（単体＝提出会社。給与/年齢/勤続は単体のみ開示）
    v, _, ctx = _pick(xml, METRIC_TAGS["employees"], ins_con)
    if v is not None:
        out["employees"] = int(v)
    v, _, _ = _pick(xml, METRIC_TAGS["avg_salary"], [r"CurrentYearInstant(_NonConsolidatedMember)?"])
    if v is not None:
        out["avg_salary"] = v
    v, _, _ = _pick(xml, METRIC_TAGS["avg_age"], [r"CurrentYearInstant(_NonConsolidatedMember)?"])
    if v is not None:
        out["avg_age"] = v
    v, _, _ = _pick(xml, METRIC_TAGS["avg_tenure"], [r"CurrentYearInstant(_NonConsolidatedMember)?"])
    if v is not None:
        out["avg_tenure"] = v
    v, _, _ = _pick(xml, METRIC_TAGS["shares_issued"], ins_con)
    if v is not None:
        out["shares_issued"] = int(v)
    return out


def fetch_and_parse():
    """各社の最新有報（無ければ最新半期/四半期）をダウンロード→パース→companies.json"""
    os.makedirs(XBRL_DIR, exist_ok=True)
    filings = load_filings()
    comp = json.load(open(COMPANIES, encoding="utf-8")) if os.path.exists(COMPANIES) else {}
    for code, (name, cat) in WATCHLIST_FULL.items():
        mine = sorted([f for f in filings if f["code"] == code],
                      key=lambda f: (f["docType"] != "120", f["date"]), reverse=False)
        yuho = [f for f in mine if f["docType"] == "120"]
        target = (yuho or mine) and sorted(yuho or mine, key=lambda f: f["date"])[-1]
        if not target:
            print(f"  - {name}: 書類なし（インデックス期間に有報未提出）")
            continue
        cur = comp.get(code, {})
        if cur.get("docID") == target["docID"] and cur.get("metrics"):
            continue  # パース済み
        zpath = os.path.join(XBRL_DIR, f"{target['docID']}.zip")
        if not os.path.exists(zpath):
            try:
                raw = api_get(f"documents/{target['docID']}", {"type": 1}, binary=True)
                open(zpath, "wb").write(raw)
                time.sleep(WAIT)
            except Exception as e:
                print(f"  ✗ {name}: DL失敗 {e}")
                continue
        try:
            metrics = parse_xbrl(zpath)
        except Exception as e:
            print(f"  ✗ {name}: XBRLパース失敗 {e}")
            continue
        comp[code] = {"name": name, "category": cat, "docID": target["docID"],
                      "docDate": target["date"], "docDesc": target["desc"],
                      "periodEnd": target.get("periodEnd", ""), "metrics": metrics,
                      "updated": datetime.now().isoformat(timespec="seconds")}
        got = ", ".join(k for k in ("revenue", "employees", "avg_salary") if k in metrics)
        print(f"  ✓ {name}: {target['desc'][:36]} → {got or '（数値抽出なし）'}")
    json.dump(comp, open(COMPANIES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"✓ companies.json: {len(comp)}社")


SITE_REPO = os.path.expanduser("~/crosshealthjp")
SITE_JSON = os.path.join(SITE_REPO, "src/data/kigyo_companies.json")


def export_site():
    """companies.json→サイトの/companies/ページ用JSONに書き出し。変わった時だけcommit+push。"""
    import subprocess
    comp = json.load(open(COMPANIES, encoding="utf-8"))
    out = []
    for code, d in comp.items():
        m = d["metrics"]
        out.append({
            "code": code, "name": d["name"], "category": d["category"],
            "periodEnd": d.get("periodEnd", ""),
            "revenue_oku": round(m["revenue"] / 1e8) if m.get("revenue") else None,
            "growth_pct": round((m["revenue"] / m["revenue_prior"] - 1) * 100, 1)
                          if m.get("revenue") and m.get("revenue_prior") else None,
            "ordinary_oku": round(m["ordinary_income"] / 1e8) if m.get("ordinary_income") else None,
            "net_oku": round(m["net_income"] / 1e8) if m.get("net_income") else None,
            "employees": m.get("employees"),
            "salary_man": round(m["avg_salary"] / 1e4) if m.get("avg_salary") else None,
            "age": m.get("avg_age"), "tenure": m.get("avg_tenure"),
        })
    out.sort(key=lambda x: -(x["revenue_oku"] or 0))
    rec = {"meta": {"updated": max(d["updated"] for d in comp.values())[:10], "count": len(out)},
           "companies": out}
    new = json.dumps(rec, ensure_ascii=False, indent=1)
    if os.path.exists(SITE_JSON) and open(SITE_JSON, encoding="utf-8").read() == new:
        print("  site: 変更なし")
        return
    open(SITE_JSON, "w", encoding="utf-8").write(new)
    for cmd in (["git", "pull", "--rebase", "--autostash", "--quiet"],
                ["git", "add", "src/data/kigyo_companies.json"],
                ["git", "commit", "-q", "-m", "companies: 企業データ自動更新"],
                ["git", "push", "-q"]):
        r = subprocess.run(cmd, cwd=SITE_REPO, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ✗ site {' '.join(cmd[:2])}: {r.stderr[-200:]}")
            return
    print("✓ site: /companies/ データ更新をpush（数分で本番反映）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, metavar="DAYS")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--export-site", action="store_true")
    args = ap.parse_args()
    if args.index:
        sweep_index(args.index)
    if args.daily:
        if sweep_index(2):
            fetch_and_parse()
            export_site()
    if args.fetch:
        fetch_and_parse()
    if args.export_site:
        export_site()


if __name__ == "__main__":
    main()
