#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pro検索エンジンのインデックス構築（Phase1・2026-07-07）

~/crosshealth_search.db（SQLite FTS5 trigram）に2つの検索コーパスを構築:
  1. news    : Notion全記事（タイトル・分類・日付・ソース・サイトURL）— 毎日再同期
  2. chuikyo : 中医協議事録アーカイブ（~/chuikyo_archive/ 466会合・1,607万字）— 差分同期

使い方: python3 src/build_search_index.py [--news-only|--chuikyo-only]
"""
import glob
import json
import os
import sqlite3
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

DB = os.path.expanduser("~/crosshealth_search.db")
ARCHIVE = os.path.expanduser("~/chuikyo_archive")
H = {"Authorization": f"Bearer {config.NOTION_API_KEY}",
     "Notion-Version": "2022-06-28", "Content-Type": "application/json"}


def notion_query(body):
    req = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{config.NOTION_DATABASE_ID}/query",
        data=json.dumps(body).encode(), method='POST', headers=H)
    return json.load(urllib.request.urlopen(req, timeout=60))


def sync_news(db):
    db.execute("DROP TABLE IF EXISTS news")
    db.execute("DROP TABLE IF EXISTS news_fts")
    db.execute("""CREATE TABLE news (
        id TEXT PRIMARY KEY, title TEXT, date TEXT, category TEXT,
        source_url TEXT, web_url TEXT, picked INTEGER)""")
    db.execute("CREATE VIRTUAL TABLE news_fts USING fts5(title, content=news, tokenize='trigram')")

    total, cursor = 0, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = notion_query(body)
        rows = []
        for p in r['results']:
            props = p['properties']
            title = ''.join(x['plain_text'] for x in props.get('Title', {}).get('title', []))
            if not title:
                continue
            st = (props.get('Status(コンテンツ作成)', {}).get('status') or {}).get('name', '')
            rows.append((
                p['id'], title,
                (props.get('Date(Search)', {}).get('date') or {}).get('start', ''),
                (props.get('Category', {}).get('select') or {}).get('name', ''),
                props.get('URL(Source)', {}).get('url') or '',
                props.get('URL(Web)', {}).get('url') or '',
                1 if st in ('完了', 'ファクトチェック待ち', '執筆待ち(url)', '執筆待ち(pdf)') else 0,
            ))
        db.executemany("INSERT OR REPLACE INTO news VALUES (?,?,?,?,?,?,?)", rows)
        total += len(rows)
        if not r.get('has_more'):
            break
        cursor = r['next_cursor']
    db.execute("INSERT INTO news_fts(news_fts) VALUES('rebuild')")
    db.commit()
    print(f"✓ news: {total}件を同期")


def sync_chuikyo(db):
    db.execute("""CREATE TABLE IF NOT EXISTS chuikyo (
        slug TEXT PRIMARY KEY, kai INTEGER, title TEXT, url TEXT, chars INTEGER, body TEXT)""")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chuikyo_fts USING fts5(body, content=chuikyo, tokenize='trigram')")
    have = {r[0] for r in db.execute("SELECT slug FROM chuikyo")}
    added = 0
    for f in glob.glob(os.path.join(ARCHIVE, "*.json")):
        slug = os.path.basename(f)[:-5]
        if slug in have:
            continue
        d = json.load(open(f, encoding='utf-8'))
        db.execute("INSERT INTO chuikyo VALUES (?,?,?,?,?,?)",
                   (slug, d.get('kai'), d.get('title', ''), d.get('url', ''),
                    d.get('chars', 0), d.get('text', '')))
        added += 1
    if added:
        db.execute("INSERT INTO chuikyo_fts(chuikyo_fts) VALUES('rebuild')")
    db.commit()
    n, chars = db.execute("SELECT COUNT(*), SUM(chars) FROM chuikyo").fetchone()
    print(f"✓ chuikyo: 追加{added}件（計{n}会合・{(chars or 0)//10000}万字）")


def main():
    db = sqlite3.connect(DB)
    if '--chuikyo-only' not in sys.argv:
        sync_news(db)
    if '--news-only' not in sys.argv:
        sync_chuikyo(db)
    db.close()
    print(f"→ {DB} ({os.path.getsize(DB)//1024//1024}MB)")


if __name__ == "__main__":
    main()
