#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""既存の「記事選択未着手」にCategory+AI採用予測を一括付与するバックフィル（2026-07-07）。
ステータスは変更しない。使い方: python3 src/backfill_predictions.py [--dry-run]
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('WORDPRESS_URL', 'x')
os.environ.setdefault('WORDPRESS_USERNAME', 'x')
os.environ.setdefault('WORDPRESS_APP_PASSWORD', 'x')
import config
from fetch_news_to_notion import CATEGORY_LABELS, score_articles_with_gemini, NewsArticle

HEADERS = {"Authorization": f"Bearer {config.NOTION_API_KEY}",
           "Notion-Version": "2022-06-28", "Content-Type": "application/json"}


def notion(path, method='GET', body=None):
    req = urllib.request.Request(f"https://api.notion.com/v1{path}",
                                 data=json.dumps(body).encode() if body else None,
                                 method=method, headers=HEADERS)
    return json.load(urllib.request.urlopen(req, timeout=60))


def main():
    dry = '--dry-run' in sys.argv
    items, cursor = [], None
    while True:
        body = {"page_size": 100,
                "filter": {"property": "Status(コンテンツ作成)", "status": {"equals": "記事選択未着手"}}}
        if cursor:
            body["start_cursor"] = cursor
        r = notion(f"/databases/{config.NOTION_DATABASE_ID}/query", 'POST', body)
        items.extend(r['results'])
        if not r.get('has_more'):
            break
        cursor = r['next_cursor']
    print(f"記事選択未着手: {len(items)}件")

    targets = []
    for p in items:
        title = ''.join(x['plain_text'] for x in p['properties'].get('Title', {}).get('title', []))
        cat_done = p['properties'].get('Category', {}).get('select')
        pick_done = p['properties'].get('AI採用予測', {}).get('select')
        if title and not (cat_done and pick_done):
            targets.append((p['id'], title))
    print(f"予測対象（未付与のみ）: {len(targets)}件")
    if dry or not targets:
        return

    stats = {"おすすめ": 0, "見送り": 0, "未分類": 0}
    for i in range(0, len(targets), 50):
        batch = targets[i:i+50]
        arts = [NewsArticle(title=t, url='', source='') for _, t in batch]
        scores = score_articles_with_gemini(arts)
        for (page_id, title), sc in zip(batch, scores):
            props = {}
            label = CATEGORY_LABELS.get(sc.get('cat'))
            if label:
                props["Category"] = {"select": {"name": label}}
            if "pick" in sc:
                pick = "おすすめ" if sc["pick"] else "見送り"
                props["AI採用予測"] = {"select": {"name": pick}}
                stats[pick] += 1
            else:
                stats["未分類"] += 1
            if not props:
                continue
            try:
                notion(f"/pages/{page_id}", 'PATCH', {"properties": props})
            except Exception as e:
                print(f"  ⚠ 更新失敗 {title[:30]}: {e}")
            time.sleep(0.35)
        print(f"  {min(i+50, len(targets))}/{len(targets)} 完了")
    print(f"\n結果: おすすめ{stats['おすすめ']}件 / 見送り{stats['見送り']}件 / 未分類{stats['未分類']}件")


if __name__ == "__main__":
    main()
