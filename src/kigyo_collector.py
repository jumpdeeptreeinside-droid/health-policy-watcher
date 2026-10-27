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
from jinji_collector import WATCHLIST_FULL, notify  # noqa: E402

DATA = os.path.expanduser("~/kigyo_data")
FILINGS = os.path.join(DATA, "filings.jsonl")
XBRL_DIR = os.path.join(DATA, "xbrl")
COMPANIES = os.path.join(DATA, "companies.json")
# 取り直しが要る日の控え（失敗した日＋当日ぶん）。swept_dates.json の形は変えない
# ＝名前を変えると既存の532日ぶんが静かに読めなくなるので、足す形にする。
PENDING = os.path.join(DATA, "pending_dates.json")
API = "https://api.edinet-fsa.go.jp/api/v2"
WAIT = 1.0
# EDINETのsecCodeは5桁（4桁コード+0）
SEC2CODE = {k + "0": k for k in WATCHLIST_FULL}
DOCTYPES = {"120": "有価証券報告書", "130": "訂正有価証券報告書",
            "140": "四半期報告書", "160": "半期報告書"}
# 🔴 パースの中身を変えたら必ず上げる。上げないと「パース済み」でとばされ、
#    新しく拾うようにした項目が古い行に入らない（employees_filer を足した時に踏んだ）。
#    zipは手元にあるのでEDINETは叩き直さない。
PARSE_VERSION = 2


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


