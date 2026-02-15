#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notionデータベースのステータス自動更新スクリプト

機能:
1. Status(コンテンツ作成)が「完了」の場合:
   - Status(Web)を「投稿待ち」に自動変更
   - Status(Podcast)を「音声化待ち」に自動変更

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
        - Status(Web)を「投稿待ち」に変更
        - Status(Podcast)を「音声化待ち」に変更

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
        
        for page in pages:
            page_id = page.get('id')
            title = self.get_property_value(page, 'Title') or 'タイトルなし'
            
            # 現在のステータスを確認
            status_web = self.get_property_value(page, 'Status(Web)')
            status_podcast = self.get_property_value(page, 'Status(Podcast)')
            
            logger.info(f"\n処理中: {title[:50]}...")
            logger.info(f"  現在のStatus(Web): {status_web}")
            logger.info(f"  現在のStatus(Podcast): {status_podcast}")
            
            # 更新するプロパティを準備
            properties_to_update = {}
            
            # Status(Web)が「デフォルト」の場合のみ「投稿待ち」に変更
            if not status_web or status_web == "デフォルト":
                properties_to_update["Status(Web)"] = {
                    "status": {
                        "name": "投稿待ち"
                    }
                }
                logger.info("  → Status(Web)を「投稿待ち」に変更")
            
            # Status(Podcast)が「デフォルト」の場合のみ「音声化待ち」に変更
            if not status_podcast or status_podcast == "デフォルト":
                properties_to_update["Status(Podcast)"] = {
                    "status": {
                        "name": "音声化待ち"
                    }
                }
                logger.info("  → Status(Podcast)を「音声化待ち」に変更")
            
            # 更新実行
            if properties_to_update:
                if self.update_page_properties(page_id, properties_to_update):
                    logger.info("  ✅ 更新成功")
                    update_count += 1
                else:
                    logger.error("  ❌ 更新失敗")
            else:
                logger.info("  ⏭️  更新不要（既に適切なステータス）")
        
        return update_count

    def process_date_recording(self) -> int:
        """
        日付の自動記録を処理
        
        注意: この機能は現在の実装では完全には実現できません。
        なぜなら、ステータスの「変更前」の値を知るためには、
        別途履歴データベースを持つか、定期的にスナップショットを取る必要があるためです。
        
        代替案: ステータス変更時に日付が空欄の場合のみ、現在日付を記録する
        
        Returns:
            処理したページ数
        """
        logger.info("\n日付の自動記録処理を開始...")
        
        # すべてのページを取得
        all_pages = self.query_database()
        update_count = 0
        
        now = datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d')
        
        for page in all_pages:
            page_id = page.get('id')
            title = self.get_property_value(page, 'Title') or 'タイトルなし'
            
            properties_to_update = {}
            
            # Date(Select): Status(コンテンツ作成)が「執筆待ち」で日付が空の場合
            status_content = self.get_property_value(page, 'Status(コンテンツ作成)')
            date_select = self.get_property_value(page, 'Date(Select)')
            
            if status_content in ["執筆待ち(URL)", "執筆待ち(PDF)"] and not date_select:
                properties_to_update["Date(Select)"] = {
                    "date": {
                        "start": now
                    }
                }
                logger.info(f"{title[:30]}... → Date(Select)を記録")
            
            # Date(W-complete): Status(Web)が「スケジュール待ち」で日付が空の場合
            status_web = self.get_property_value(page, 'Status(Web)')
            date_w_complete = self.get_property_value(page, 'Date(W-complete)')
            
            if status_web == "スケジュール待ち" and not date_w_complete:
                properties_to_update["Date(W-complete)"] = {
                    "date": {
                        "start": now
                    }
                }
                logger.info(f"{title[:30]}... → Date(W-complete)を記録")
            
            # Date(P-complete): Status(Podcast)が「完了」で日付が空の場合
            status_podcast = self.get_property_value(page, 'Status(Podcast)')
            date_p_complete = self.get_property_value(page, 'Date(P-complete)')
            
            if status_podcast == "完了" and not date_p_complete:
                properties_to_update["Date(P-complete)"] = {
                    "date": {
                        "start": now
                    }
                }
                logger.info(f"{title[:30]}... → Date(P-complete)を記録")
            
            # 更新実行
            if properties_to_update:
                if self.update_page_properties(page_id, properties_to_update):
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
