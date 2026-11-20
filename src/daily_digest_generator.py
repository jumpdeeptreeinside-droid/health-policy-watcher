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
ONE_PER_DAY_HOURS = 14   # この時間内に作ったまとめがあれば作らない（1日1本を守る）


def recent_digest_filter(now_utc: datetime) -> dict:
    since = (now_utc - timedelta(hours=ONE_PER_DAY_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {"and": [
        {"property": "Title", "title": {"starts_with": DIGEST_PREFIX}},
        {"timestamp": "created_time", "created_time": {"on_or_after": since}},
        {"timestamp": "created_time",
         "created_time": {"on_or_before": now_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")}},
    ]}


def already_made_recently(query, now_utc: datetime) -> bool:
    """🔴 2026-11-20: Mac の launchd から 19:05 に workflow_dispatch を送るようにした。
    GitHub の cron（予定 19:05・実測は8〜10時間遅れて翌04時ごろ）も残すので、
    同じ日に2本目（夜のうちに入った数本だけの回）ができないよう、直近14時間に
    まとめ行を作っていれば作らない。query(filter)->rows。失敗は例外のまま（作らない側に倒れる）。
    同じ記事の二重配信は concurrency（daily_digest.yml）と「元記事を完了に」で防いでいる。"""
    return bool(query(recent_digest_filter(now_utc)))


def _strict_query(nw, filt: dict) -> list:
    """検索。🔴 失敗を0件に見せない（0件だと「まだ無い」と読んで2本目を作ってしまう）。"""
    import requests
    r = requests.post(f"{nw.notion_base}/databases/{nw.database_id}/query",
                      headers=nw.notion_headers, json={"filter": filt, "page_size": 1}, timeout=30)
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

    now = datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc)   # JST 11/21 04:00（遅れた cron）
    f = recent_digest_filter(now)
    check("題の頭で絞る", f["and"][0], {"property": "Title", "title": {"starts_with": "【海外ヘッドライン】"}})
    check("14時間前から", f["and"][1]["created_time"]["on_or_after"], "2026-11-20T05:00:00.000Z")
    check("直近に1本あれば作らない", already_made_recently(lambda q: [{"id": "x"}], now), True)
    check("無ければ作る", already_made_recently(lambda q: [], now), False)

    def boom(q):
        raise RuntimeError("HTTP 400")
    try:
        already_made_recently(boom, now)
        check("検索失敗は例外（作らない側）", "例外なし", "例外")
    except RuntimeError:
        check("検索失敗は例外（作らない側）", "例外", "例外")
    print(f"\nself-test: {ok}件合格 / {fail}件不合格")
    return 0 if fail == 0 else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    nw = NotionWordPressUploader()
    import requests

    if already_made_recently(lambda q: _strict_query(nw, q), datetime.now(timezone.utc)):
        logger.info(f"直近{ONE_PER_DAY_HOURS}時間にまとめを作成済み（1日1本）。今回は作りません")
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

    today = datetime.now(_JST)
    ep_title = f"【海外ヘッドライン】{today.strftime('%Y年%-m月%-d日')} 世界の医療・保健ニュース{len(items)}本"
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
