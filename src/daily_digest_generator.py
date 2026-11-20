#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海外ヘッドラインまとめ生成（2026-07-07 木内さん設計）

国際系（国際・日本関連／国際・その他）の記事は個別に音声化せず、
1日1本の「海外ヘッドライン」エピソードに束ねる。記事（Web）は従来どおり個別に詳細。

フロー（毎日 JST 19:05・Mac launchd の workflow_dispatch＋GitHub cron の予備）:
  1. Status(Podcast)=音声化待ち かつ Category=国際系 の記事を収集
  2. 各記事の本文（Notionページのchildren＝生成済みブログ）から事実ベースの1〜2文ヘッドラインをGeminiで作成
  3. まとめ台本を持つ新しいDB行【海外ヘッドライン】を作成し Status(Podcast)=音声化待ち に
     → 以降はMac音声パイプライン（合成→AI検品→試聴→公開）に自然合流
  4. 元記事の Status(Podcast) を「完了」へ（個別音声化はしない）
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('WORDPRESS_URL', 'https://unused.invalid')
os.environ.setdefault('WORDPRESS_USERNAME', 'unused')
os.environ.setdefault('WORDPRESS_APP_PASSWORD', 'unused')

from notion_wordpress_uploader import NotionWordPressUploader, logger  # noqa: E402

_JST = timezone(timedelta(hours=9))
INTL = ("国際・日本関連", "国際・その他")


def gemini_headlines(items: list) -> list:
    """[(title, 本文抜粋)] → 事実ベースの1〜2文ヘッドライン（配列で返す）"""
    import google.generativeai as genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        try:
            import config
            api_key = config.GEMINI_API_KEY
        except ImportError:
            pass
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(os.environ.get("GEMINI_MODEL") or "gemini-flash-latest")
    blocks = "\n\n".join(f"### 記事{i+1}: {t}\n{body[:1200]}" for i, (t, body) in enumerate(items))
    prompt = (
        "あなたはニュース番組の原稿作成者です。以下の各記事を、音声で読み上げる1〜2文のヘッドライン原稿にしてください。\n"
        "ルール: 事実のみ（記事にない情報は追加しない）／話し言葉のデスマス調／固有名詞・数字は変更しない／"
        "一文は短く／句点（。）ごとに改行。\n"
        "英語を残さない（最重要）: 機関名・薬剤名・略語はすべて日本語かカタカナに（WHO→世界保健機関、"
        "AMR→薬剤耐性、RUXOLITINIB→ルキソリチニブ 等）。アルファベットのまま残さないこと。\n"
        f"JSON配列のみ出力（記事数={len(items)}・インデックス対応）: [\"ヘッドライン1\", ...]\n\n" + blocks
    )
    raw = model.generate_content(prompt).text.strip().strip('`').removeprefix('json').strip()
    out = json.loads(raw)
    if len(out) != len(items):
        raise ValueError(f"件数不一致: {len(out)} vs {len(items)}")
    return out


DIGEST_PREFIX = "【海外ヘッドライン】"


def last_noon_utc(now_utc: datetime) -> datetime:
    """いまから見て直近の JST 12:00（UTC で返す）。"""
    j = now_utc.astimezone(_JST)
    noon = j.replace(hour=12, minute=0, second=0, microsecond=0)
    if j < noon:
        noon -= timedelta(days=1)
    return noon.astimezone(timezone.utc)


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def recent_digest_filter(now_utc: datetime) -> dict:
    return {"and": [
        {"property": "Title", "title": {"starts_with": DIGEST_PREFIX}},
        {"timestamp": "created_time", "created_time": {"on_or_after": _z(last_noon_utc(now_utc))}},
        {"timestamp": "created_time", "created_time": {"on_or_before": _z(now_utc)}},
    ]}


def already_made_recently(query, now_utc: datetime) -> bool:
    """🔴 2026-11-20: Mac の launchd から 19:05 に workflow_dispatch を送るようにした。
    GitHub の cron（予定 19:05・実測は翌朝4〜5時台に遅れて起動）も残すので、
    「直近の JST 12:00 以降にまとめ行を作っていれば作らない」で1日1本を守る。
    （14時間の窓だと、cron が 05:05 より遅れて動いた日はその日の 19:05 の回まで止まる＝ミハル 11/20）
    query(filter)->rows。失敗は例外のまま（作らない側に倒れる）。
    同じ記事の二重配信は concurrency（daily_digest.yml）と「元記事を完了に」で防いでいる。"""
    return bool(query(recent_digest_filter(now_utc)))


