#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notion監視型コンテンツ自動生成スクリプト

機能:
1. Notionデータベースを監視
2. Status(コンテンツ作成)が「執筆待ち(PDF)」or「執筆待ち(URL)」のページを検出
3. 該当するコンテンツ生成処理を実行:
   - 執筆待ち(PDF): PDFダウンロード → analyze_pdf実行 → ブログ記事・台本生成
   - 執筆待ち(URL): analyze_url実行 → ブログ記事・台本生成
4. 生成したコンテンツをNotionページに追加（children blocks）
5. Status(コンテンツ作成)を「ファクトチェック待ち」に変更

実行方法:
    python notion_content_generator.py
"""

import sys
import io
import os
import logging
import time
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional
import tempfile

import google.generativeai as genai
from bs4 import BeautifulSoup

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
        logging.FileHandler('notion_content_generator.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 設定読み込み（環境変数優先、なければconfig.py）
try:
    NOTION_API_KEY = os.environ.get('NOTION_API_KEY')
    NOTION_DATABASE_ID = os.environ.get('NOTION_DATABASE_ID')
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
    
    if not NOTION_API_KEY or not NOTION_DATABASE_ID or not GEMINI_API_KEY:
        import config
        NOTION_API_KEY = NOTION_API_KEY or config.NOTION_API_KEY
        NOTION_DATABASE_ID = NOTION_DATABASE_ID or config.NOTION_DATABASE_ID
        GEMINI_API_KEY = GEMINI_API_KEY or config.GEMINI_API_KEY
        BLOG_OUTPUT_DIR = config.BLOG_OUTPUT_DIR
        SCRIPT_OUTPUT_DIR = config.SCRIPT_OUTPUT_DIR
        DOWNLOAD_DIR = config.DOWNLOAD_DIR
        GEMINI_MODEL_NAME = getattr(config, 'GEMINI_MODEL_NAME', 'gemini-1.5-flash')
        logger.info("config.py から設定を読み込みました")
    else:
        # 環境変数から読み込む場合は、出力ディレクトリをリポジトリルートに
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        BLOG_OUTPUT_DIR = os.path.join(base_dir, 'output', 'blog')
        SCRIPT_OUTPUT_DIR = os.path.join(base_dir, 'output', 'script')
        DOWNLOAD_DIR = os.path.join(base_dir, 'downloads')
        GEMINI_MODEL_NAME = os.environ.get('GEMINI_MODEL_NAME', 'gemini-1.5-flash')
        logger.info("環境変数から設定を読み込みました")
        logger.info(f"  出力先: {BLOG_OUTPUT_DIR}")
    
    # 出力ディレクトリを作成
    os.makedirs(BLOG_OUTPUT_DIR, exist_ok=True)
    os.makedirs(SCRIPT_OUTPUT_DIR, exist_ok=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        
except (ImportError, AttributeError) as e:
    logger.error(f"設定の読み込みに失敗: {e}")
    sys.exit(1)

# Gemini API設定は後ほど（GEMINI_MODEL_NAMEの読み込み後に実行）
GEMINI_MODEL_NAME = None  # 初期化

# プロンプト定義（analyze_url.py / analyze_pdf.py から移植）
PROMPT_BLOG = """
# 役割設定
あなたは、厚生労働省の医療政策資料分析のプロフェッショナルです。

# 出力内容
提供された資料を統合的に分析し、ブログ記事を作成してください。

## ブログ記事（医療政策ニュース記事）

### 構成
以下の流れで構成し、全体で1,500〜2,000文字程度にまとめてください。

1. **導入:** 記事の概要と全体的なトーンを簡潔に。
2. **主要な論点・合意事項（重要な変更点を3つ）:**
   - それぞれの論点について事実を伝えた後、必ず**「政策的な含意（何が動きそうか／現場の実務にどう影響するか）」**を一文で補足して解説してください。
3. **結び:** 今後のスケジュールや注視すべき点。

### 執筆ルール（音声読み上げ用）
- **文体:** ニュースキャスターが読むための、平易でリズムの良い「話し言葉（デスマス調）」にしてください。
- **改行（最重要）:** **読み上げソフトの仕様上、句点（。）が来るたびに必ず改行を入れてください。**
- **一文の長さ:** 息継ぎがしやすいよう短めにし、同じ語尾（〜です）が連続しないよう変化をつけてください。
- **事実性:** ニュースなので、感情的にならず、事実を淡々と伝えてください。固有名詞や数字は変更しないでください。

