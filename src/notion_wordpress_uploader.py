#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notion → WordPress 自動アップロードスクリプト

機能:
1. NotionデータベースでStatus(Web)が「投稿待ち」のページを検出
2. Article(Web)プロパティのリンク先（別Notionページ）から記事本文を取得
3. Notionブロック → Markdown → HTML に変換
4. WordPress に下書きとして自動投稿（重複チェックあり）
5. 投稿成功後、Notion の Status(Web) を「スケジュール待ち」に自動更新

実行方法:
    python notion_wordpress_uploader.py
"""

import sys
import io
import os
import re
import logging
import requests
import markdown as markdown_lib
from datetime import datetime, timezone
from typing import List, Dict, Optional

# ──────────────────────────────────────────────
# Windows 環境での文字エンコーディング対応
# ──────────────────────────────────────────────
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# ──────────────────────────────────────────────
# ロギング設定
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('notion_wordpress_uploader.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 設定読み込み（環境変数優先、なければ config.py）
# ──────────────────────────────────────────────
try:
    NOTION_API_KEY         = os.environ.get('NOTION_API_KEY')
    NOTION_DATABASE_ID     = os.environ.get('NOTION_DATABASE_ID')
    WORDPRESS_URL          = os.environ.get('WORDPRESS_URL')
    WORDPRESS_USERNAME     = os.environ.get('WORDPRESS_USERNAME')
    WORDPRESS_APP_PASSWORD = os.environ.get('WORDPRESS_APP_PASSWORD')

    _missing = not all([
        NOTION_API_KEY, NOTION_DATABASE_ID,
        WORDPRESS_URL, WORDPRESS_USERNAME, WORDPRESS_APP_PASSWORD
    ])

    if _missing:
        import config
        NOTION_API_KEY         = NOTION_API_KEY         or config.NOTION_API_KEY
        NOTION_DATABASE_ID     = NOTION_DATABASE_ID     or config.NOTION_DATABASE_ID
        WORDPRESS_URL          = WORDPRESS_URL          or config.WORDPRESS_URL
        WORDPRESS_USERNAME     = WORDPRESS_USERNAME     or config.WORDPRESS_USERNAME
        WORDPRESS_APP_PASSWORD = WORDPRESS_APP_PASSWORD or config.WORDPRESS_APP_PASSWORD
        DEFAULT_CATEGORY_ID    = getattr(config, 'DEFAULT_CATEGORY_ID',    None)
        DEFAULT_TAGS           = getattr(config, 'DEFAULT_TAGS',           [])
        DEFAULT_FEATURED_IMAGE_ID = getattr(config, 'DEFAULT_FEATURED_IMAGE_ID', None)
        logger.info("config.py から設定を読み込みました")
    else:
        DEFAULT_CATEGORY_ID       = None
        DEFAULT_TAGS              = []
        DEFAULT_FEATURED_IMAGE_ID = None
        logger.info("環境変数から設定を読み込みました")

except ImportError:
    logger.error("config.py が見つからず、環境変数も設定されていません。処理を中断します。")
    sys.exit(1)


# ══════════════════════════════════════════════════════════
#  NotionBlockConverter  ─  Notion ブロック → Markdown 変換
# ══════════════════════════════════════════════════════════
class NotionBlockConverter:
    """
    Notion API のブロックオブジェクトリストを Markdown 文字列に変換するクラス。

    対応ブロックタイプ:
        paragraph, heading_1/2/3, bulleted_list_item, numbered_list_item,
        code, quote, callout, divider, image, to_do
    """

    def convert(self, blocks: List[Dict]) -> str:
        """ブロックリスト全体を Markdown に変換して返す"""
        segments: List[str] = []
        for block in blocks:
            md = self._convert_block(block)
            if md is not None:
                segments.append(md)
        return '\n\n'.join(segments)

    # ── ブロック変換 ──────────────────────────────────────

    def _convert_block(self, block: Dict) -> Optional[str]:
        block_type: str = block.get('type', '')
        data: Dict = block.get(block_type, {})

        if block_type == 'paragraph':
            text = self._rich_text_to_md(data.get('rich_text', []))
            return text  # 空段落も空文字として保持（段落間の余白を保つため）

        elif block_type == 'heading_1':
            text = self._rich_text_to_md(data.get('rich_text', []))
            return f'# {text}' if text.strip() else None

        elif block_type == 'heading_2':
            text = self._rich_text_to_md(data.get('rich_text', []))
            return f'## {text}' if text.strip() else None

        elif block_type == 'heading_3':
            text = self._rich_text_to_md(data.get('rich_text', []))
            return f'### {text}' if text.strip() else None

        elif block_type == 'bulleted_list_item':
            text = self._rich_text_to_md(data.get('rich_text', []))
            return f'- {text}' if text.strip() else None

        elif block_type == 'numbered_list_item':
            text = self._rich_text_to_md(data.get('rich_text', []))
            return f'1. {text}' if text.strip() else None

        elif block_type == 'to_do':
            checked = data.get('checked', False)
            text = self._rich_text_to_md(data.get('rich_text', []))
            mark = 'x' if checked else ' '
            return f'- [{mark}] {text}' if text.strip() else None

        elif block_type == 'code':
            text = self._rich_text_to_plain(data.get('rich_text', []))
            lang = data.get('language', '')
            return f'```{lang}\n{text}\n```'

        elif block_type == 'quote':
            text = self._rich_text_to_md(data.get('rich_text', []))
            return f'> {text}' if text.strip() else None

        elif block_type == 'callout':
            # callout は引用ブロックとして扱う
            text = self._rich_text_to_md(data.get('rich_text', []))
            return f'> {text}' if text.strip() else None

        elif block_type == 'divider':
            return '---'

        elif block_type == 'image':
            url = ''
            if data.get('type') == 'external':
                url = data.get('external', {}).get('url', '')
            elif data.get('type') == 'file':
                url = data.get('file', {}).get('url', '')
            caption = self._rich_text_to_plain(data.get('caption', []))
            return f'![{caption}]({url})' if url else None

        elif block_type in ('child_page', 'child_database', 'unsupported', 'column_list', 'column'):
            return None

        else:
            # 未対応ブロックは rich_text があればテキストのみ抽出
            rich_text = data.get('rich_text', [])
            if rich_text:
                return self._rich_text_to_md(rich_text)
            return None

    # ── テキスト変換ヘルパー ────────────────────────────

    def _rich_text_to_md(self, rich_text_array: List[Dict]) -> str:
        """
        rich_text 配列を Markdown 付き文字列に変換する。
        ボールド / イタリック / コード / 打消し / リンク に対応。
        """
        result = ''
        for rt in rich_text_array:
            rt_type = rt.get('type', 'text')
            plain   = rt.get('plain_text', '')
            if not plain:
                continue

            # mention（Notionページ参照など）はプレーンテキストのみ
            if rt_type == 'mention':
                result += plain
                continue

            annotations: Dict = rt.get('annotations', {})
            href: Optional[str] = rt.get('href')

            # アノテーションを内側から適用
            text = plain
            if annotations.get('code'):
                text = f'`{text}`'
            else:
                if annotations.get('bold') and annotations.get('italic'):
                    text = f'***{text}***'
                elif annotations.get('bold'):
                    text = f'**{text}**'
                elif annotations.get('italic'):
                    text = f'*{text}*'
                if annotations.get('strikethrough'):
                    text = f'~~{text}~~'

            # リンクを適用（code スパンには付けない）
            if href and not annotations.get('code'):
                text = f'[{text}]({href})'

            result += text
        return result

    def _rich_text_to_plain(self, rich_text_array: List[Dict]) -> str:
        """rich_text 配列をプレーンテキストのみに変換する"""
        return ''.join(rt.get('plain_text', '') for rt in rich_text_array)


# ══════════════════════════════════════════════════════════
#  NotionWordPressUploader  ─  メイン処理クラス
# ══════════════════════════════════════════════════════════
class NotionWordPressUploader:
    """
    Notion で「投稿待ち」になっている Web 記事を
    WordPress に自動アップロードするクラス。
    """

    # Notion ページ ID の正規表現パターン
    _RE_UUID = re.compile(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        re.IGNORECASE
    )
    _RE_HEX32 = re.compile(r'[0-9a-f]{32}', re.IGNORECASE)

    def __init__(self):
        # Notion
        self.notion_headers = {
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        self.notion_base = "https://api.notion.com/v1"
        self.database_id = NOTION_DATABASE_ID

        # WordPress
        self.wp_url     = WORDPRESS_URL.rstrip('/')
        self.wp_auth    = (WORDPRESS_USERNAME, WORDPRESS_APP_PASSWORD)
        self.wp_api_url: Optional[str] = None

        # コンバーター & Markdown エンジン
        self.converter = NotionBlockConverter()
        self.md_engine = markdown_lib.Markdown(extensions=[
            'extra', 'codehilite', 'tables', 'fenced_code', 'nl2br'
        ])

    # ──────────────────────────────────────────
    #  Notion API
    # ──────────────────────────────────────────

    def query_database(self, filter_conditions: Optional[Dict] = None) -> List[Dict]:
        """データベースをクエリしてページリストを返す（全ページ取得対応）"""
        url     = f"{self.notion_base}/databases/{self.database_id}/query"
        payload = {}
        if filter_conditions:
            payload['filter'] = filter_conditions

        all_pages: List[Dict] = []
        while True:
            try:
                resp = requests.post(url, headers=self.notion_headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                all_pages.extend(data.get('results', []))
                if data.get('has_more'):
                    payload['start_cursor'] = data['next_cursor']
                else:
                    break
            except Exception as e:
                logger.error(f"データベースクエリエラー: {e}")
                break
        return all_pages

    def get_property_value(self, page: Dict, property_name: str) -> Optional[str]:
        """ページのプロパティ値を文字列で返す"""
        try:
            prop      = page.get('properties', {}).get(property_name, {})
            prop_type = prop.get('type')

            if prop_type == 'status':
                s = prop.get('status')
                return s.get('name') if s else None
            elif prop_type == 'title':
                arr = prop.get('title', [])
                return arr[0].get('plain_text') if arr else None
            elif prop_type == 'rich_text':
                arr = prop.get('rich_text', [])
                return arr[0].get('plain_text') if arr else None
            elif prop_type == 'url':
                return prop.get('url')
        except Exception as e:
            logger.debug(f"プロパティ取得エラー ({property_name}): {e}")
        return None

    def get_article_linked_page_id(self, page: Dict) -> Optional[str]:
        """
        Article(Web) プロパティからリンク先の Notion ページ ID を抽出する。

        対応プロパティタイプ:
          - url 型:       プロパティの URL 文字列から直接 ID を抽出
          - rich_text 型: 以下の3パターンを順に試みる
              1. mention.page.id  （Notionページ参照）
              2. rt.href          （外部リンク形式の Notion URL）
              3. rt.text.link.url （text ブロック内の link）
        """
        try:
            prop      = page.get('properties', {}).get('Article(Web)', {})
            prop_type = prop.get('type')

            # ── url 型（URLフィールドに直接 Notion ページ URL が入っている場合）
            if prop_type == 'url':
                url_value = prop.get('url', '')
                if url_value:
                    pid = self._extract_notion_page_id(url_value)
                    if pid:
                        logger.info(f"  url プロパティ経由でページ ID を取得: {pid}")
                        return pid
                logger.warning(
                    "  Article(Web) の url プロパティに有効な Notion ページ URL が"
                    f" 見つかりません (値: {url_value!r})"
                )
                return None

            # ── rich_text 型（リンク付きテキストや mention が入っている場合）
            if prop_type == 'rich_text':
                for rt in prop.get('rich_text', []):
                    # パターン 1: mention（Notionページ参照）
                    if rt.get('type') == 'mention':
                        mention = rt.get('mention', {})
                        if mention.get('type') == 'page':
                            page_id = mention['page'].get('id')
                            if page_id:
                                logger.info(f"  mention 経由でページ ID を取得: {page_id}")
                                return page_id

                    # パターン 2: href
                    href = rt.get('href', '')
                    if href:
                        pid = self._extract_notion_page_id(href)
                        if pid:
                            logger.info(f"  href 経由でページ ID を取得: {pid}")
                            return pid

                    # パターン 3: text.link.url
                    link_url = rt.get('text', {}).get('link', {}).get('url', '')
                    if link_url:
                        pid = self._extract_notion_page_id(link_url)
                        if pid:
                            logger.info(f"  text.link 経由でページ ID を取得: {pid}")
                            return pid

                logger.warning("  Article(Web) に Notion ページへのリンクが見つかりませんでした")
                return None

            # ── 未対応の型
            logger.warning(
                f"  Article(Web) のプロパティタイプが未対応: {prop_type!r}\n"
                "  対応タイプ: url / rich_text"
            )

        except Exception as e:
            logger.error(f"  Article(Web) ページ ID 取得エラー: {e}")
        return None

    def _extract_notion_page_id(self, url: str) -> Optional[str]:
        """
        Notion の URL（または ID 文字列）からページ ID を抽出して
        UUID 形式（8-4-4-4-12）に正規化して返す。
        """
        if not url:
            return None

        # UUID 形式がそのまま含まれている場合
        m = self._RE_UUID.search(url)
        if m:
            return m.group(0).lower()

        # 32 文字 HEX が末尾付近にある場合 → UUID 形式に変換
        m = self._RE_HEX32.search(url)
        if m:
            h = m.group(0).lower()
            return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

        return None

    def fetch_page_blocks(self, page_id: str) -> List[Dict]:
        """
        Notion ページの children ブロックをすべて取得する（ページネーション対応）。
        """
        url    = f"{self.notion_base}/blocks/{page_id}/children"
        params: Dict = {}
        all_blocks: List[Dict] = []

        while True:
            try:
                resp = requests.get(
                    url, headers=self.notion_headers, params=params, timeout=30
                )
                resp.raise_for_status()
                data = resp.json()
                all_blocks.extend(data.get('results', []))
                if data.get('has_more'):
                    params['start_cursor'] = data['next_cursor']
                else:
                    break
            except Exception as e:
                logger.error(f"  ブロック取得エラー (page_id={page_id}): {e}")
                break

        return all_blocks

    def fetch_page_title(self, page_id: str) -> Optional[str]:
        """
        Notion ページのメタデータを取得してタイトルを返す。
        title 型プロパティを自動検索するため、プロパティ名に依存しない。
        """
        url = f"{self.notion_base}/pages/{page_id}"
        try:
            resp = requests.get(url, headers=self.notion_headers, timeout=30)
            resp.raise_for_status()
            page_data = resp.json()

            # properties の中から type == "title" のものを探す
            for prop_data in page_data.get('properties', {}).values():
                if prop_data.get('type') == 'title':
                    title_array = prop_data.get('title', [])
                    if title_array:
                        title = title_array[0].get('plain_text', '').strip()
                        if title:
                            return title

            # ページが DB ページではなく通常ページの場合
            # "child_page" ブロックの title を代替として使う
            if page_data.get('object') == 'page':
                props = page_data.get('properties', {})
                # "Name" または "title" というキーを探す
                for key in ('Name', 'title', 'Title'):
                    if key in props:
                        arr = props[key].get('title', [])
                        if arr:
                            return arr[0].get('plain_text', '').strip()

        except Exception as e:
            logger.error(f"  記事ページタイトル取得エラー (page_id={page_id}): {e}")
        return None

    def update_notion_status(self, page_id: str, status_name: str) -> bool:
        """Notion ページの Status(Web) を指定値に更新する"""
        url = f"{self.notion_base}/pages/{page_id}"
        payload = {
            "properties": {
                "Status(Web)": {
                    "status": {"name": status_name}
                }
            }
        }
        try:
            resp = requests.patch(
                url, headers=self.notion_headers, json=payload, timeout=30
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"  Notion ステータス更新エラー: {e}")
            return False

    # ──────────────────────────────────────────
    #  WordPress API
    # ──────────────────────────────────────────

    def detect_wp_api_url(self) -> bool:
        """WordPress REST API の投稿エンドポイント URL を自動検出する"""
        candidates = [
            f"{self.wp_url}/wp-json/wp/v2/posts",
            f"{self.wp_url}/index.php/wp-json/wp/v2/posts",
            f"{self.wp_url}/?rest_route=/wp/v2/posts",
        ]
        for url in candidates:
            try:
                resp = requests.get(url, auth=self.wp_auth, timeout=10)
                if resp.status_code in (200, 401):
                    self.wp_api_url = url
                    return True
            except Exception:
                continue
        return False

    def _find_existing_post(self, title: str) -> Optional[int]:
        """
        同タイトルの投稿が WordPress に存在するか確認し、
        存在すれば投稿 ID を返す。
        """
        try:
            resp = requests.get(
                self.wp_api_url,
                params={'search': title, 'per_page': 5, 'status': 'any'},
                auth=self.wp_auth,
                timeout=10
            )
            if resp.status_code == 200:
                for post in resp.json():
                    rendered = post.get('title', {}).get('rendered', '')
                    if rendered == title:
                        return post.get('id')
        except Exception:
            pass
        return None

    def upload_to_wordpress(self, title: str, html_content: str) -> Optional[int]:
        """
        WordPress に下書きとして投稿し、投稿 ID を返す。
        同タイトルの投稿が既に存在する場合はスキップする。
        """
        if not self.wp_api_url:
            logger.error("  WordPress API URL が未設定です")
            return None

        # 重複チェック
        existing_id = self._find_existing_post(title)
        if existing_id:
            logger.warning(
                f"  ⏭️  スキップ: 同タイトルの投稿が既に存在します "
                f"(WordPress ID: {existing_id})"
            )
            return existing_id

        post_data: Dict = {
            'title':   title,
            'content': html_content,
            'status':  'draft',
        }
        if DEFAULT_CATEGORY_ID:
            post_data['categories'] = [DEFAULT_CATEGORY_ID]
        if DEFAULT_TAGS:
            post_data['tags'] = DEFAULT_TAGS
        if DEFAULT_FEATURED_IMAGE_ID:
            post_data['featured_media'] = DEFAULT_FEATURED_IMAGE_ID

        try:
            resp = requests.post(
                self.wp_api_url,
                json=post_data,
                auth=self.wp_auth,
                timeout=30
            )
            resp.raise_for_status()
            post_id: int = resp.json().get('id')
            logger.info(f"  ✅ WordPress 投稿成功: ID={post_id}, タイトル='{title}'")
            return post_id

        except requests.exceptions.HTTPError as e:
            logger.error(f"  ❌ HTTP エラー: {e}")
            if e.response is not None:
                try:
                    err = e.response.json()
                    logger.error(f"     コード: {err.get('code')}")
                    logger.error(f"     メッセージ: {err.get('message')}")
                except Exception:
                    logger.error(f"     レスポンス: {e.response.text[:300]}")
            return None
        except requests.exceptions.ConnectionError:
            logger.error("  ❌ 接続エラー: WordPress サイトに到達できません")
            return None
        except requests.exceptions.Timeout:
            logger.error("  ❌ タイムアウト: 30 秒以内に応答がありませんでした")
            return None
        except Exception as e:
            logger.error(f"  ❌ 予期しないエラー: {e}")
            return None

    def _markdown_to_html(self, md_content: str) -> str:
        """Markdown 文字列を HTML に変換する"""
        self.md_engine.reset()
        return self.md_engine.convert(md_content)

    # ──────────────────────────────────────────
    #  メイン処理
    # ──────────────────────────────────────────

    def process(self) -> int:
        """
        メイン処理フロー:
          1. Status(Web) = "投稿待ち" のページを全件取得
          2. 各ページの Article(Web) リンク先から Notion ページ本文を取得
          3. ブロック → Markdown → HTML に変換
          4. WordPress に下書き投稿
          5. 成功時に Notion の Status(Web) を「スケジュール待ち」に更新

        Returns:
            WordPress 投稿に成功した件数
        """
        logger.info("Status(Web) が「投稿待ち」のページを検索中...")

        pages = self.query_database({
            "property": "Status(Web)",
            "status":   {"equals": "投稿待ち"}
        })
        logger.info(f"{len(pages)} 件のページが見つかりました")

        if not pages:
            logger.info("処理対象なし。終了します。")
            return 0

        # WordPress API 疎通確認
        logger.info("WordPress REST API を確認中...")
        if not self.detect_wp_api_url():
            logger.error(
                "WordPress REST API への接続に失敗しました。"
                "URL・認証情報を確認してください。処理を中断します。"
            )
            return 0
        logger.info(f"WordPress API URL: {self.wp_api_url}")

        success_count = 0

        for page in pages:
            page_id = page.get('id')
            logger.info(f"\n{'=' * 55}")

            # ── DB タイトル（ログ・フォールバック用）
            db_title = (
                self.get_property_value(page, 'Title(Web)')
                or self.get_property_value(page, 'Title')
                or 'タイトルなし'
            )
            logger.info(f"処理中 (DB): {db_title[:60]}")

            # ── Article(Web) のリンク先ページ ID を取得
            article_page_id = self.get_article_linked_page_id(page)
            if not article_page_id:
                logger.error(
                    "  ❌ スキップ: Article(Web) プロパティに "
                    "Notion ページへのリンクが見つかりません"
                )
                continue

            # ── リンク先ページのタイトルを WordPress 投稿タイトルとして使用
            #    取得できなかった場合は DB タイトルにフォールバック
            title = self.fetch_page_title(article_page_id) or db_title
            logger.info(f"  記事タイトル: {title[:60]}")

            # ── リンク先ページのブロックを取得
            logger.info(f"  Notion 記事ページ (ID: {article_page_id}) の本文を取得中...")
            blocks = self.fetch_page_blocks(article_page_id)

            if not blocks:
                logger.error(
                    "  ❌ スキップ: 記事ページが空、"
                    "またはブロックの取得に失敗しました"
                )
                continue

            logger.info(f"  取得ブロック数: {len(blocks)}")

            # ── ブロック → Markdown → HTML 変換
            md_content   = self.converter.convert(blocks)
            html_content = self._markdown_to_html(md_content)

            if not html_content.strip():
                logger.error("  ❌ スキップ: HTML 変換後のコンテンツが空です")
                continue

            # ── WordPress に下書き投稿
            logger.info("  WordPress に投稿中...")
            post_id = self.upload_to_wordpress(title, html_content)

            if post_id:
                # ── Notion ステータスを「スケジュール待ち」に更新
                if self.update_notion_status(page_id, "スケジュール待ち"):
                    logger.info(
                        "  ✅ Notion ステータス更新: 投稿待ち → スケジュール待ち"
                    )
                else:
                    logger.warning(
                        "  ⚠️  WordPress 投稿は成功しましたが、"
                        "Notion ステータスの更新に失敗しました"
                    )
                success_count += 1
            else:
                logger.error("  ❌ WordPress 投稿失敗。このページはスキップします。")

        return success_count


# ══════════════════════════════════════════════════════════
#  エントリーポイント
# ══════════════════════════════════════════════════════════
def main():
    logger.info("=" * 55)
    logger.info("Notion → WordPress 自動アップロードを開始します")
    logger.info(f"実行日時: {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    logger.info("=" * 55)

    try:
        uploader = NotionWordPressUploader()
        count    = uploader.process()

        logger.info("\n" + "=" * 55)
        logger.info("処理完了")
        logger.info(f"WordPress 投稿成功: {count} 件")
        logger.info("=" * 55)

    except Exception as e:
        logger.error(f"予期せぬエラーが発生しました: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