_TITLE_DAY = re.compile(r"^【海外ヘッドライン】(\d{4})年(\d{1,2})月(\d{1,2})日")


def make_title(day, n_items: int, existing_titles: list) -> str:
    """題。日付は記事の Date(Search) のいちばん新しい日。
    🔴 同じ日付の回がもうあれば「その2」「その3」…を付ける（同じ題を2本作らない）。
       例：今朝 03:57（旧方式・実行日で題）に「11月20日」ができていて、19:05 の回も記事の日が 11/20。
    「11月2日」と「11月20日」を取り違えないよう、前方一致でなく年月日を数として照らす。"""
    same = 0
    for t in existing_titles:
        m = _TITLE_DAY.match(t or "")
        if m and (int(m.group(1)), int(m.group(2)), int(m.group(3))) == (day.year, day.month, day.day):
            same += 1
    suffix = f"その{same + 1}" if same else ""
    return f"{DIGEST_PREFIX}{day.year}年{day.month}月{day.day}日{suffix} 世界の医療・保健ニュース{n_items}本"


def titles_for_day(query, day) -> list:
    rows = query({"property": "Title",
                  "title": {"starts_with": f"{DIGEST_PREFIX}{day.year}年{day.month}月{day.day}日"}})
    out = []
    for r in rows:
        arr = ((r.get("properties") or {}).get("Title") or {}).get("title") or []
        out.append("".join(x.get("plain_text", "") for x in arr))
    return out


def digest_date(pages: list, now_jst: datetime):
    """題の日付＝入っている記事の Date(Search)（収集日）のうち、いちばん新しい日（JST）。

    🔴 2026-11-20: 実行時刻で付けていたため、cron が翌朝4時に遅れて動くと前日の記事に
       翌日の日付が付いた（11/19 分が「11月20日」に）。
    Date(Search) が無い記事：日付の計算から外す（題は残りの記事で決める）。
    全部に無い：実行日（JST）にして、理由を返す（呼ぶ側がログに出す）。
    戻り値: (date, 無かった件数)
    """
    days, missing = [], 0
    for p in pages:
        d = ((p.get("properties") or {}).get("Date(Search)") or {}).get("date") or {}
        start = d.get("start")
        if not start:
            missing += 1
            continue
        try:
            if "T" in start:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_JST)
                days.append(dt.astimezone(_JST).date())
            else:
                days.append(datetime.strptime(start[:10], "%Y-%m-%d").date())
        except ValueError:
            missing += 1
    return (max(days) if days else now_jst.date()), missing


def _strict_query(nw, filt: dict, page_size: int = 50) -> list:
    """検索。🔴 失敗を0件に見せない（0件だと「まだ無い」と読んで2本目を作ってしまう）。"""
    import requests
    r = requests.post(f"{nw.notion_base}/databases/{nw.database_id}/query",
                      headers=nw.notion_headers, json={"filter": filt, "page_size": page_size}, timeout=30)
    r.raise_for_status()
    return r.json().get("results", [])


