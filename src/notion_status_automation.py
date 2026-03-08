#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notionデータベースのステータス自動更新スクリプト

機能:
1. Status(コンテンツ作成)が「コンテンツ作成完了」の場合:
   - Status(Web)を「-」（デフォルト）→「投稿待ち」に自動変更
   - Status(Podcast)を「-」（デフォルト）→「音声化待ち」に自動変更

2. 日付の自動記録:
   - Date(Select): Status(コンテンツ作成)が「デフォルト」→「執筆待ち」に変更時
   - Date(W-complete): Status(Web)が「投稿待ち」→「スケジュール待ち」に変更時
   - Date(P-complete): Status(Podcast)が「音声化待ち」→「完了」に変更時

実行方法:
    python notion_status_automation.py
"""

import sys
import io
import os
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

import requests

# Windows環境での文字エンコーディング問題を解決
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('notion_automation.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 設定読み込み（環境変数優先、なければconfig.py）
try:
    NOTION_API_KEY = os.environ.get('NOTION_API_KEY')
    NOTION_DATABASE_ID = os.environ.get('NOTION_DATABASE_ID')
    
    if not NOTION_API_KEY or not NOTION_DATABASE_ID:
        import config
        NOTION_API_KEY = config.NOTION_API_KEY
        NOTION_DATABASE_ID = config.NOTION_DATABASE_ID
        logger.info("config.py から設定を読み込みました")
    else:
        logger.info("環境変数から設定を読み込みました")
        
except ImportError:
    logger.error("config.py ファイルが見つからず、環境変数も設定されていません。")
    sys.exit(1)

GMAIL_ADDRESS     = os.environ.get('GMAIL_ADDRESS', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
NOTIFY_TO = "jump.deep.tree.inside@gmail.com"


def send_podcast_notification(articles: list) -> None:
    """音声化待ちになった記事の通知メールを送信する"""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.warning("Gmail未設定のため通知メールをスキップします")
        return

    article_list = "\n".join(
        f"  [{i+1}] {art['title']}" for i, art in enumerate(articles)
    )
    body = f"""\
【医療政策ウォッチャー】音声化待ち記事のお知らせ

以下の記事の音声収録をお願いします。

■ 音声化待ち記事（{len(articles)}件）
{article_list}

収録完了後、Notionで Status(Podcast) を「完了」に変更してください。
"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【要対応】音声化待ち記事 {len(articles)}件"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_TO, msg.as_string())
        logger.info(f"音声化待ち通知メール送信完了: {NOTIFY_TO}")
    except Exception as e:
        logger.error(f"メール送信失敗: {e}")


