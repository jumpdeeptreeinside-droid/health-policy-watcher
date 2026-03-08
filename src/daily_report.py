#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日次進捗レポート送信スクリプト

機能:
- Notionデータベースの各ステータス件数を集計
- 毎朝 JST 8:00 にメールで進捗レポートを送信

実行方法:
    python daily_report.py
"""

import sys
import io
import os
import logging
import requests
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

_JST = timezone(timedelta(hours=9))

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 設定読み込み（環境変数優先、なければ config.py）
# ──────────────────────────────────────────────
try:
    NOTION_API_KEY     = os.environ.get('NOTION_API_KEY')
    NOTION_DATABASE_ID = os.environ.get('NOTION_DATABASE_ID')
    GMAIL_ADDRESS      = os.environ.get('GMAIL_ADDRESS', '')
    GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')

    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        import config
        NOTION_API_KEY     = NOTION_API_KEY     or config.NOTION_API_KEY
        NOTION_DATABASE_ID = NOTION_DATABASE_ID or config.NOTION_DATABASE_ID
        if not GMAIL_ADDRESS:
            GMAIL_ADDRESS = getattr(config, 'GMAIL_ADDRESS', '')
        if not GMAIL_APP_PASSWORD:
            GMAIL_APP_PASSWORD = getattr(config, 'GMAIL_APP_PASSWORD', '')
        logger.info("config.py から設定を読み込みました")
    else:
        logger.info("環境変数から設定を読み込みました")

except ImportError:
    logger.error("config.py が見つからず、環境変数も未設定です。処理を中断します。")
    sys.exit(1)

NOTIFY_TO: List[str] = [
    "jump.deep.tree.inside@gmail.com",
    "kremlin006@gmail.com",
]

_NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
_NOTION_BASE = "https://api.notion.com/v1"


# ──────────────────────────────────────────────
# Notion クエリヘルパー
# ──────────────────────────────────────────────

def count_by_filter(filter_conditions: Dict) -> int:
    """フィルター条件に合致するページの総件数を返す（ページネーション対応）"""
    url     = f"{_NOTION_BASE}/databases/{NOTION_DATABASE_ID}/query"
    payload = {"filter": filter_conditions, "page_size": 100}
    total   = 0

    while True:
        try:
            resp = requests.post(url, headers=_NOTION_HEADERS, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            total += len(data.get('results', []))
            if data.get('has_more'):
                payload['start_cursor'] = data['next_cursor']
            else:
                break
        except Exception as e:
            logger.error(f"クエリエラー: {e}")
            break

    return total


def _content_status(name: str) -> Dict:
    return {"property": "Status(コンテンツ作成)", "status": {"equals": name}}

def _web_status(name: str) -> Dict:
    return {"property": "Status(Web)", "status": {"equals": name}}

def _podcast_status(name: str) -> Dict:
    return {"property": "Status(Podcast)", "status": {"equals": name}}


# ──────────────────────────────────────────────
# レポート集計
# ──────────────────────────────────────────────

def build_report() -> Dict:
    """各ステータスの件数を集計してレポートデータを返す"""
    now_jst  = datetime.now(_JST)
    week_ago = (now_jst - timedelta(days=7)).strftime('%Y-%m-%d')

    logger.info("各ステータスの件数を集計中...")

    data = {
        "timestamp": now_jst.strftime('%Y-%m-%d %H:%M JST'),

        # コンテンツパイプライン
        "未着手":           count_by_filter(_content_status("情報選択未着手")),
        "ストック":         count_by_filter(_content_status("ストック")),
        "執筆待ちURL":      count_by_filter(_content_status("執筆待ち(URL)")),
        "執筆待ちPDF":      count_by_filter(_content_status("執筆待ち(PDF)")),
        "ファクトチェック": count_by_filter(_content_status("ファクトチェック待ち")),

        # 完了（今週）: Date(W-complete) が7日以内のもの
        "完了今週": count_by_filter({
            "and": [
                _content_status("完了"),
                {"property": "Date(W-complete)", "date": {"on_or_after": week_ago}},
            ]
        }),

        # 配信パイプライン
        "投稿待ち":   count_by_filter(_web_status("投稿待ち")),
        "音声化待ち": count_by_filter(_podcast_status("音声化待ち")),

        # 今週の新着: Date(Search) が7日以内
        "今週新着": count_by_filter({
            "property": "Date(Search)",
            "date": {"on_or_after": week_ago},
        }),
    }

    logger.info("集計完了")
    return data


# ──────────────────────────────────────────────
# メール本文フォーマット
# ──────────────────────────────────────────────

def format_report(data: Dict) -> str:
    return (
        f"【医療政策ウォッチャー】日次進捗レポート\n"
        f"{data['timestamp']}\n"
        f"\n"
        f"■ コンテンツパイプライン\n"
        f"  情報選択未着手      : {data['未着手']:>3}件\n"
        f"  ストック            : {data['ストック']:>3}件\n"
        f"  執筆待ち(URL)       : {data['執筆待ちURL']:>3}件\n"
        f"  執筆待ち(PDF)       : {data['執筆待ちPDF']:>3}件\n"
        f"  ファクトチェック待ち: {data['ファクトチェック']:>3}件\n"
        f"  完了（今週）        : {data['完了今週']:>3}件\n"
        f"\n"
        f"■ 配信パイプライン\n"
        f"  投稿待ち(WordPress) : {data['投稿待ち']:>3}件\n"
        f"  音声化待ち(Podcast) : {data['音声化待ち']:>3}件\n"
        f"\n"
        f"■ 今週の新着記事\n"
        f"  新規収集            : {data['今週新着']:>3}件\n"
        f"\n"
        f"─────────────────────────────\n"
        f"次回レポート: 明日 08:00 JST\n"
    )


# ──────────────────────────────────────────────
# メール送信
# ──────────────────────────────────────────────

def send_report(body: str) -> None:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.error("Gmail が未設定のため送信をスキップします")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "【日次レポート】医療政策ウォッチャー進捗"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = ", ".join(NOTIFY_TO)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_TO, msg.as_string())
        logger.info(f"レポートメール送信完了: {', '.join(NOTIFY_TO)}")
    except Exception as e:
        logger.error(f"メール送信失敗: {e}")


# ──────────────────────────────────────────────
# エントリーポイント
# ──────────────────────────────────────────────

def main():
    logger.info("=" * 50)
    logger.info("日次進捗レポートを開始します")
    logger.info("=" * 50)

    try:
        data = build_report()
        body = format_report(data)
        logger.info("\n" + body)
        send_report(body)
    except Exception as e:
        logger.error(f"予期せぬエラー: {e}")
        import traceback
        traceback.print_exc()

    logger.info("=" * 50)
    logger.info("処理完了")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
