#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
医療政策関連ニュースを複数の情報源から取得し、Notionデータベースに自動追加するスクリプト

対応情報源:
1. 厚生労働省 (MHLW) - RSS
2. 財務省 (MOF) - RSS
3. 内閣府 (CAO) - RSS (RDF形式)
4. World Bank (Health) - 公式Search API (Healthトピック絞り込み)
5. UN News (Health) - RSS (WHO・UNICEF・UNAIDS等を網羅)
6. FIP - Webスクレイピング (プレスリリースページ)
7. 日本医療政策機構 (HGPI) - Webスクレイピング
8. WHO - 公式JSON API

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

    def fetch_mof_rss(self, limit: int = 20) -> List[NewsArticle]:
        """
        財務省のRSSフィードから最新ニュースを取得

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        logger.info("財務省 RSS を取得中...")
        articles = []

        try:
            rss_url = "https://www.mof.go.jp/news.rss"
            feed = feedparser.parse(rss_url)

            if feed.bozo:
                logger.warning(f"財務省 RSS解析エラー: {feed.bozo_exception}")

            for entry in feed.entries[:limit]:
                title = entry.get('title', 'タイトルなし')
                link = entry.get('link', '')
                published = entry.get('published', None)

                if link:
                    article = NewsArticle(
                        title=title,
                        url=link,
                        source="MOF",
                        published_date=published
                    )
                    articles.append(article)

            logger.info(f"✅ 財務省: {len(articles)} 件の記事を取得")

        except Exception as e:
            logger.error(f"❌ 財務省 RSS取得エラー: {e}")

        return articles

    def fetch_cao_rss(self, limit: int = 20) -> List[NewsArticle]:
        """
        内閣府のRSSフィード（RDF形式）から最新ニュースを取得

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        logger.info("内閣府 RSS を取得中...")
        articles = []

        try:
            rss_url = "https://www.cao.go.jp/rss/news.rdf"
            feed = feedparser.parse(rss_url)

            if feed.bozo:
                logger.warning(f"内閣府 RSS解析エラー: {feed.bozo_exception}")

            for entry in feed.entries[:limit]:
                title = entry.get('title', 'タイトルなし')
                link = entry.get('link', '')
                # RDF形式では dc:date を使用
                published = entry.get('dc_date', entry.get('published', None))

                if link:
                    article = NewsArticle(
                        title=title,
                        url=link,
                        source="CAO",
                        published_date=published
                    )
                    articles.append(article)

            logger.info(f"✅ 内閣府: {len(articles)} 件の記事を取得")

        except Exception as e:
            logger.error(f"❌ 内閣府 RSS取得エラー: {e}")

        return articles

    def fetch_worldbank_health_news(self, limit: int = 20) -> List[NewsArticle]:
        """
        World Bank の公式Search APIからHealthトピックの最新ニュースを取得

        World Bankのニュースページ (/en/news/all?topic_exact=Health) はJS動的レンダリングのため
        スクレイピング不可。代わりに公式Search APIを使用する。
        - エンドポイント: https://search.worldbank.org/api/v2/news
        - Healthトピックに絞り込み、公開日降順で取得

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        logger.info("World Bank (Health) ニュースを取得中 (Search API)...")
        articles = []

        # 対象とするコンテンツタイプ（BriefやPublicationは除外）
        NEWS_TYPES = {
            "Press Release", "Feature Story", "Factsheet",
            "News Release", "Speech", "Op-Ed", "Results Brief"
        }

        try:
            api_url = (
                "https://search.worldbank.org/api/v2/news"
                f"?format=json&lang_exact=English&topic_exact=Health"
                f"&rows={limit * 3}&os=0&srt=lnchdt&order=desc"
            )
            response = requests.get(api_url, headers=HEADERS, timeout=30)
            response.raise_for_status()

            data = response.json()
            documents = data.get("documents", {})

            # dict形式で返るため、facetsキーを除いてリスト化し、日付降順でソート
            items = [
                v for k, v in documents.items()
                if k != "facets" and isinstance(v, dict)
            ]
            items.sort(key=lambda x: x.get("lnchdt", ""), reverse=True)

            for item in items:
                conttype = item.get("conttype", "")
                if conttype not in NEWS_TYPES:
                    continue

                title = item.get("title", "")
                if isinstance(title, dict):
                    title = title.get("cdata!", "")
                title = title.strip()

                url = item.get("url", "").strip()
                published = item.get("lnchdt", None)

                if not title or not url:
                    continue

                # http → https に正規化
                if url.startswith("http://"):
                    url = "https://" + url[7:]

                article = NewsArticle(
                    title=title,
                    url=url,
                    source="WorldBank",
                    published_date=published
                )
                articles.append(article)

                if len(articles) >= limit:
                    break

            logger.info(f"✅ World Bank (Health): {len(articles)} 件の記事を取得")

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ World Bank ニュース取得エラー (ネットワーク): {e}")
        except Exception as e:
            logger.error(f"❌ World Bank ニュース取得エラー: {e}")
            import traceback
            traceback.print_exc()

        return articles

    def fetch_un_news_health_rss(self, limit: int = 20) -> List[NewsArticle]:
        """
        UN News の Health トピック RSS フィードから最新記事を取得

        WHO・UNICEF・UNAIDS 等の国連機関によるHealth関連ニュースを網羅。
        unicef.org はCloudflare保護のため直接アクセス不可のため、こちらで代替。

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        logger.info("UN News (Health) RSS を取得中...")
        articles = []

        try:
            rss_url = "https://news.un.org/feed/subscribe/en/news/topic/health/feed/rss.xml"
            feed = feedparser.parse(rss_url)

            if feed.bozo:
                logger.warning(f"UN News RSS解析エラー: {feed.bozo_exception}")

            for entry in feed.entries[:limit]:
                title = entry.get('title', 'タイトルなし')
                # guid (perma link) が本来のURL、link はfeed viewer経由のURLのため guid を優先
                link = entry.get('id', entry.get('link', ''))
                published = entry.get('published', None)

                if link:
                    article = NewsArticle(
                        title=title,
                        url=link,
                        source="UN News",
                        published_date=published
                    )
                    articles.append(article)

            logger.info(f"✅ UN News (Health): {len(articles)} 件の記事を取得")

        except Exception as e:
            logger.error(f"❌ UN News RSS取得エラー: {e}")

        return articles

    def fetch_fip_news(self, limit: int = 20) -> List[NewsArticle]:
        """
        FIP (International Pharmaceutical Federation) のプレスリリースページから最新記事を取得

        HTMLが静的レンダリングのためBeautifulSoupでスクレイピング可能。
        ページ構造: 各<a>タグに「タイトル More 場所 • 日付」が一体で格納されている。

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        import re
        logger.info("FIP プレスリリースを取得中...")
        articles = []

        try:
            url = "https://www.fip.org/press-releases"
            response = requests.get(url, headers=HEADERS, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')

            # フィーチャー記事（article タグ）を取得
            featured = soup.find('article')
            if featured:
                a_tag = featured.find('a', href=True)
                if a_tag and 'press-item' in a_tag.get('href', ''):
                    raw = a_tag.get_text(separator=' ', strip=True)
                    title = re.split(r'\s+More\s+', raw)[0].strip()
                    href = a_tag['href']
                    if href.startswith('./'):
                        href = 'https://www.fip.org/' + href[2:]
                    date_match = re.search(r'\d+\s+\w+\s+\d{4}', raw)
                    published = date_match.group(0) if date_match else None
                    if title:
                        articles.append(NewsArticle(
                            title=title, url=href, source="FIP", published_date=published
                        ))

            # アーカイブリスト（タイトルテキスト付きの press-item リンク）
            links = [
                a for a in soup.find_all('a', href=True)
                if 'press-item' in a.get('href', '') and a.get_text(strip=True) != 'More'
            ]

            for a_tag in links:
                if len(articles) >= limit:
                    break
                raw = a_tag.get_text(separator=' ', strip=True)
                # "タイトル More 場所 • 日付" 形式を分割
                parts = re.split(r'\s+More\s+', raw, maxsplit=1)
                title = parts[0].strip()

                href = a_tag['href']
                if href.startswith('./'):
                    href = 'https://www.fip.org/' + href[2:]
                elif href.startswith('/'):
                    href = 'https://www.fip.org' + href

                date_match = re.search(r'\d+\s+\w+\s+\d{4}', raw)
                published = date_match.group(0) if date_match else None

                if title and href:
                    articles.append(NewsArticle(
                        title=title, url=href, source="FIP", published_date=published
                    ))

            logger.info(f"✅ FIP: {len(articles)} 件の記事を取得")

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ FIP プレスリリース取得エラー (ネットワーク): {e}")
        except Exception as e:
            logger.error(f"❌ FIP プレスリリース取得エラー: {e}")
            import traceback
            traceback.print_exc()

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
        WHO (World Health Organization) の公式JSON APIから最新記事を取得

        WHOのニュースページはJavaScriptで動的レンダリングされるため、
        requests によるスクレイピングでは "Loading..." しか取得できない。
        旧RSSフィード (feeds/entity/mediacentre/news/en/rss.xml) も廃止済み。
        代わりに公式のREST APIエンドポイントを使用する。

        Args:
            limit: 取得する記事の最大数

        Returns:
            NewsArticleのリスト
        """
        logger.info("WHO ニュースを取得中 (公式JSON API)...")
        articles = []

        try:
            api_url = (
                "https://www.who.int/api/news/articles"
                f"?sf_culture=en&$top={limit}&$orderby=PublicationDate+desc"
            )
            response = requests.get(api_url, headers=HEADERS, timeout=30)
            response.raise_for_status()

            data = response.json()
            items = data.get("value", [])

            for item in items:
                title = item.get("Title", "").strip()
                url_path = item.get("ItemDefaultUrl", "")
                published = item.get("PublicationDate", None)

                if not title or not url_path:
                    continue

                # 相対パスを絶対URLに変換
                if url_path.startswith("/"):
                    url = f"https://www.who.int{url_path}"
                else:
                    url = url_path

                article = NewsArticle(
                    title=title,
                    url=url,
                    source="WHO",
                    published_date=published
                )
                articles.append(article)

            logger.info(f"✅ WHO (JSON API): {len(articles)} 件の記事を取得")

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

        # 財務省
        mof_articles = self.fetch_mof_rss(limit=limit_per_source)
        all_articles.extend(mof_articles)
        time.sleep(1)

        # 内閣府
        cao_articles = self.fetch_cao_rss(limit=limit_per_source)
        all_articles.extend(cao_articles)
        time.sleep(1)

        # World Bank (Health)
        wb_articles = self.fetch_worldbank_health_news(limit=limit_per_source)
        all_articles.extend(wb_articles)
        time.sleep(1)

        # UN News (Health)
        un_articles = self.fetch_un_news_health_rss(limit=limit_per_source)
        all_articles.extend(un_articles)
        time.sleep(1)

        # FIP
        fip_articles = self.fetch_fip_news(limit=limit_per_source)
        all_articles.extend(fip_articles)
        time.sleep(1)

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
