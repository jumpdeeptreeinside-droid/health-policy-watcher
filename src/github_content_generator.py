#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions用 コンテンツ自動生成スクリプト

機能:
  1. Notionデータベースを監視
  2. Status(コンテンツ作成) が「執筆待ち(PDF)」or「執筆待ち(URL)」のページを検出
  3. Gemini API でブログ記事・Podcast台本を生成
  4. HealthPolicyWatcherDB の各ページ下に子ページとして保存:
       - ブログ記事 → Article(Web) プロパティにリンクを設定
       - Podcast台本 → Script(Podcast) プロパティにリンクを設定
  5. Status(コンテンツ作成) を「ファクトチェック待ち」に更新
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# ログ設定
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 設定読み込み（環境変数 → config.py フォールバック）
# ──────────────────────────────────────────────
def _load_config() -> tuple[str, str, str, str]:
    """(NOTION_API_KEY, NOTION_DATABASE_ID, GEMINI_API_KEY, GEMINI_MODEL) を返す"""
    notion_key  = os.environ.get("NOTION_API_KEY")
    notion_db   = os.environ.get("NOTION_DATABASE_ID")
    gemini_key  = os.environ.get("GEMINI_API_KEY")
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    if not (notion_key and notion_db and gemini_key):
        try:
            src_dir = Path(__file__).parent
            sys.path.insert(0, str(src_dir))
            import config as cfg
            notion_key  = notion_key  or cfg.NOTION_API_KEY
            notion_db   = notion_db   or cfg.NOTION_DATABASE_ID
            gemini_key  = gemini_key  or cfg.GEMINI_API_KEY
            gemini_model = gemini_model or getattr(cfg, "GEMINI_MODEL_NAME", "gemini-2.0-flash")
            logger.info("config.py から設定を読み込みました")
        except ImportError:
            logger.error("環境変数と config.py のどちらも見つかりません。")
            sys.exit(1)

    missing = [k for k, v in {
        "NOTION_API_KEY": notion_key,
        "NOTION_DATABASE_ID": notion_db,
        "GEMINI_API_KEY": gemini_key,
    }.items() if not v]
    if missing:
        logger.error(f"必須の設定が不足しています: {', '.join(missing)}")
        sys.exit(1)

    return notion_key, notion_db, gemini_key, gemini_model


NOTION_API_KEY, NOTION_DATABASE_ID, GEMINI_API_KEY, GEMINI_MODEL = _load_config()

# ──────────────────────────────────────────────
# Gemini クライアント
# ──────────────────────────────────────────────
try:
    from google import genai
    from google.genai import types as genai_types
    _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    logger.info(f"Gemini クライアント初期化完了 (model: {GEMINI_MODEL})")
except ImportError:
    logger.error("google-genai パッケージが見つかりません: pip install google-genai")
    sys.exit(1)

# ──────────────────────────────────────────────
# プロンプト定義
# ──────────────────────────────────────────────
PROMPT_BLOG = """
# 役割設定
あなたは、厚生労働省の医療政策資料分析のプロフェッショナルです。

# 出力内容
提供された資料（PDFまたはWebページ）を統合的に分析し、ブログ記事を作成してください。

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

### タイトル作成ルール
- 記事の主旨を正確に捉えつつ、読者が「おっ、読んでみようかな」と興味を持つフックを入れてください。
- 煽りすぎず、事実を淡々と伝える「信頼できる報道」のトーンを維持してください。
- 簡潔で、中身がひと目で伝わる言い回しにしてください。

### 校閲チェック
1. **ハルシネーション（捏造）チェック**: 元の情報の意味を歪めていないか。
2. **差別・不快表現**: 特定の疾患、職業、地域、個人に対する差別的・侮辱的な表現が含まれていないか。
3. **炎上リスク**: 政治的公平性を欠く表現や、過激すぎる表現がないか。
4. **誤字脱字・誤用**: 日本語として不自然な点や、明らかな誤変換がないか。

### 出力フォーマット
- 記事のタイトルを # で記載した後、本文を出力してください。
- 本文（マークダウン形式だが、装飾は最小限に）
- 数字・英語は「半角」、記号は「全角」を使用してください。
- 段落が変わる箇所には空行を入れてください。

# 重要な出力ルール
1. **入力されたコンテンツに含まれていない情報は、絶対に付け足さないでください。**
2. 事実関係（数字、固有名詞、日付）を勝手に変更しないでください。
3. 前置きや挨拶（「はい、作成します」等）は一切不要です。
4. 絵文字や顔文字は使用しないでください。
5. **個人名や配信者名は絶対に出力しないでください。**
"""