### 出力フォーマット
- 記事のタイトルのみを出力してください（見出し1として # で記載）
- 本文（マークダウン形式だが、装飾は最小限に）
- 数字・英語は「半角」、記号は「全角」を使用してください。
- 段落が変わる箇所には空行を入れてください。

# 重要な出力ルール
1. **入力された資料に含まれていない情報は、絶対に付け足さないでください。**
2. 事実関係（数字、固有名詞、日付）を勝手に変更しないでください。
3. 前置きや挨拶（「はい、作成します」等）は一切不要です。
4. マークダウン形式で出力してください。
5. 絵文字や顔文字は使用しないでください。
"""

PROMPT_PODCAST = """
# 役割設定
あなたは、ポッドキャスト番組「医療政策ウォッチャー」の台本作成者です。

# 出力内容
提供された資料を基に、ポッドキャスト用台本を作成してください。

## ポッドキャスト台本

### 構成
1. **オープニング:** 簡潔な導入
2. **本編:** 資料の要点を対話形式で解説（15分程度の内容）
3. **まとめ:** 今後の注目点

### 執筆ルール
- **文体:** 話し言葉（デスマス調）で自然な会話調
- **改行:** 句点（。）ごとに改行
- **長さ:** 音声読み上げで12〜15分程度

### 出力フォーマット
- タイトルのみ（見出し1として # で記載）
- 台本本文（マークダウン形式）

# 重要な出力ルール
1. 資料にない情報は追加しない
2. 事実関係を変更しない
3. 前置きや挨拶は不要
4. マークダウン形式で出力
"""


class NotionContentGenerator:
    """Notion監視型コンテンツ生成クラス"""

    def __init__(self):
        self.api_key = NOTION_API_KEY
        self.database_id = NOTION_DATABASE_ID
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        self.base_url = "https://api.notion.com/v1"
        
        # Gemini API設定
        genai.configure(api_key=GEMINI_API_KEY)
        self.model = genai.GenerativeModel(GEMINI_MODEL_NAME)

    def query_database(self, filter_conditions: Optional[Dict] = None) -> List[Dict]:
        """Notionデータベースをクエリ"""
        url = f"{self.base_url}/databases/{self.database_id}/query"
        payload = {}
        if filter_conditions:
            payload["filter"] = filter_conditions
        
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json().get('results', [])
        except Exception as e:
            logger.error(f"データベースクエリエラー: {e}")
            return []

    def get_property_value(self, page: Dict, property_name: str) -> Optional[str]:
        """ページのプロパティ値を取得"""
        try:
            prop = page.get('properties', {}).get(property_name, {})
            prop_type = prop.get('type')
            
            if prop_type == 'status':
                status = prop.get('status')
                return status.get('name') if status else None
            elif prop_type == 'url':
                return prop.get('url')
            elif prop_type == 'title':
                title_array = prop.get('title', [])
                return title_array[0].get('plain_text') if title_array else None
            elif prop_type == 'rich_text':
                rich_text_array = prop.get('rich_text', [])
                return rich_text_array[0].get('plain_text') if rich_text_array else None
            
            return None
        except Exception as e:
            logger.debug(f"プロパティ取得エラー ({property_name}): {e}")
            return None

    def update_page_properties(self, page_id: str, properties: Dict) -> bool:
        """ページのプロパティを更新"""
        url = f"{self.base_url}/pages/{page_id}"
        try:
            response = requests.patch(url, headers=self.headers, json={"properties": properties})
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"ページ更新エラー: {e}")
            return False

    def download_pdf(self, url: str, save_path: str) -> bool:
        """PDFをダウンロード"""
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            with open(save_path, 'wb') as f:
                f.write(response.content)
            
            logger.info(f"PDF downloaded: {save_path}")
            return True
        except Exception as e:
            logger.error(f"PDFダウンロードエラー: {e}")
            return False

    def fetch_url_content(self, url: str) -> Optional[str]:
        """URLからテキストコンテンツを取得"""
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 不要なタグを削除
            for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
                tag.decompose()
            
            text = soup.get_text(separator='\n', strip=True)
            return text
        except Exception as e:
            logger.error(f"URL取得エラー: {e}")
            return None

    def save_content_to_file(self, content: str, output_path: str, source_url: str, title: str) -> bool:
        """生成したコンテンツをファイルに保存"""
        try:
            # Frontmatterを追加
            now = datetime.now(timezone.utc).astimezone()
            date_str = now.strftime('%Y-%m-%d')
            
            frontmatter = f"""---
title: "{title}"
date: {date_str}
source_url: {source_url}
---