def self_test() -> int:
    ok = fail = 0

    def check(name, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1; print(f"  ok  {name}")
        else:
            fail += 1; print(f"  NG  {name}: got={got!r} want={want!r}")

    J = lambda *t: datetime(*t, tzinfo=_JST)

    # 偽の DB：title の starts_with と created_time の範囲だけを本物どおりに評価する
    def fake_query(rows):
        def q(filt):
            conds = filt.get("and", [filt])
            out = []
            for r in rows:
                good = True
                for c in conds:
                    if "title" in c and not r["title"].startswith(c["title"]["starts_with"]):
                        good = False
                    ct = c.get("created_time") or {}
                    if "on_or_after" in ct and _z(r["created"]) < ct["on_or_after"]:
                        good = False
                    if "on_or_before" in ct and _z(r["created"]) > ct["on_or_before"]:
                        good = False
                if good:
                    out.append({"properties": {"Title": {"title": [{"plain_text": r["title"]}]}}})
            return out
        return q

    check("直近の正午：19:05 JST → 当日 12:00", last_noon_utc(J(2026, 11, 20, 19, 5)), J(2026, 11, 20, 12, 0))
    check("直近の正午：05:18 JST → 前日 12:00", last_noon_utc(J(2026, 11, 21, 5, 18)), J(2026, 11, 20, 12, 0))

    # 今朝 03:57 にできた「11月20日」（旧方式＝実行日で題）
    morning = {"title": "【海外ヘッドライン】2026年11月20日 世界の医療・保健ニュース5本",
               "created": J(2026, 11, 20, 3, 57)}
    db = [morning]
    q = fake_query(db)
    check("今朝 03:57 の1本は 19:05 の回を止めない", already_made_recently(q, J(2026, 11, 20, 19, 5)), False)
    day = datetime(2026, 11, 20).date()                         # 19:05 の回の記事の日も 11/20
    t2 = make_title(day, 3, titles_for_day(q, day))
    check("同じ日付の2本目は「その2」", t2, "【海外ヘッドライン】2026年11月20日その2 世界の医療・保健ニュース3本")
    check("同じ題（日付の部分）が2本できない", len({x.split(" ")[0] for x in [morning["title"], t2]}), 2)
    # cron が 05:18 に遅れて動いて作った1本も、その日の 19:05 の回を止めない（14時間窓だと止まった）
    check("05:18 の1本は 19:05 の回を止めない",
          already_made_recently(fake_query([{"title": morning["title"], "created": J(2026, 11, 20, 5, 18)}]),
                                J(2026, 11, 20, 19, 5)), False)
    db.append({"title": t2, "created": J(2026, 11, 20, 19, 6)})
    check("3本目は「その3」", make_title(day, 1, titles_for_day(q, day)).split(" ")[0],
          "【海外ヘッドライン】2026年11月20日その3")
    check("11月2日 と 11月20日 を取り違えない",
          make_title(datetime(2026, 11, 2).date(), 1, ["【海外ヘッドライン】2026年11月20日 世界の医療・保健ニュース5本"]).split(" ")[0],
          "【海外ヘッドライン】2026年11月2日")
    # 19:05 の回の後、cron が翌朝 05:18 に遅れて動いても作らない
    check("翌朝 05:18 の cron は作らない", already_made_recently(q, J(2026, 11, 21, 5, 18)), True)
    check("翌日 19:05 は作る", already_made_recently(q, J(2026, 11, 21, 19, 5)), False)

    # 本物の _strict_query：requests.post を差し替えて 400 を返させる → 例外
    import requests

    class _R:
        def __init__(self, code): self.status_code = code
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code} Client Error")
        def json(self): return {"results": []}

    class _NW:
        notion_base, database_id, notion_headers = "https://invalid", "db", {}

    orig = requests.post
    try:
        requests.post = lambda *a, **k: _R(400)
        try:
            _strict_query(_NW(), recent_digest_filter(J(2026, 11, 20, 19, 5)))
            check("_strict_query：400 は例外（0件にしない）", "例外なし", "例外")
        except requests.HTTPError:
            check("_strict_query：400 は例外（0件にしない）", "例外", "例外")
        try:
            already_made_recently(lambda f: _strict_query(_NW(), f), J(2026, 11, 20, 19, 5))
            check("400 のとき作る側に倒れない", "作る判定", "例外")
        except requests.HTTPError:
            check("400 のとき作る側に倒れない", "例外", "例外")
        requests.post = lambda *a, **k: _R(200)
        check("_strict_query：200 は結果を返す", _strict_query(_NW(), {}), [])
    finally:
        requests.post = orig

    # 題の日付＝記事の Date(Search) のいちばん新しい日（JST）
    def pg(v):
        return {"properties": {"Date(Search)": {"type": "date", "date": ({"start": v} if v else None)}}}
    late = datetime(2026, 11, 20, 3, 56, tzinfo=_JST)          # 遅れた cron（11/19 分を束ねる）
    check("11/19 の記事を翌朝束ねても 11/19",
          digest_date([pg("2026-11-19T10:00:00.000+09:00"), pg("2026-11-18T23:00:00.000+09:00")], late),
          (datetime(2026, 11, 19).date(), 0))
    check("UTC の時刻は JST の日に直す（11/19 15:30Z＝11/20 00:30 JST）",
          digest_date([pg("2026-11-19T15:30:00.000Z")], late), (datetime(2026, 11, 20).date(), 0))
    check("日付だけの値も読む", digest_date([pg("2026-11-18")], late), (datetime(2026, 11, 18).date(), 0))
    check("Date(Search) が無い記事は外す（残りで決める）",
          digest_date([pg(None), pg("2026-11-19T09:00:00.000+09:00")], late),
          (datetime(2026, 11, 19).date(), 1))
    check("全部に無ければ実行日（JST）",
          digest_date([pg(None), {"properties": {}}], late), (datetime(2026, 11, 20).date(), 2))

    print(f"\nself-test: {ok}件合格 / {fail}件不合格")
    return 0 if fail == 0 else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    nw = NotionWordPressUploader()
    import requests

    if already_made_recently(lambda q: _strict_query(nw, q), datetime.now(timezone.utc)):
        logger.info("直近の JST 12:00 以降にまとめを作成済み（1日1本）。今回は作りません")
        return

    pages = nw.query_database({
        "and": [
            {"property": "Status(Podcast)", "status": {"equals": "音声化待ち"}},
            {"or": [{"property": "Category", "select": {"equals": c}} for c in INTL]},
        ]
    })
    logger.info(f"海外まとめ対象: {len(pages)}件")
    if not pages:
        return

    items, page_ids, links = [], [], []
    for p in pages:
        title = nw.get_property_value(p, 'Title') or ''
        blocks = nw.fetch_page_blocks(p['id'])
        body = nw.converter.convert(blocks) if blocks else ''
        items.append((title, body))
        page_ids.append(p['id'])
        weburl = (p['properties'].get('URL(Web)', {}) or {}).get('url') or ''
        links.append((title, weburl))

    headlines = gemini_headlines(items)

    day, missing = digest_date(pages, datetime.now(_JST))
    if missing:
        logger.warning(f"⚠ Date(Search) が無い記事 {missing}/{len(pages)} 件"
                       + ("（全部に無いので実行日を題に）" if missing == len(pages) else "（日付の計算から外した）"))
    ep_title = make_title(day, len(items), titles_for_day(lambda q: _strict_query(nw, q), day))
    lines = [f"本日の海外の医療・保健ニュースを、まとめてお届けします。"]
    for i, h in enumerate(headlines):
        lines.append(h.strip())
        if i < len(headlines) - 1:
            lines.append("続いてのニュースです。")  # 見出し間に一拍おき、速度・内容の混線を防ぐ
    lines.append("詳しくは、クロスヘルス公式サイトの記事をご覧ください。")

    # まとめ行をDBに作成（台本は自ページのchildrenに置き、Script(Podcast)は自分を指す）
    children = [{"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"text": {"content": ln[:1900]}}]}} for ln in lines]
    headers = nw.notion_headers
    resp = requests.post("https://api.notion.com/v1/pages", headers=headers, timeout=60, json={
        "parent": {"database_id": nw.database_id},
        "properties": {
            "Title": {"title": [{"text": {"content": ep_title}}]},
            "Status(Podcast)": {"status": {"name": "音声化待ち"}},
        },
        "children": children,
    })
    resp.raise_for_status()
    new_page = resp.json()
    page_url = new_page.get("url", "")
    desc_lines = ["本日の海外の医療・保健ニュースまとめ。各記事の全文はこちら："]
    for t, u in links:
        desc_lines.append(f"・{t[:60]}")
        if u:
            desc_lines.append(f"　▶️{u}")
    desc_lines.append("「医療政策ウォッチャー」公式サイト ▶️https://www.crosshealthjp.org/watcher")
    requests.patch(f"https://api.notion.com/v1/pages/{new_page['id']}", headers=headers, timeout=30,
                   json={"properties": {
                       "Script(Podcast)": {"url": page_url},
                       "PodcastDescription": {"rich_text": [{"text": {"content": "\n".join(desc_lines)[:1900]}}]},
                   }}).raise_for_status()
    logger.info(f"✅ まとめエピソード作成: {ep_title}")

    # 元記事は個別音声化しない＝完了へ
    for pid in page_ids:
        try:
            requests.patch(f"https://api.notion.com/v1/pages/{pid}", headers=headers, timeout=30,
                           json={"properties": {"Status(Podcast)": {"status": {"name": "完了"}}}}).raise_for_status()
        except Exception as e:
            logger.warning(f"  ⚠ 元記事ステータス更新失敗: {e}")
    logger.info(f"✅ 元記事{len(page_ids)}件を完了に（音声はまとめ側で配信）")


if __name__ == "__main__":
    main()