PROMPT_SCRIPT = """
# 役割設定
あなたは、医療政策ニュースを音声で読み上げるプロフェッショナルです。

# タスク
提供されたブログ記事を、音声読み上げ用のPodcast台本に変換してください。

## 変換ルール

### 内容
- **ブログ記事の内容をそのまま使用してください。情報の追加や変更は一切禁止です。**
- 引用部分（> で始まる行）と資料名・ページ番号の記載は削除してください。
- 本文のみを音声読み上げ用に最適化してください。

### 執筆ルール
- **文体:** 平易でリズムの良い「話し言葉（デスマス調）」
- **改行（最重要）:** 句点（。）が来るたびに必ず改行を入れる
- **一文の長さ:** 40-60文字目安で、息継ぎがしやすいように
- **語尾:** 「～です」「～ます」が3回以上連続しないように変化をつける
- **トーン:** 感情を込めすぎず、事実を淡々と伝える

### 出力フォーマット
- マークダウンや見出しは使わず、プレーンテキストで出力
- 冒頭の挨拶（「皆さん、こんにちは」など）や自己紹介は一切不要
- 記号は「全角」、数字・英語は「半角」に統一
- 引用ブロック（>）は削除

# 重要な制約
1. **ブログ記事に書かれていることだけを使用してください。**
2. 事実関係（数字、固有名詞、日付）を勝手に変更しないでください。
3. 前置きや挨拶は一切不要です。
4. **個人名や配信者名は絶対に出力しないでください。**
"""

# ──────────────────────────────────────────────
# Markdown → Notion ブロック変換
# ──────────────────────────────────────────────
def _make_rich_text(text: str, bold: bool = False) -> dict:
    obj: dict = {"type": "text", "text": {"content": text[:2000]}}
    if bold:
        obj["annotations"] = {"bold": True}
    return obj

def _parse_inline(text: str) -> list[dict]:
    """インラインMarkdown（**bold**）を Notion rich_text リストに変換"""
    parts: list[dict] = []
    for seg in re.split(r"(\*\*[^*]+\*\*)", text):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            content = seg[2:-2]
            while content:
                parts.append(_make_rich_text(content[:2000], bold=True))
                content = content[2000:]
        else:
            while seg:
                parts.append(_make_rich_text(seg[:2000]))
                seg = seg[2000:]
    return parts or [_make_rich_text("")]

def _block(block_type: str, rich_text: list[dict]) -> dict:
    return {"object": "block", "type": block_type, block_type: {"rich_text": rich_text}}

def markdown_to_notion_blocks(markdown_text: str) -> list[dict]:
    """
    Markdownテキストを Notion ブロックのリストに変換する。
    対応: # H1, ## H2, ### H3, > quote, - list, ---, 通常段落
    [temp id=N] タグは無視する。
    """
    blocks: list[dict] = []

    for line in markdown_text.splitlines():
        stripped = line.rstrip()

        # システムタグをスキップ
        if re.match(r"^\[temp id=\d+\]$", stripped):
            continue

        # 空行 → 直前が空段落でなければ空の段落を追加
        if not stripped:
            if blocks and not (
                blocks[-1]["type"] == "paragraph"
                and not blocks[-1]["paragraph"]["rich_text"][0]["text"]["content"]
            ):
                blocks.append(_block("paragraph", [_make_rich_text("")]))
            continue

        if stripped.startswith("### "):
            blocks.append(_block("heading_3", _parse_inline(stripped[4:])))
        elif stripped.startswith("## "):
            blocks.append(_block("heading_2", _parse_inline(stripped[3:])))
        elif stripped.startswith("# "):
            blocks.append(_block("heading_1", _parse_inline(stripped[2:])))
        elif stripped.startswith("> "):
            blocks.append(_block("quote", _parse_inline(stripped[2:])))
        elif stripped == ">":
            blocks.append(_block("quote", [_make_rich_text("")]))
        elif re.match(r"^[-*] ", stripped):
            blocks.append(_block("bulleted_list_item", _parse_inline(stripped[2:])))
        elif re.match(r"^[-*_]{3,}$", stripped):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
        else:
            blocks.append(_block("paragraph", _parse_inline(stripped)))

    # 末尾の空段落を除去
    while blocks and blocks[-1]["type"] == "paragraph" and \
            not blocks[-1]["paragraph"]["rich_text"][0]["text"]["content"]:
        blocks.pop()

    return blocks