class NotionAutomation:
    """Notionデータベースのステータス自動更新を行うクラス"""

    def __init__(self):
        self.api_key = NOTION_API_KEY
        self.database_id = NOTION_DATABASE_ID
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        self.base_url = "https://api.notion.com/v1"

    def query_database(self, filter_conditions: Optional[Dict] = None) -> List[Dict]:
        """
        Notionデータベースをクエリしてページリストを取得

        Args:
            filter_conditions: フィルター条件（オプション）

        Returns:
            ページのリスト
        """
        url = f"{self.base_url}/databases/{self.database_id}/query"
        
        payload = {}
        if filter_conditions:
            payload["filter"] = filter_conditions
        
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get('results', [])
        except Exception as e:
            logger.error(f"データベースクエリエラー: {e}")
            return []

    def get_property_value(self, page: Dict, property_name: str) -> Optional[str]:
        """
        ページのプロパティ値を取得

        Args:
            page: Notionページオブジェクト
            property_name: プロパティ名

        Returns:
            プロパティ値（文字列）
        """
        try:
            prop = page.get('properties', {}).get(property_name, {})
            prop_type = prop.get('type')
            
            if prop_type == 'status':
                status = prop.get('status')
                return status.get('name') if status else None
            elif prop_type == 'date':
                date = prop.get('date')
                return date.get('start') if date else None
            elif prop_type == 'title':
                title_array = prop.get('title', [])
                return title_array[0].get('plain_text') if title_array else None
            
            return None
        except Exception as e:
            logger.debug(f"プロパティ取得エラー ({property_name}): {e}")
            return None

    def update_page_properties(self, page_id: str, properties: Dict) -> bool:
        """
        ページのプロパティを更新

        Args:
            page_id: ページID
            properties: 更新するプロパティの辞書

        Returns:
            成功時True、失敗時False
        """
        url = f"{self.base_url}/pages/{page_id}"
        
        try:
            response = requests.patch(url, headers=self.headers, json={"properties": properties})
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"ページ更新エラー: {e}")
            return False

    def process_content_complete_status(self) -> int:
        """
        Status(コンテンツ作成)が「完了」のページを処理

        処理内容:
        - Status(Web)が「-」（デフォルト）の場合のみ「投稿待ち」に変更
        - Status(Podcast)が「-」（デフォルト）の場合のみ「音声化待ち」に変更

        Returns:
            処理したページ数
        """
        logger.info("Status(コンテンツ作成)が「完了」のページを検索中...")

        # フィルター: Status(コンテンツ作成) = "完了"
        filter_conditions = {
            "property": "Status(コンテンツ作成)",
            "status": {
                "equals": "完了"
            }
        }
        
        pages = self.query_database(filter_conditions)
        logger.info(f"{len(pages)} 件のページが見つかりました")

        update_count = 0
        podcast_notified: list = []  # 音声化待ちになった記事リスト

        for page in pages:
            page_id = page.get('id')
            title = self.get_property_value(page, 'Title') or 'タイトルなし'

            # 現在のステータスを確認
            status_web     = self.get_property_value(page, 'Status(Web)')
            status_podcast = self.get_property_value(page, 'Status(Podcast)')

            logger.info(f"\n処理中: {title[:50]}...")
            logger.info(f"  現在のStatus(Web): {status_web}")
            logger.info(f"  現在のStatus(Podcast): {status_podcast}")

            # 更新するプロパティを準備
            properties_to_update = {}
            will_set_podcast = False

            # Status(Web)が「-」（デフォルト）の場合のみ「投稿待ち」に変更
            if not status_web or status_web == "-":
                properties_to_update["Status(Web)"] = {
                    "status": {"name": "投稿待ち"}
                }
                logger.info("  → Status(Web)を「投稿待ち」に変更")

            # Status(Podcast)が「-」（デフォルト）の場合のみ「音声化待ち」に変更
            if not status_podcast or status_podcast == "-":
                properties_to_update["Status(Podcast)"] = {
                    "status": {"name": "音声化待ち"}
                }
                logger.info("  → Status(Podcast)を「音声化待ち」に変更")
                will_set_podcast = True

            # 更新実行
            if properties_to_update:
                if self.update_page_properties(page_id, properties_to_update):
                    logger.info("  ✅ 更新成功")
                    update_count += 1
                    if will_set_podcast:
                        podcast_notified.append({"title": title})
                else:
                    logger.error("  ❌ 更新失敗")
            else:
                logger.info("  ⏭️  更新不要（既に適切なステータス）")

        # 音声化待ちになった記事があればメール通知
        if podcast_notified:
            send_podcast_notification(podcast_notified)

        return update_count

    def process_date_recording(self) -> int:
        """
        日付の自動記録を処理（フィルター活用で効率化）

        全件取得せず、条件に合致するページのみ絞り込んで処理する。

        Returns:
            処理したページ数
        """
        logger.info("\n日付の自動記録処理を開始...")
        update_count = 0
        now = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d')

        # ── 1. Date(Select): 執筆待ちかつ日付未設定 ──────────────
        pages_select = self.query_database({
            "and": [
                {"or": [
                    {"property": "Status(コンテンツ作成)", "status": {"equals": "執筆待ち(URL)"}},
                    {"property": "Status(コンテンツ作成)", "status": {"equals": "執筆待ち(PDF)"}},
                ]},
                {"property": "Date(Select)", "date": {"is_empty": True}},
            ]
        })
        for page in pages_select:
            page_id = page.get('id')
            title = self.get_property_value(page, 'Title') or 'タイトルなし'
            if self.update_page_properties(page_id, {"Date(Select)": {"date": {"start": now}}}):
                logger.info(f"  {title[:30]}... → Date(Select) 記録")
                update_count += 1

        # ── 2. Date(W-complete): スケジュール待ちかつ日付未設定 ──
        pages_w = self.query_database({
            "and": [
                {"property": "Status(Web)", "status": {"equals": "スケジュール待ち"}},
                {"property": "Date(W-complete)", "date": {"is_empty": True}},
            ]
        })
        for page in pages_w:
            page_id = page.get('id')
            title = self.get_property_value(page, 'Title') or 'タイトルなし'
            if self.update_page_properties(page_id, {"Date(W-complete)": {"date": {"start": now}}}):
                logger.info(f"  {title[:30]}... → Date(W-complete) 記録")
                update_count += 1

        # ── 3. Date(P-complete): Podcast完了かつ日付未設定 ───────
        pages_p = self.query_database({
            "and": [
                {"property": "Status(Podcast)", "status": {"equals": "完了"}},
                {"property": "Date(P-complete)", "date": {"is_empty": True}},
            ]
        })
        for page in pages_p:
            page_id = page.get('id')
            title = self.get_property_value(page, 'Title') or 'タイトルなし'
            if self.update_page_properties(page_id, {"Date(P-complete)": {"date": {"start": now}}}):
                logger.info(f"  {title[:30]}... → Date(P-complete) 記録")
                update_count += 1

        return update_count


def main():
    """メイン実行関数"""
    logger.info("=" * 50)
    logger.info("Notionステータス自動更新を開始します")
    logger.info("=" * 50)
    
    try:
        automation = NotionAutomation()
        
        # 1. Status(コンテンツ作成)が「完了」のページを処理
        content_complete_count = automation.process_content_complete_status()
        
        # 2. 日付の自動記録
        date_record_count = automation.process_date_recording()
        
        logger.info("\n" + "=" * 50)
        logger.info("処理完了")
        logger.info(f"ステータス更新: {content_complete_count} 件")
        logger.info(f"日付記録: {date_record_count} 件")
        logger.info("=" * 50)
        
    except Exception as e:
        logger.error(f"予期せぬエラー: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
