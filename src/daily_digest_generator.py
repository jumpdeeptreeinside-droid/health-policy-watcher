#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""海外ヘッドラインまとめ生成（2026-07-07 木内さん設計）

国際系（国際・日本関連／国際・その他）の記事は個別に音声化せず、
1日1本の「海外ヘッドライン」エピソードに束ねる。記事（Web）は従来どおり個別に詳細。

フロー（毎日 JST 19:05・GitHub Actions）:
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
        f"JSON配列のみ出力（記事数={len(items)}・インデックス対応）: [\"ヘッドライン1\", ...]\n\n" + blocks
    )
    raw = model.generate_content(prompt).text.strip().strip('`').removeprefix('json').strip()
    out = json.loads(raw)
    if len(out) != len(items):
        raise ValueError(f"件数不一致: {len(out)} vs {len(items)}")
    return out


def main():
    nw = NotionWordPressUploader()
    import requests

    pages = nw.query_database({
        "and": [
            {"property": "Status(Podcast)", "status": {"equals": "音声化待ち"}},
            {"or": [{"property": "Category", "select": {"equals": c}} for c in INTL]},
        ]
    })
    logger.info(f"海外まとめ対象: {len(pages)}件")
    if not pages:
        return

    items, page_ids = [], []
    for p in pages:
        title = nw.get_property_value(p, 'Title') or ''
        blocks = nw.fetch_page_blocks(p['id'])
        body = nw.converter.convert(blocks) if blocks else ''
        items.append((title, body))
        page_ids.append(p['id'])

    headlines = gemini_headlines(items)

    today = datetime.now(_JST)
    ep_title = f"【海外ヘッドライン】{today.strftime('%Y年%-m月%-d日')} 世界の医療・保健ニュース{len(items)}本"
    lines = [f"本日の海外の医療・保健ニュースを、まとめてお届けします。"]
    for i, h in enumerate(headlines):
        lines.append(h.strip())
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
    requests.patch(f"https://api.notion.com/v1/pages/{new_page['id']}", headers=headers, timeout=30,
                   json={"properties": {"Script(Podcast)": {"url": page_url}}}).raise_for_status()
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