# ──────────────────────────────────────────────
# Notion API クライアント
# ──────────────────────────────────────────────
class NotionAPI:
    BASE = "https://api.notion.com/v1"
    BLOCK_LIMIT = 100  # append_block_children の最大ブロック数

    def __init__(self, api_key: str, database_id: str):
        self.database_id = database_id
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict:
        r = requests.get(f"{self.BASE}{path}", headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        r = requests.post(f"{self.BASE}{path}", headers=self.headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict) -> dict:
        r = requests.patch(f"{self.BASE}{path}", headers=self.headers, json=payload, timeout=15)
        r.raise_for_status()
        return r.json()

    def query_pages(self, status_name: str) -> list[dict]:
        """指定ステータスのページ一覧を取得"""
        payload = {
            "filter": {
                "property": "Status(コンテンツ作成)",
                "status": {"equals": status_name},
            }
        }
        try:
            result = self._post(f"/databases/{self.database_id}/query", payload)
            return result.get("results", [])
        except Exception as e:
            logger.error(f"DB クエリエラー ({status_name}): {e}")
            return []

    def get_property(self, page: dict, name: str) -> Optional[str]:
        """ページプロパティの値を取得"""
        try:
            prop = page["properties"].get(name, {})
            t = prop.get("type")
            if t == "status":
                s = prop.get("status")
                return s["name"] if s else None
            if t == "url":
                return prop.get("url")
            if t == "title":
                arr = prop.get("title", [])
                return arr[0]["plain_text"] if arr else None
            if t == "rich_text":
                arr = prop.get("rich_text", [])
                return arr[0]["plain_text"] if arr else None
        except Exception:
            pass
        return None

    def create_child_page(
        self, parent_page_id: str, title: str, blocks: list[dict]
    ) -> Optional[str]:
        """
        親ページ下に子ページを作成し、ページIDを返す。
        100ブロック制限を考慮して分割してアップロードする。
        """
        # 最初の100ブロックと一緒にページを作成
        first_batch = blocks[: self.BLOCK_LIMIT]
        payload = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {"title": [{"text": {"content": title[:2000]}}]}
            },
            "children": first_batch,
        }
        try:
            result = self._post("/pages", payload)
            page_id = result["id"]
            logger.info(f"  子ページ作成: {title[:40]} (ID: {page_id})")
        except Exception as e:
            logger.error(f"  子ページ作成失敗: {e}")
            return None

        # 残りのブロックを追加（100件ずつ）
        remaining = blocks[self.BLOCK_LIMIT :]
        for i in range(0, len(remaining), self.BLOCK_LIMIT):
            batch = remaining[i : i + self.BLOCK_LIMIT]
            try:
                self._patch(
                    f"/blocks/{page_id}/children",
                    {"children": batch},
                )
            except Exception as e:
                logger.warning(f"  追加ブロックの書き込み失敗 (batch {i}): {e}")

        return page_id

    def update_properties(self, page_id: str, properties: dict) -> bool:
        """ページのプロパティを更新"""
        try:
            self._patch(f"/pages/{page_id}", {"properties": properties})
            return True
        except Exception as e:
            logger.error(f"  プロパティ更新失敗 (ID: {page_id}): {e}")
            return False

    def update_status(self, page_id: str, status_name: str) -> bool:
        """Status(コンテンツ作成) を更新"""
        return self.update_properties(
            page_id,
            {"Status(コンテンツ作成)": {"status": {"name": status_name}}},
        )

    def set_child_page_link(
        self, page_id: str, property_name: str, child_page_id: str
    ) -> bool:
        """
        Article(Web) / Script(Podcast) プロパティに子ページへのリンクを設定する。
        URL型・rich_text型の両方を試みる。
        """
        notion_url = f"https://www.notion.so/{child_page_id.replace('-', '')}"

        # まず URL 型として試みる
        try:
            self.update_properties(
                page_id, {property_name: {"url": notion_url}}
            )
            logger.info(f"  {property_name} を URL 型で更新しました")
            return True
        except Exception:
            pass

        # 次に rich_text 型として試みる
        try:
            self.update_properties(
                page_id,
                {
                    property_name: {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "リンクを開く", "link": {"url": notion_url}},
                            }
                        ]
                    }
                },
            )
            logger.info(f"  {property_name} を rich_text 型で更新しました")
            return True
        except Exception as e:
            logger.warning(f"  {property_name} の更新に失敗しました: {e}")
            return False

    @staticmethod
    def page_url(page_id: str) -> str:
        return f"https://www.notion.so/{page_id.replace('-', '')}"