def _save_json(path, obj):
    """🔴 open(path,"w") を先に開くと、落ちた時に0バイトが残る。tmpに書いて置き換える。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _load_pending() -> tuple:
    """取り直し待ちの日と、最後に通知した日数を返す。壊れていたら空から始める。"""
    try:
        j = json.load(open(PENDING, encoding="utf-8"))
    except Exception:
        return set(), -1
    if isinstance(j, list):          # 古い形（念のため）
        return set(j), -1
    return set(j.get("pending") or []), int(j.get("notified", -1))


def sweep_index(days: int):
    """過去days日＋取り直し待ちの日の提出書類一覧からウォッチ企業の報告書を拾う。

    🔴「済みの日はスキップ・直近2日だけ再チェック」だけだと、取りこぼしが永久に残る
       （2026-10-27にEDINETを取り直して実測で判明）。実際に起きた形：
         2026-08-19 07:20 の走査では、その日の日中に出た有報がまだEDINETに載っていない
           → それでもその日を「済み」に入れる
         2026-08-20 の再チェック（i=1）が DNS エラーで落ちる → どこにも残らない
         2026-08-21 には 08-19 が直近2日の窓から外れ、「済み」なので二度と見ない
       ＝ドラッグストア1社の有報が2か月以上入らず、ページは1年前の期のままだった。
       2026-10-14 は 403 が3日続いて同じ形で落ちた（2社の半期報告書）。社名は filings.jsonl 側。
    対策は2つ：
      ① 失敗した日を pending_dates.json に残し、窓に関係なく毎回取り直す（直近2日に頼らない）
      ② 当日ぶんは「まだ増える」ので、走査できても pending に残し、翌日もう一度見るまで外さない
    """
    os.makedirs(DATA, exist_ok=True)
    state_f = os.path.join(DATA, "swept_dates.json")
    swept = set(json.load(open(state_f))) if os.path.exists(state_f) else set()
    pending, notified = _load_pending()
    have = {f["docID"] for f in load_filings()}
    found = 0
    today = date.today().isoformat()
    yday = (date.today() - timedelta(days=1)).isoformat()
    window = [(date.today() - timedelta(days=i)).isoformat() for i in range(days, -1, -1)]
    # 窓の外の取り直し待ち（古い順）＋ 窓の中で見る必要がある日
    targets = sorted(d for d in pending if d not in window)
    targets += [d for d in window if d not in swept or d in pending or d >= yday]
    for n, d in enumerate(targets):
        r, why = None, "応答がありません"
        for attempt in range(3):
            try:
                r = api_get("documents.json", {"date": d, "type": 2})
                break
            except Exception as e:
                why = f"{type(e).__name__}: {str(e)[:70]}"
                if attempt < 2:
                    time.sleep(WAIT * 5 * (attempt + 1))   # 403はまとまって出るので少し待つ
        if r is None:
            # 🔴 ここで黙って continue していたのが根っこ。済みの日だと誰も気づかない。
            pending.add(d)
            _save_json(PENDING, {"pending": sorted(pending), "notified": notified})
            print(f"  ✗ {d}: {why}（取り直し待ちに入れました）")
            time.sleep(WAIT)
            continue
        for doc in r.get("results") or []:
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
        if d >= today:
            pending.add(d)      # 当日ぶんは翌日もう一度見る
        else:
            pending.discard(d)
        _save_json(state_f, sorted(swept))
        _save_json(PENDING, {"pending": sorted(pending), "notified": notified})
        if len(targets) > 20 and n % 50 == 0:
            print(f"  …{d} まで走査（新規{found}件）")
        time.sleep(WAIT)
    # 当日ぶんはまだ増える。前日ぶんは次の回で取り直せる＝1日ぶんの猶予を置き、
    # 一度こけただけで毎回通知が出て本物の取りこぼしが薄まるのを防ぐ。
    stale = sorted(d for d in pending if d < yday)
    if stale:
        print(f"  🔴 取れなかった日 {len(stale)}（{stale[0]} 〜 {stale[-1]}・次の回でまた取り直します）")
        if len(stale) != notified:
            notify("企業データ", f"取れなかった日 {len(stale)}")   # ntfyは公開話題＝数だけ
            notified = len(stale)
    elif notified > 0:
        notify("企業データ", "取れなかった日 0")
        notified = 0
    _save_json(PENDING, {"pending": sorted(pending), "notified": notified})
    print(f"✓ index: 新規{found}件（累計{len(have)}件・取れなかった日{len(stale)}）")
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
    # 🔴 平均給与・年齢・勤続は「提出会社」の値で、連結の従業員とは別の母集団。
    #    持株会社だと提出会社＝本社だけ（連結5,478人のうち10人、で平均年収1,109万円の社がある）。
    #    数字と一緒に人数を出さないと、読む人は会社全体の平均だと受け取る（翔太さん決定 2026-10-27）。
    #    純粋持株会社で「提出会社の状況」を記載省略している社は、この要素が無い＝Noneのまま。
    v, _, _ = _pick(xml, METRIC_TAGS["employees"], [r"CurrentYearInstant_NonConsolidatedMember"])
    if v is not None:
        out["employees_filer"] = int(v)
    return out


def _kinki(desc: str):
    """書類の題から期を取り出す（「第161期(2025/04/01－2026/03/31)」）。

    訂正有報(130)は periodEnd が空で返ってくるので、有報と訂正有報を結び付ける鍵は題の中の期。
    """
    m = re.search(r"第\d+期\([\d/]+－[\d/]+\)", desc or "")
    return m.group(0) if m else None


def _download(doc_id: str, name: str):
    """XBRLのzipを（無ければ）落として置き場所を返す。落ちたら None。"""
    zpath = os.path.join(XBRL_DIR, f"{doc_id}.zip")
    if os.path.exists(zpath):
        return zpath
    try:
        raw = api_get(f"documents/{doc_id}", {"type": 1}, binary=True)
        # 🔴 open(...,"w") を先に開くと落ちた時に0バイトのzipが残り、次から「ある」と判断される
        tmp = zpath + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, zpath)
        time.sleep(WAIT)
        return zpath
    except Exception as e:
        print(f"  ✗ {name}: DL失敗 {type(e).__name__}: {str(e)[:70]}")
        return None


def fetch_and_parse():
    """各社の最新有報（無ければ最新半期/四半期）をダウンロード→パース→companies.json

    同じ期の有報より後に出た訂正有価証券報告書(130)があれば、その値を上に重ねる
    （翔太さん決定 2026-10-27）。2026-10-27 の実測では、対象5件のうち
      ・第一三共 税引前利益 263,432→292,391百万円（本文にも訂正後の表がある）
      ・小野薬品工業 平均年齢 43.7→44.6（本文に「訂正前」「訂正後」で並んでいる）
    の2件で数字が変わり、残り3件は1項目も変わらなかった。
    🔴 全部を訂正有報で置き換えてはいけない。訂正有報は直した部分だけの書類なので、
       載っていない項目は有報の値を残す（＝重ねる・消さない）。
    """
    os.makedirs(XBRL_DIR, exist_ok=True)
    filings = load_filings()
    comp = json.load(open(COMPANIES, encoding="utf-8")) if os.path.exists(COMPANIES) else {}
    for code, (name, cat) in WATCHLIST_FULL.items():
        mine = sorted([f for f in filings if f["code"] == code], key=lambda f: f["date"])
        yuho = [f for f in mine if f["docType"] == "120"]
        target = (yuho or [f for f in mine if f["docType"] != "130"] or mine)
        target = target and target[-1]
        if not target:
            print(f"  - {name}: 書類なし（インデックス期間に有報未提出）")
            continue
        ki = _kinki(target["desc"])
        tei = [f for f in mine if f["docType"] == "130" and ki
               and _kinki(f["desc"]) == ki and f["date"] > target["date"]]
        tei = tei[-1] if tei else None
        cur = comp.get(code, {})
        if (cur.get("docID") == target["docID"] and cur.get("metrics")
                and cur.get("parsed_v") == PARSE_VERSION
                and (cur.get("corrected") or {}).get("docID") == (tei or {}).get("docID")):
            continue  # パース済み（訂正有報の有無もパースの版も同じ）
        zpath = _download(target["docID"], name)
        if not zpath:
            continue
        try:
            metrics = parse_xbrl(zpath)
        except Exception as e:
            print(f"  ✗ {name}: XBRLパース失敗 {type(e).__name__}: {str(e)[:70]}")
            continue
        fixed = []
        if tei:
            zt = _download(tei["docID"], name + "（訂正有報）")
            if zt:
                try:
                    tm = parse_xbrl(zt)
                except Exception as e:
                    print(f"  ✗ {name}: 訂正有報のパース失敗 {type(e).__name__}: {str(e)[:70]}")
                    tm = {}
                for k, v in tm.items():       # 載っている項目だけ上に重ねる
                    if metrics.get(k) != v:
                        fixed.append(k)
                    metrics[k] = v
        comp[code] = {"name": name, "category": cat, "docID": target["docID"],
                      "docDate": target["date"], "docDesc": target["desc"],
                      "periodEnd": target.get("periodEnd", ""), "metrics": metrics,
                      "parsed_v": PARSE_VERSION,
                      "updated": datetime.now().isoformat(timespec="seconds")}
        if tei:
            comp[code]["corrected"] = {"docID": tei["docID"], "date": tei["date"]}
        got = ", ".join(k for k in ("revenue", "employees", "avg_salary") if k in metrics)
        note = (f"／訂正有報{tei['date']}で{'・'.join(fixed) if fixed else '変わった数字なし'}"
                if tei else "")
        print(f"  ✓ {name}: {target['desc'][:36]} → {got or '（数値抽出なし）'}{note}")
    json.dump(comp, open(COMPANIES, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"✓ companies.json: {len(comp)}社")


SITE_REPO = os.path.expanduser("~/crosshealthjp")
SITE_JSON = os.path.join(SITE_REPO, "src/data/kigyo_companies.json")


# 次の期の有報の提出期限（期末の3か月後）＋1か月の余裕。これを過ぎても新しい有報が無ければ
# 「これが最新の有報」と出す。日数で持つのは、うるう年と月末（2/28・5/20 など）で
# 月の足し算が崩れないようにするため。365＋92＋30＝487日。
NEXT_YUHO_DAYS = 487


def no_newer_yuho(period_end: str, today=None) -> bool:
    """次の期の有報が出ているはずの時期を過ぎても、手元が古い期のままか。

    🔴 ここで分かるのは「新しい有報が出ていない」ことだけ。
       上場廃止・統合などの理由はEDINETの索引からは読めないので、ページにも書かない
       （サクマ 2026-10-27）。
    """
    if not period_end:
        return False
    try:
        pe = date.fromisoformat(period_end)
    except ValueError:
        return False
    return (today or date.today()) > pe + timedelta(days=NEXT_YUHO_DAYS)


def missed_days() -> int:
    """取り直しても取れていない日の数（ページで「確認できていない日がN日」と出すため）。"""
    pending, _ = _load_pending()
    yday = (date.today() - timedelta(days=1)).isoformat()
    return len([d for d in pending if d < yday])


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
            # 次の有報が出ているはずなのに出ていない社（ページで「これが最新の有報」と出す）
            "latest_yuho": no_newer_yuho(d.get("periodEnd", "")),
            "revenue_oku": round(m["revenue"] / 1e8) if m.get("revenue") else None,
            "growth_pct": round((m["revenue"] / m["revenue_prior"] - 1) * 100, 1)
                          if m.get("revenue") and m.get("revenue_prior") else None,
            "ordinary_oku": round(m["ordinary_income"] / 1e8) if m.get("ordinary_income") else None,
            "net_oku": round(m["net_income"] / 1e8) if m.get("net_income") else None,
            "employees": m.get("employees"),
            "salary_man": round(m["avg_salary"] / 1e4) if m.get("avg_salary") else None,
            "age": m.get("avg_age"), "tenure": m.get("avg_tenure"),
            # 平均年収・年齢・勤続の母集団（提出会社。持株会社だと本社だけ）
            "employees_filer": m.get("employees_filer"),
        })
    out.sort(key=lambda x: -(x["revenue_oku"] or 0))
    rec = {"meta": {"updated": max(d["updated"] for d in comp.values())[:10], "count": len(out),
                    "missed_days": missed_days()},
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
        # 🔴 以前は「その回に新しい書類が見つかったら」だけ fetch/export を回していた。
        #    前の回にダウンロードやパースが落ちた社は、新着が出るまで取り直されなかった
        #    （翔太さん決定 2026-10-27 で直した）。fetch_and_parse はパース済みの社を
        #    とばすので、やることが無い日はEDINETを1回も叩かない。
        sweep_index(2)
        fetch_and_parse()
        export_site()
    if args.fetch:
        fetch_and_parse()
    if args.export_site:
        export_site()


if __name__ == "__main__":
    main()