"""
            # 引用セクションを追加
            citation_section = f"""> 引用元: [{title}]({source_url})

"""
            
            full_content = frontmatter + content.strip() + "\n\n" + citation_section
            
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(full_content)
            
            logger.info(f"  ファイル保存: {os.path.basename(output_path)}")
            return True
            
        except Exception as e:
            logger.error(f"ファイル保存エラー: {e}")
            return False

    def generate_content_from_pdf(self, pdf_path: str, source_url: str, title: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        PDFからブログ記事と台本を生成してファイルに保存
        
        Returns:
            (blog_content, podcast_content, blog_file_path, podcast_file_path)
        """
        try:
            logger.info("PDFをアップロード中...")
            pdf_file = genai.upload_file(pdf_path)
            logger.info(f"PDF uploaded: {pdf_file.name}")
            
            # ブログ記事生成
            logger.info("ブログ記事を生成中...")
            blog_response = self.model.generate_content([PROMPT_BLOG, pdf_file])
            blog_content = blog_response.text
            
            time.sleep(2)  # API制限対策
            
            # Podcast台本生成
            logger.info("Podcast台本を生成中...")
            podcast_response = self.model.generate_content([PROMPT_PODCAST, pdf_file])
            podcast_content = podcast_response.text
            
            # ファイル削除
            genai.delete_file(pdf_file.name)
            
            # ファイル名生成（タイムスタンプ + タイトルの一部）
            now = datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            safe_title = "".join(c for c in title[:30] if c.isalnum() or c in (' ', '-', '_')).strip()
            
            blog_filename = os.path.join(BLOG_OUTPUT_DIR, f"{timestamp}_{safe_title}_blog.md")
            script_filename = os.path.join(SCRIPT_OUTPUT_DIR, f"{timestamp}_{safe_title}_script.md")
            
            # ファイル保存
            if not self.save_content_to_file(blog_content, blog_filename, source_url, title):
                return None, None, None, None
            
            if not self.save_content_to_file(podcast_content, script_filename, source_url, title):
                return None, None, None, None
            
            return blog_content, podcast_content, blog_filename, script_filename
            
        except Exception as e:
            logger.error(f"PDF処理エラー: {e}")
            return None, None, None, None

    def generate_content_from_url(self, url: str, title: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        URLからブログ記事と台本を生成してファイルに保存
        
        Returns:
            (blog_content, podcast_content, blog_file_path, podcast_file_path)
        """
        try:
            # URLコンテンツ取得
            content = self.fetch_url_content(url)
            if not content:
                return None, None, None, None
            
            # ブログ記事生成
            logger.info("ブログ記事を生成中...")
            blog_prompt = PROMPT_BLOG + f"\n\n# 入力コンテンツ\n\n{content[:50000]}"  # 文字数制限
            blog_response = self.model.generate_content(blog_prompt)
            blog_content = blog_response.text
            
            time.sleep(2)
            
            # Podcast台本生成
            logger.info("Podcast台本を生成中...")
            podcast_prompt = PROMPT_PODCAST + f"\n\n# 入力コンテンツ\n\n{content[:50000]}"
            podcast_response = self.model.generate_content(podcast_prompt)
            podcast_content = podcast_response.text
            
            # ファイル名生成
            now = datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            safe_title = "".join(c for c in title[:30] if c.isalnum() or c in (' ', '-', '_')).strip()
            
            blog_filename = os.path.join(BLOG_OUTPUT_DIR, f"{timestamp}_{safe_title}_blog.md")
            script_filename = os.path.join(SCRIPT_OUTPUT_DIR, f"{timestamp}_{safe_title}_script.md")
            
            # ファイル保存
            if not self.save_content_to_file(blog_content, blog_filename, url, title):
                return None, None, None, None
            
            if not self.save_content_to_file(podcast_content, script_filename, url, title):
                return None, None, None, None
            
            return blog_content, podcast_content, blog_filename, script_filename
            
        except Exception as e:
            logger.error(f"URL処理エラー: {e}")
            return None, None, None, None

    def process_pdf_pages(self) -> int:
        """Status(コンテンツ作成) = 執筆待ち(PDF) のページを処理"""
        logger.info("\n「執筆待ち(PDF)」のページを検索中...")
        
        filter_conditions = {
            "property": "Status(コンテンツ作成)",
            "status": {
                "equals": "執筆待ち(PDF)"
            }
        }
        
        pages = self.query_database(filter_conditions)
        logger.info(f"{len(pages)} 件のページが見つかりました")
        
        success_count = 0
        
        for page in pages:
            page_id = page.get('id')
            title = self.get_property_value(page, 'Title') or 'タイトルなし'
            source_url = self.get_property_value(page, 'URL(Source)')
            
            logger.info(f"\n処理中: {title[:50]}...")
            
            if not source_url:
                logger.warning("  ⏭️  スキップ: URL(Source)が空です")
                continue
            
            # PDFダウンロード（一時ディレクトリまたはDOWNLOAD_DIRに保存）
            # 既存の仕組みを尊重してDOWNLOAD_DIRに保存
            safe_filename = "".join(c for c in title[:50] if c.isalnum() or c in (' ', '-', '_')).strip()
            pdf_filename = f"{safe_filename}.pdf"
            pdf_path = os.path.join(DOWNLOAD_DIR, pdf_filename)
            
            if not self.download_pdf(source_url, pdf_path):
                logger.error("  ❌ PDFダウンロード失敗")
                continue
            
            # コンテンツ生成とファイル保存
            blog_content, podcast_content, blog_file, script_file = self.generate_content_from_pdf(
                pdf_path, source_url, title
            )
            
            if not blog_content or not podcast_content:
                logger.error("  ❌ コンテンツ生成失敗")
                continue
            
            logger.info("  ✅ コンテンツ生成成功")
            logger.info(f"  保存先:")
            logger.info(f"    - Blog: {os.path.basename(blog_file)}")
            logger.info(f"    - Script: {os.path.basename(script_file)}")
            
            # ステータスを「ファクトチェック待ち」に更新
            properties_to_update = {
                "Status(コンテンツ作成)": {
                    "status": {
                        "name": "ファクトチェック待ち"
                    }
                }
            }
            
            if self.update_page_properties(page_id, properties_to_update):
                logger.info("  ✅ ステータス更新: 執筆待ち(PDF) → ファクトチェック待ち")
                success_count += 1
            else:
                logger.error("  ❌ ステータス更新失敗")
        
        return success_count

    def process_url_pages(self) -> int:
        """Status(コンテンツ作成) = 執筆待ち(URL) のページを処理"""
        logger.info("\n「執筆待ち(URL)」のページを検索中...")
        
        filter_conditions = {
            "property": "Status(コンテンツ作成)",
            "status": {
                "equals": "執筆待ち(URL)"
            }
        }
        
        pages = self.query_database(filter_conditions)
        logger.info(f"{len(pages)} 件のページが見つかりました")
        
        success_count = 0
        
        for page in pages:
            page_id = page.get('id')
            title = self.get_property_value(page, 'Title') or 'タイトルなし'
            source_url = self.get_property_value(page, 'URL(Source)')
            
            logger.info(f"\n処理中: {title[:50]}...")
            
            if not source_url:
                logger.warning("  ⏭️  スキップ: URL(Source)が空です")
                continue
            
            # コンテンツ生成とファイル保存
            blog_content, podcast_content, blog_file, script_file = self.generate_content_from_url(
                source_url, title
            )
            
            if not blog_content or not podcast_content:
                logger.error("  ❌ コンテンツ生成失敗")
                continue
            
            logger.info("  ✅ コンテンツ生成成功")
            logger.info(f"  保存先:")
            logger.info(f"    - Blog: {os.path.basename(blog_file)}")
            logger.info(f"    - Script: {os.path.basename(script_file)}")
            
            # ステータスを「ファクトチェック待ち」に更新
            properties_to_update = {
                "Status(コンテンツ作成)": {
                    "status": {
                        "name": "ファクトチェック待ち"
                    }
                }
            }
            
            if self.update_page_properties(page_id, properties_to_update):
                logger.info("  ✅ ステータス更新: 執筆待ち(URL) → ファクトチェック待ち")
                success_count += 1
            else:
                logger.error("  ❌ ステータス更新失敗")
        
        return success_count


def main():
    """メイン実行関数"""
    logger.info("=" * 50)
    logger.info("Notion監視型コンテンツ自動生成を開始します")
    logger.info("=" * 50)
    
    try:
        generator = NotionContentGenerator()
        
        # 1. 執筆待ち(PDF)のページを処理
        pdf_count = generator.process_pdf_pages()
        
        # 2. 執筆待ち(URL)のページを処理
        url_count = generator.process_url_pages()
        
        logger.info("\n" + "=" * 50)
        logger.info("処理完了")
        logger.info(f"PDF処理: {pdf_count} 件")
        logger.info(f"URL処理: {url_count} 件")
        logger.info("=" * 50)
        
    except Exception as e:
        logger.error(f"予期せぬエラー: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