# ──────────────────────────────────────────────
# MHLW クローラー（PDF ダウンロード）
# ──────────────────────────────────────────────
def _sanitize(text: str, max_len: int = 80) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r'[\\\/:*?"<>|]', "_", text)
    return text[:max_len].strip()

def crawl_mhlw_page(page_url: str, download_dir: str) -> list[str]:
    """
    MHLWページからPDFをダウンロードし、保存したファイルパスのリストを返す。
    GitHub Actions の一時ディレクトリに保存する。
    """
    logger.info(f"  クロール開始: {page_url}")
    downloaded: list[str] = []

    try:
        resp = requests.get(page_url, timeout=30)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        html = resp.text
    except Exception as e:
        logger.error(f"  ページ取得失敗: {e}")
        return downloaded

    soup = BeautifulSoup(html, "html.parser")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not re.search(r"\.pdf", href, re.IGNORECASE):
            continue

        # 絶対URLに変換
        if re.match(r"^https?://", href, re.IGNORECASE):
            abs_url = href
        elif href.startswith("/"):
            abs_url = "https://www.mhlw.go.jp" + href
        else:
            abs_url = urljoin(page_url, href)

        # ファイル名を推測
        link_text = link.get_text(strip=True)
        if link_text:
            guess = re.sub(r"［PDF形式：.*?］", "", link_text)
            guess = re.sub(r"\[PDF.*?\]", "", guess)
            name = _sanitize(guess) or "document"
        else:
            name = _sanitize(abs_url.split("/")[-1]) or "document"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"

        save_path = os.path.join(download_dir, name)
        # 重複回避
        counter = 1
        base = name[:-4]
        while os.path.exists(save_path):
            save_path = os.path.join(download_dir, f"{base}_{counter}.pdf")
            counter += 1

        try:
            pdf_resp = requests.get(abs_url, timeout=30)
            pdf_resp.raise_for_status()
            with open(save_path, "wb") as f:
                f.write(pdf_resp.content)
            logger.info(f"    PDF ダウンロード: {os.path.basename(save_path)}")
            downloaded.append(save_path)
        except Exception as e:
            logger.warning(f"    PDF ダウンロード失敗 {abs_url}: {e}")

    return downloaded

