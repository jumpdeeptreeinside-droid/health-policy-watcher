#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
医療政策関連ニュースを複数の情報源から取得し、Notionデータベースに自動追加するスクリプト

対応情報源:
1. 厚生労働省 (MHLW) - RSS
2. 日本医療政策機構 (HGPI) - Webスクレイピング
3. WHO - Webスクレイピング

機能:
- 各情報源から最新ニュース記事のタイトルとURLを取得
- Notionデータベースへの自動追加（重複チェック付き）
- エラーハンドリング（一部の情報源が失敗してもスクリプトは継続）

必要なライブラリ:
    pip install requests beautifulsoup4 feedparser notion-client
"""

import sys
import io
import logging
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional

import requests
import feedparser
from bs4 import BeautifulSoup
from notion_client import Client

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
        logging.FileHandler('fetch_news.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# 設定ファイルをインポート
# GitHub Actions実行時は環境変数から、ローカル実行時はconfig.pyから読み取る
import os

try:
    # まず環境変数を確認（GitHub Actions用）
    NOTION_API_KEY = os.environ.get('NOTION_API_KEY')
    NOTION_DATABASE_ID = os.environ.get('NOTION_DATABASE_ID')
    
    # 環境変数がない場合はconfig.pyから読み込む（ローカル実行用）
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
except AttributeError as e:
    logger.error(f"config.py の設定が不足しています: {e}")
    sys.exit(1)

# ユーザーエージェント設定（WHOなどのスクレイピング用）
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}


class NewsArticle:
    """ニュース記事を表すデータクラス"""
    def __init__(self, title: str, url: str, source: str, published_date: Optional[str] = None):
        self.title = title
        self.url = url
        self.source = source
        self.published_date = published_date

    def __repr__(self):
        return f"NewsArticle(title='{self.title[:30]}...', source='{self.source}')"


class NewsCollector:
    """複数の情報源からニュースを収集するクラス"""

    def __init__(self):
        self.articles: List[NewsArticle] = []

    def fetch_mhlw_rss(self, limit: int = 20) -> List[NewsArticle]:
        """
        厚生労働省のRSSフィードから最新ニュースを取得

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        logger.info("厚生労働省 RSS を取得中...")
        articles = []
        
        try:
            rss_url = "https://www.mhlw.go.jp/stf/news.rdf"
            feed = feedparser.parse(rss_url)
            
            if feed.bozo:
                logger.warning(f"RSS解析エラー: {feed.bozo_exception}")
            
            for entry in feed.entries[:limit]:
                title = entry.get('title', 'タイトルなし')
                link = entry.get('link', '')
                published = entry.get('published', None)
                
                if link:
                    article = NewsArticle(
                        title=title,
                        url=link,
                        source="MHLW",
                        published_date=published
                    )
                    articles.append(article)
            
            logger.info(f"✅ 厚生労働省: {len(articles)} 件の記事を取得")
            
        except Exception as e:
            logger.error(f"❌ 厚生労働省 RSS取得エラー: {e}")
        
        return articles

    def fetch_hgpi_news(self, limit: int = 20) -> List[NewsArticle]:
        """
        日本医療政策機構(HGPI)のニュースページから最新記事を取得

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        logger.info("日本医療政策機構 (HGPI) ニュースを取得中...")
        articles = []
        
        try:
            news_url = "https://hgpi.org/news/"
            response = requests.get(news_url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # ニュース記事のリンクを探す
            news_items = []
            
            # パターン1: リスト内のh2/h3リンク（最も一般的）
            headings = soup.find_all(['h2', 'h3', 'h4'], limit=limit*3)
            for heading in headings:
                link_tag = heading.find('a', href=True)
                if link_tag:
                    title = heading.get_text(strip=True)
                    url = link_tag['href']
                    if title and url and len(title) > 5:
                        news_items.append({'title': title, 'url': url})
            
            # パターン2: article タグ内のリンク
            if len(news_items) < 5:
                articles_tags = soup.find_all('article', limit=limit*2)
                for article in articles_tags:
                    link_tag = article.find('a', href=True)
                    if link_tag:
                        title = link_tag.get_text(strip=True)
                        # タイトルが短すぎるなら、h2/h3を探す
                        if len(title) < 10:
                            title_tag = article.find(['h2', 'h3', 'h4'])
                            if title_tag:
                                title = title_tag.get_text(strip=True)
                        
                        if title and len(title) > 5:
                            news_items.append({
                                'title': title,
                                'url': link_tag['href']
                            })
            
            # パターン3: すべてのリンクから /news/ または /post/ を含むものを抽出
            if len(news_items) < 5:
                all_links = soup.find_all('a', href=True, limit=limit*5)
                for link in all_links:
                    url = link.get('href', '')
                    if '/news/' in url or '/post/' in url or '/research/' in url:
                        title = link.get_text(strip=True)
                        if title and len(title) > 10 and len(title) < 200:
                            news_items.append({'title': title, 'url': url})
            
            logger.debug(f"HGPI: {len(news_items)} 個の候補を発見")
            
            # 取得した記事をNewsArticleオブジェクトに変換
            for item in news_items[:limit]:
                url = item['url']
                # 相対URLを絶対URLに変換
                if url.startswith('/'):
                    url = f"https://hgpi.org{url}"
                elif not url.startswith('http'):
                    url = f"https://hgpi.org/{url}"
                
                article = NewsArticle(
                    title=item['title'],
                    url=url,
                    source="HGPI"
                )
                articles.append(article)
            
            logger.info(f"✅ HGPI: {len(articles)} 件の記事を取得")
            
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ HGPI ニュース取得エラー (ネットワーク): {e}")
        except Exception as e:
            logger.error(f"❌ HGPI ニュース取得エラー: {e}")
            import traceback
            traceback.print_exc()
        
        return articles

    def fetch_who_news(self, limit: int = 20) -> List[NewsArticle]:
        """
        WHO (World Health Organization) のニュースページから最新記事を取得
        
        WHOはRSSフィードも提供しているため、まずRSSを試し、失敗した場合はWebスクレイピングにフォールバック

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        logger.info("WHO ニュースを取得中...")
        articles = []
        
        # 方法1: RSSフィードを試す
        try:
            rss_url = "https://www.who.int/feeds/entity/mediacentre/news/en/rss.xml"
            feed = feedparser.parse(rss_url)
            
            if not feed.bozo and len(feed.entries) > 0:
                logger.info("WHO RSS フィードから取得します")
                for entry in feed.entries[:limit]:
                    title = entry.get('title', 'タイトルなし')
                    link = entry.get('link', '')
                    published = entry.get('published', None)
                    
                    if link:
                        article = NewsArticle(
                            title=title,
                            url=link,
                            source="WHO",
                            published_date=published
                        )
                        articles.append(article)
                
                logger.info(f"✅ WHO (RSS): {len(articles)} 件の記事を取得")
                return articles
        except Exception as e:
            logger.warning(f"WHO RSS取得失敗、Webスクレイピングにフォールバック: {e}")
        
        # 方法2: Webスクレイピング
        try:
            news_url = "https://www.who.int/news"
            response = requests.get(news_url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # WHOのニュース記事を探す
            news_items = []
            
            # パターン1: リスト内のh2/h3/h4リンク
            headings = soup.find_all(['h2', 'h3', 'h4'], limit=limit*3)
            for heading in headings:
                link_tag = heading.find('a', href=True)
                if link_tag:
                    title = heading.get_text(strip=True)
                    url = link_tag['href']
                    if title and url and len(title) > 10 and len(title) < 200:
                        news_items.append({'title': title, 'url': url})
            
            # パターン2: article タグ
            if len(news_items) < 5:
                articles_tags = soup.find_all('article', limit=limit*2)
                for article in articles_tags:
                    link_tag = article.find('a', href=True)
                    if link_tag:
                        # タイトルをarticle内から探す
                        title_tag = article.find(['h2', 'h3', 'h4', 'span'], class_=['title', 'heading'])
                        if not title_tag:
                            title_tag = article.find(['h2', 'h3', 'h4'])
                        
                        title = link_tag.get_text(strip=True) if not title_tag else title_tag.get_text(strip=True)
                        
                        if title and len(title) > 10 and len(title) < 200:
                            news_items.append({
                                'title': title,
                                'url': link_tag['href']
                            })
            
            # パターン3: すべてのリンクから /news/ を含むものを抽出
            if len(news_items) < 5:
                all_links = soup.find_all('a', href=True, limit=limit*5)
                for link in all_links:
                    url = link.get('href', '')
                    if '/news/' in url or '/news-room/' in url:
                        title = link.get_text(strip=True)
                        if title and len(title) > 15 and len(title) < 200:
                            news_items.append({'title': title, 'url': url})
            
            logger.debug(f"WHO: {len(news_items)} 個の候補を発見")
            
            # 取得した記事をNewsArticleオブジェクトに変換
            for item in news_items[:limit]:
                url = item['url']
                # 相対URLを絶対URLに変換
                if url.startswith('/'):
                    url = f"https://www.who.int{url}"
                elif not url.startswith('http'):
                    url = f"https://www.who.int/{url}"
                
                article = NewsArticle(
                    title=item['title'],
                    url=url,
                    source="WHO"
                )
                articles.append(article)
            
            logger.info(f"✅ WHO (スクレイピング): {len(articles)} 件の記事を取得")
            
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ WHO ニュース取得エラー (ネットワーク): {e}")
        except Exception as e:
            logger.error(f"❌ WHO ニュース取得エラー: {e}")
            import traceback
            traceback.print_exc()
        
        return articles

    def collect_all(self, limit_per_source: int = 20) -> List[NewsArticle]:
        """
        すべての情報源からニュースを収集

        Args:
            limit_per_source: 各情報源から取得する記事の最大数

        Returns:
            すべての情報源からのNewsArticleのリスト
        """
        logger.info("=" * 50)
        logger.info("ニュース収集を開始します")
        logger.info("=" * 50)
        
        all_articles = []
        
        # 厚生労働省
        mhlw_articles = self.fetch_mhlw_rss(limit=limit_per_source)
        all_articles.extend(mhlw_articles)
        time.sleep(1)  # 礼儀正しく待機
        
        # HGPI
        hgpi_articles = self.fetch_hgpi_news(limit=limit_per_source)
        all_articles.extend(hgpi_articles)
        time.sleep(1)
        
        # WHO
        who_articles = self.fetch_who_news(limit=limit_per_source)
        all_articles.extend(who_articles)
        
        logger.info("=" * 50)
        logger.info(f"合計 {len(all_articles)} 件の記事を収集しました")
        logger.info("=" * 50)
        
        return all_articles


class NotionUploader:
    """Notionデータベースにニュースをアップロードするクラス"""

    def __init__(self):
        if not NOTION_API_KEY or not NOTION_DATABASE_ID:
            raise ValueError("Notion API Key または Database ID が設定されていません。")
        
        self.notion = Client(auth=NOTION_API_KEY)
        self.database_id = NOTION_DATABASE_ID

    def check_url_exists(self, url: str) -> bool:
        """
        指定されたURLが既にNotionデータベースに存在するかチェック

        Args:
            url: チェックするURL

        Returns:
            存在する場合True、存在しない場合False
        """
        try:
            # URL(Source)プロパティでフィルタリング
            # notion-client 2.x では query メソッドを直接使用
            import requests
            headers = {
                "Authorization": f"Bearer {NOTION_API_KEY}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json"
            }
            
            query_url = f"https://api.notion.com/v1/databases/{self.database_id}/query"
            payload = {
                "filter": {
                    "property": "URL(Source)",
                    "url": {
                        "equals": url
                    }
                },
                "page_size": 1
            }
            
            response = requests.post(query_url, headers=headers, json=payload)
            response.raise_for_status()
            results = response.json()
            
            return len(results.get('results', [])) > 0
            
        except Exception as e:
            logger.warning(f"重複チェックエラー: {e}")
            return False

    def add_article(self, article: NewsArticle) -> Optional[str]:
        """
        Notionデータベースに記事を追加

        Args:
            article: 追加するNewsArticleオブジェクト

        Returns:
            成功時はページID、失敗時はNone
        """
        try:
            # 現在の日時（JST）
            now = datetime.now(timezone.utc).astimezone()
            date_str = now.strftime('%Y-%m-%d')
            
            # プロパティを構築
            properties = {
                "Title": {
                    "title": [
                        {
                            "text": {
                                "content": article.title
                            }
                        }
                    ]
                },
                "URL(Source)": {
                    "url": article.url
                },
                "Date(Search)": {
                    "date": {
                        "start": date_str
                    }
                }
            }
            
            # Sourceプロパティが存在する場合は追加（存在しない場合はスキップ）
            # ※この部分は実際のDBスキーマに合わせて調整
            # properties["Source"] = {
            #     "select": {
            #         "name": article.source
            #     }
            # }
            
            # ページを作成
            new_page = self.notion.pages.create(
                parent={"database_id": self.database_id},
                properties=properties
            )
            
            return new_page['id']
            
        except Exception as e:
            logger.error(f"Notion追加エラー: {e}")
            return None

    def upload_articles(self, articles: List[NewsArticle]) -> Dict[str, int]:
        """
        複数の記事をNotionにアップロード（重複チェック付き）

        Args:
            articles: アップロードするNewsArticleのリスト

        Returns:
            結果の統計情報（success, skip, fail）
        """
        logger.info("\n" + "=" * 50)
        logger.info("Notionへのアップロードを開始します")
        logger.info("=" * 50)
        
        stats = {"success": 0, "skip": 0, "fail": 0}
        
        for i, article in enumerate(articles, 1):
            logger.info(f"\n[{i}/{len(articles)}] 処理中: {article.title[:50]}...")
            
            # 重複チェック
            if self.check_url_exists(article.url):
                logger.info(f"⏭️  スキップ: 既に登録済み ({article.source})")
                stats["skip"] += 1
                continue
            
            # Notionに追加
            page_id = self.add_article(article)
            
            if page_id:
                logger.info(f"✅ 成功: {article.source} - {article.title[:50]}")
                stats["success"] += 1
            else:
                logger.error(f"❌ 失敗: {article.source} - {article.title[:50]}")
                stats["fail"] += 1
            
            # API制限を考慮して待機
            time.sleep(0.5)
        
        logger.info("\n" + "=" * 50)
        logger.info("アップロード完了")
        logger.info(f"成功: {stats['success']} 件 / スキップ: {stats['skip']} 件 / 失敗: {stats['fail']} 件")
        logger.info("=" * 50)
        
        return stats


def main():
    """メイン実行関数"""
    try:
        # ニュース収集
        collector = NewsCollector()
        articles = collector.collect_all(limit_per_source=20)
        
        if not articles:
            logger.warning("収集された記事がありません。処理を終了します。")
            return
        
        # Notionにアップロード
        uploader = NotionUploader()
        stats = uploader.upload_articles(articles)
        
        logger.info("\n✅ すべての処理が完了しました！")
        
    except Exception as e:
        logger.error(f"予期せぬエラー: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