# ──────────────────────────────────────────────
# Webスクレイパー
# ──────────────────────────────────────────────
def scrape_article(url: str) -> dict:
    """URLから記事本文を取得して dict を返す"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "lxml")
    for el in soup(["script", "style", "nav", "footer", "header", "aside"]):
        el.decompose()

    title = (soup.find("title") or soup.find("h1") or soup.find("h2"))
    title_text = title.get_text(strip=True) if title else "タイトルなし"

    candidates = (
        soup.find_all("article")
        or soup.find_all("div", class_=["article", "content", "post", "entry"])
        or soup.find_all("main")
        or [soup.find("body")]
    )
    parts: list[str] = []
    for el in candidates:
        if not el:
            continue
        for p in el.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"]):
            t = p.get_text(strip=True)
            if t and len(t) > 10:
                parts.append(t)

    content = "\n\n".join(parts)
    if len(content) < 100:
        raise ValueError("記事本文が取得できませんでした（テキストが短すぎます）")

    return {"url": url, "title": title_text, "content": content}

# ──────────────────────────────────────────────
# Gemini コンテンツ生成
# ──────────────────────────────────────────────
def generate_blog_and_script_from_pdfs(
    pdf_paths: list[str], source_url: str, page_title: str
) -> tuple[Optional[str], Optional[str]]:
    """PDF複数ファイルからブログ記事と台本を生成して (blog, script) を返す"""
    client = _gemini_client
    uploaded: list = []

    # PDF をアップロード
    for path in pdf_paths:
        basename = os.path.basename(path)
        logger.info(f"  Gemini にアップロード中: {basename}")
        try:
            with open(path, "rb") as f:
                uf = client.files.upload(
                    file=f,
                    config={"display_name": basename, "mime_type": "application/pdf"},
                )
            # PROCESSING 待ち
            for _ in range(30):
                if uf.state.name != "PROCESSING":
                    break
                time.sleep(3)
                uf = client.files.get(name=uf.name)
            if uf.state.name == "ACTIVE":
                uploaded.append(uf)
            else:
                logger.warning(f"  アップロード失敗 (state={uf.state.name}): {basename}")
        except Exception as e:
            logger.warning(f"  アップロードエラー {basename}: {e}")

    if not uploaded:
        logger.error("  有効な PDF ファイルがアップロードできませんでした")
        return None, None

    pdf_name_list = "\n".join(f"- {os.path.basename(p)}" for p in pdf_paths)
    prompt_with_files = (
        f"{PROMPT_BLOG}\n\n"
        f"# 処理対象PDFファイル一覧（引用時はこのファイル名を正確に使用）:\n{pdf_name_list}"
    )

    try:
        logger.info("  [第1段階] ブログ記事を生成中...")
        blog_resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=uploaded + [prompt_with_files],
            config=genai_types.GenerateContentConfig(temperature=0.7),
        )
        blog_content = blog_resp.text.strip()

        time.sleep(2)

        logger.info("  [第2段階] Podcast 台本を生成中...")
        script_resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[blog_content, PROMPT_SCRIPT],
            config=genai_types.GenerateContentConfig(temperature=0.7),
        )
        script_content = script_resp.text.strip()

    except Exception as e:
        logger.error(f"  Gemini 生成エラー: {e}")
        return None, None
    finally:
        # アップロードしたファイルを削除
        for uf in uploaded:
            try:
                client.files.delete(name=uf.name)
            except Exception:
                pass

    return blog_content, script_content


def generate_blog_and_script_from_url(
    url: str, title: str
) -> tuple[Optional[str], Optional[str]]:
    """URLからブログ記事と台本を生成して (blog, script) を返す"""
    client = _gemini_client

    logger.info(f"  スクレイピング中: {url}")
    try:
        article = scrape_article(url)
    except Exception as e:
        logger.error(f"  スクレイピングエラー: {e}")
        return None, None

    article_text = f"URL: {article['url']}\nタイトル: {article['title']}\n\n{article['content'][:50000]}"

    try:
        logger.info("  [第1段階] ブログ記事を生成中...")
        blog_resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[article_text, PROMPT_BLOG],
            config=genai_types.GenerateContentConfig(temperature=0.7),
        )
        blog_content = blog_resp.text.strip()

        time.sleep(2)

        logger.info("  [第2段階] Podcast 台本を生成中...")
        script_resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[blog_content, PROMPT_SCRIPT],
            config=genai_types.GenerateContentConfig(temperature=0.7),
        )
        script_content = script_resp.text.strip()

    except Exception as e:
        logger.error(f"  Gemini 生成エラー: {e}")
        return None, None

    return blog_content, script_content

# ──────────────────────────────────────────────
# コンテンツを Notion に保存
# ──────────────────────────────────────────────
def _extract_title(markdown: str) -> tuple[str, str]:
    """
    先頭の `# タイトル` を抽出して (title, body) を返す。
    見つからない場合は最初の行をタイトルとする。
    """
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[i + 1:]).strip()
    return (lines[0].strip() if lines else "無題"), markdown


def save_to_notion(
    notion: NotionAPI,
    parent_page_id: str,
    page_title: str,
    source_url: str,
    blog_content: str,
    script_content: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    ブログ記事・台本を Notion の子ページとして保存し、
    (blog_page_id, script_page_id) を返す。
    """
    # ────── ブログ記事ページ ──────
    blog_title, blog_body = _extract_title(blog_content)

    # ページの冒頭に引用元情報を追加
    header_blocks = [
        _block("quote", [
            _make_rich_text("引用元: "),
            {"type": "text", "text": {"content": page_title, "link": {"url": source_url}}},
        ]),
        {"object": "block", "type": "divider", "divider": {}},
    ]
    blog_blocks = header_blocks + markdown_to_notion_blocks(blog_body)
    blog_page_id = notion.create_child_page(parent_page_id, blog_title, blog_blocks)

    # ────── Podcast 台本ページ ──────
    script_title_line, script_body = _extract_title(script_content)
    script_title = f"[台本] {script_title_line}" if not script_title_line.startswith("[台本]") else script_title_line

    # 台本はプレーンテキストなので、段落ブロックとして保存
    script_blocks: list[dict] = []
    for para in script_body.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        # 長い段落は 2000 文字で分割
        while para:
            chunk, para = para[:2000], para[2000:]
            script_blocks.append(_block("paragraph", [_make_rich_text(chunk)]))

    script_page_id = notion.create_child_page(parent_page_id, script_title, script_blocks)

    return blog_page_id, script_page_id

# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def process_pdf_pages(notion: NotionAPI) -> int:
    """執筆待ち(PDF) のページを処理してステータスを更新、成功件数を返す"""
    logger.info("\n── 執筆待ち(PDF) の処理開始 ──")
    pages = notion.query_pages("執筆待ち(PDF)")
    logger.info(f"{len(pages)} 件のページを検出")
    success = 0

    for page in pages:
        page_id = page["id"]
        title = notion.get_property(page, "Title") or "タイトルなし"
        source_url = notion.get_property(page, "URL(Source)")

        logger.info(f"\n処理中: {title[:60]}")

        if not source_url:
            logger.warning("  URL(Source) が空のためスキップ")
            continue

        # PDF を一時ディレクトリにダウンロード
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_paths = crawl_mhlw_page(source_url, tmpdir)

            if not pdf_paths:
                logger.warning(f"  PDF が見つかりませんでした: {source_url}")
                continue

            logger.info(f"  {len(pdf_paths)} 件の PDF をダウンロード完了")

            blog, script = generate_blog_and_script_from_pdfs(pdf_paths, source_url, title)
        # 一時ディレクトリは TemporaryDirectory の with ブロック終了時に自動削除

        if not blog or not script:
            logger.error("  コンテンツ生成失敗")
            continue

        # Notion に子ページを保存
        blog_pid, script_pid = save_to_notion(
            notion, page_id, title, source_url, blog, script
        )

        if blog_pid:
            notion.set_child_page_link(page_id, "Article(Web)", blog_pid)
        if script_pid:
            notion.set_child_page_link(page_id, "Script(Podcast)", script_pid)

        # ステータスを更新
        if notion.update_status(page_id, "ファクトチェック待ち"):
            logger.info("  ✓ ステータス更新: 執筆待ち(PDF) → ファクトチェック待ち")
            success += 1
        else:
            logger.error("  ✗ ステータス更新失敗")

        # API レート制限対策
        time.sleep(3)

    return success


def process_url_pages(notion: NotionAPI) -> int:
    """執筆待ち(URL) のページを処理してステータスを更新、成功件数を返す"""
    logger.info("\n── 執筆待ち(URL) の処理開始 ──")
    pages = notion.query_pages("執筆待ち(URL)")
    logger.info(f"{len(pages)} 件のページを検出")
    success = 0

    for page in pages:
        page_id = page["id"]
        title = notion.get_property(page, "Title") or "タイトルなし"
        source_url = notion.get_property(page, "URL(Source)")

        logger.info(f"\n処理中: {title[:60]}")

        if not source_url:
            logger.warning("  URL(Source) が空のためスキップ")
            continue

        blog, script = generate_blog_and_script_from_url(source_url, title)

        if not blog or not script:
            logger.error("  コンテンツ生成失敗")
            continue

        # Notion に子ページを保存
        blog_pid, script_pid = save_to_notion(
            notion, page_id, title, source_url, blog, script
        )

        if blog_pid:
            notion.set_child_page_link(page_id, "Article(Web)", blog_pid)
        if script_pid:
            notion.set_child_page_link(page_id, "Script(Podcast)", script_pid)

        # ステータスを更新
        if notion.update_status(page_id, "ファクトチェック待ち"):
            logger.info("  ✓ ステータス更新: 執筆待ち(URL) → ファクトチェック待ち")
            success += 1
        else:
            logger.error("  ✗ ステータス更新失敗")

        # API レート制限対策
        time.sleep(3)

    return success


def main() -> None:
    logger.info("=" * 60)
    logger.info("  Health Policy Watcher - コンテンツ自動生成 (GitHub Actions)")
    logger.info("=" * 60)

    notion = NotionAPI(NOTION_API_KEY, NOTION_DATABASE_ID)

    pdf_success = process_pdf_pages(notion)
    url_success = process_url_pages(notion)

    logger.info("\n" + "=" * 60)
    logger.info(f"  処理完了")
    logger.info(f"  PDF モード: {pdf_success} 件成功")
    logger.info(f"  URL モード: {url_success} 件成功")
    logger.info("=" * 60)

    if pdf_success == 0 and url_success == 0:
        logger.info("  処理対象がなかったか、すべてエラーでした。")


if __name__ == "__main__":
    main()
