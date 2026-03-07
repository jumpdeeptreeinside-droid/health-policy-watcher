#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
週次レポート自動生成スクリプト (Weekly Report Generator)

機能:
  1. Notionデータベースで WeeklyReport? = "Yes" の記事を取得
  2. 各記事の URL(Source) から直接スクレイピングして元記事本文を取得
  3. Gemini API で週次レポート (note記事 + Marpスライドアウトライン) を生成
  4. 週次レポート本文を Notion 子ページとして保存
  5. Marpスライドを output/slides/ フォルダに .md ファイルとして保存
  6. 各ソース記事の Article(WeeklyReport) プロパティにリンクを設定
  7. WeeklyReport? ステータスを "完了" に更新
  8. Vol番号を config/weekly_vol.txt にインクリメントして保存
  9. 完了メールをGmailで送信（タイトル候補一覧 + Notionリンク）
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# パス定数（リポジトリルートからの相対位置）
# ──────────────────────────────────────────────
REPO_ROOT   = Path(__file__).parent.parent
VOL_FILE    = REPO_ROOT / "config" / "weekly_vol.txt"
SLIDES_DIR  = REPO_ROOT / "output" / "slides"

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
# 設定読み込み（環境変数 → config.py の順に参照）
# ──────────────────────────────────────────────
def _load_config() -> tuple[str, str, str, str, str, str, str]:
    """
    (NOTION_API_KEY, NOTION_DATABASE_ID, GEMINI_API_KEY,
     GEMINI_MODEL, WEEKLY_REPORT_PARENT_PAGE_ID,
     GMAIL_ADDRESS, GMAIL_APP_PASSWORD) を返す
    """
    notion_key    = os.environ.get("NOTION_API_KEY")
    notion_db     = os.environ.get("NOTION_DATABASE_ID")
    gemini_key    = os.environ.get("GEMINI_API_KEY")
    gemini_model  = os.environ.get("GEMINI_MODEL")
    parent_page   = os.environ.get("WEEKLY_REPORT_PARENT_PAGE_ID")
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_pass    = os.environ.get("GMAIL_APP_PASSWORD")

    if not (notion_key and notion_db and gemini_key):
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import config as cfg
            notion_key   = notion_key  or cfg.NOTION_API_KEY
            notion_db    = notion_db   or cfg.NOTION_DATABASE_ID
            gemini_key   = gemini_key  or cfg.GEMINI_API_KEY
            if not gemini_model:
                gemini_model = getattr(cfg, "GEMINI_MODEL_NAME", None)
            if not parent_page:
                parent_page = getattr(cfg, "WEEKLY_REPORT_PARENT_PAGE_ID", None)
            if not gmail_address:
                gmail_address = getattr(cfg, "GMAIL_ADDRESS", None)
            if not gmail_pass:
                gmail_pass = getattr(cfg, "GMAIL_APP_PASSWORD", None)
            logger.info("config.py から設定を読み込みました")
        except ImportError:
            logger.error("環境変数と config.py のどちらも見つかりません。")
            sys.exit(1)

    if not gemini_model:
        gemini_model = "gemini-2.0-flash"

    missing = [k for k, v in {
        "NOTION_API_KEY":               notion_key,
        "NOTION_DATABASE_ID":           notion_db,
        "GEMINI_API_KEY":               gemini_key,
        "WEEKLY_REPORT_PARENT_PAGE_ID": parent_page,
    }.items() if not v]
    if missing:
        logger.error(f"必須設定が不足: {', '.join(missing)}")
        sys.exit(1)

    if not gmail_address or not gmail_pass:
        logger.warning("GMAIL_ADDRESS / GMAIL_APP_PASSWORD が未設定です。メール送信はスキップされます。")

    logger.info(f"使用モデル: {gemini_model}")
    return notion_key, notion_db, gemini_key, gemini_model, parent_page, gmail_address or "", gmail_pass or ""


NOTION_API_KEY, NOTION_DATABASE_ID, GEMINI_API_KEY, GEMINI_MODEL, \
    WEEKLY_REPORT_PARENT_PAGE_ID, GMAIL_ADDRESS, GMAIL_APP_PASSWORD = _load_config()

NOTIFY_TO = "jump.deep.tree.inside@gmail.com"


# ──────────────────────────────────────────────
# Gemini クライアント
# ──────────────────────────────────────────────
try:
    from google import genai
    from google.genai import types as genai_types
    _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    logger.info("Gemini クライアント初期化完了")
except ImportError:
    logger.error("google-genai パッケージが見つかりません: pip install google-genai")
    sys.exit(1)


# ──────────────────────────────────────────────
# Vol番号管理
# ──────────────────────────────────────────────
def read_vol_number() -> int:
    """config/weekly_vol.txt から現在のVol番号を読み込む"""
    try:
        return int(VOL_FILE.read_text(encoding="utf-8").strip())
    except Exception as e:
        logger.error(f"Vol番号の読み込み失敗: {e}")
        sys.exit(1)


def write_next_vol_number(current_vol: int) -> None:
    """次回用にVol番号をインクリメントして書き込む"""
    try:
        VOL_FILE.write_text(str(current_vol + 1) + "\n", encoding="utf-8")
        logger.info(f"  Vol番号を {current_vol} → {current_vol + 1} に更新しました")
    except Exception as e:
        logger.error(f"Vol番号の書き込み失敗: {e}")


# ──────────────────────────────────────────────
# Web スクレイパー（github_content_generator.py と同一ロジック）
# ──────────────────────────────────────────────
_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def scrape_article(url: str) -> dict:
    """URL から記事本文を取得して dict を返す"""
    resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "lxml")

    title_el   = soup.find("title") or soup.find("h1") or soup.find("h2")
    title_text = title_el.get_text(strip=True) if title_el else "タイトルなし"

    entry_content = soup.find("div", class_="entry-content")
    if entry_content:
        candidates = [entry_content]
    else:
        for el in soup(["script", "style", "nav", "footer", "header", "aside"]):
            el.decompose()
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

    # 短い場合は og:description にフォールバック
    if len(content) < 100:
        og = (
            soup.find("meta", property="og:description")
            or soup.find("meta", attrs={"name": "description"})
        )
        if og:
            content = og.get("content", "").strip()

    # それでも短い場合は Jina AI Reader にフォールバック
    if len(content) < 100:
        try:
            jina_resp = requests.get(f"https://r.jina.ai/{url}", headers=_SCRAPE_HEADERS, timeout=30)
            jina_resp.raise_for_status()
            content = jina_resp.text
            logger.info(f"  Jina AI Reader にフォールバック ({len(content)} 文字)")
        except Exception:
            raise ValueError(f"記事本文を取得できませんでした: {url}")

    if len(content) < 100:
        raise ValueError(f"記事本文が短すぎます（スクレイピング失敗の可能性）: {url}")

    return {"url": url, "title": title_text, "content": content}


# ──────────────────────────────────────────────
# プロンプト定義
# ──────────────────────────────────────────────
PROMPT_WEEKLY_REPORT_TEMPLATE = """\
# 役割設定
あなたは「医療政策ウォッチャー」の編集パートナーであり、ヘルスケア業界の経営層および投資家に向けた「敏腕の戦略アナリスト」です。
薬学・公衆衛生の専門知を背景に、膨大な政策・エビデンス情報を整理し、それが「ビジネスや市場にどのようなインパクトを与えるか（So What?）」を鋭く翻訳するプロとして振る舞ってください。

# タスク
以下に提供する {article_count} 本の記事を基に、医療政策ウォッチャー週刊レポート（Vol.{vol_number}、{issue_date}号）を作成してください。

# 各記事の出力構成
各記事について、以下の3つのセクションを必ず含めてください。

**【事象の要約（What）】**
文章の核となる意味や重要なキーワードは改変せず、読者が短時間で内容を把握できるよう構造的に要約（箇条書きや太字を活用）。

**【ビジネス・市場へのインパクト（So What?）】**
この事象が、薬局・ドラッグストア業界の収益（P/L）や市場構造、競合優位性にどう影響するかを分析。投資家目線での「市場の機会と脅威」も示唆すること。

**【戦略的アクション（Next Step）】**
経営層や戦略担当者が、直近（あるいは次期事業計画に向けて）検討・決断すべき「次の一手」を具体的に提示すること。

# タイトル生成（最重要）
全記事を俯瞰し、以下の形式でタイトル候補を5つ提案してください。
- メインタイトル形式: 「○○するxx、△△する□□。」（動詞＋名詞の対比構造）
- 副題（ビジネスフック）: 「〜○○がもたらす市場再編と次の一手〜」形式
- 採用例: 「縮小するモノの経済、復権するヒトの資本。」「限界を迎える価格の論理、拡張される医療の境界。」

5つ提案した後、最も今回の内容にふさわしいものを選び、「## 決定タイトル:」として1行で明示してください。

# 制約
- 記事全体のトーンは客観的・知的・格調高く。「企業の生存戦略」を想起させること
- 要約は元の文章のニュアンスを最大限尊重し、AI特有の誇張表現は避けること
- ビジネスへの翻訳部分は薬局・ヘルスケア業界に特化した具体的なリスクやチャンスに踏み込むこと
- 前置きや挨拶（「はい、作成します」等）は一切不要
- 絵文字・顔文字は使用しないでください

# 出力フォーマット（マークダウン形式）

# 【Vol.{vol_number}】（{issue_date}号）

## 1本目: [記事タイトル]

### 【事象の要約（What）】
...

### 【ビジネス・市場へのインパクト（So What?）】
...

### 【戦略的アクション（Next Step）】
...

（2本目以降も同様の構成）

## タイトル候補

1. [タイトル1]。〜[副題1]〜
2. [タイトル2]。〜[副題2]〜
3. [タイトル3]。〜[副題3]〜
4. [タイトル4]。〜[副題4]〜
5. [タイトル5]。〜[副題5]〜

## 決定タイトル:
[決定したメインタイトル]。〜[決定した副題]〜

---

# 入力記事

{articles_text}
"""

PROMPT_WEEKLY_SLIDE_TEMPLATE = """\
# 役割設定
あなたは「医療政策ウォッチャー」の動画コンテンツ制作パートナーです。
週刊レポートの内容を、視覚的にわかりやすいMarpスライドのアウトラインに変換するプロとして振る舞ってください。

# タスク
以下の週刊レポート（Vol.{vol_number}、{issue_date}号）を基に、動画プレゼン用スライドをMarp形式で作成してください。

# スライド構成

## 1本目（無料パート: 15〜18枚）
```
1. タイトルスライド（Vol番号・日付・今週の{article_count}本のニュース一覧）
2. 今週のニュース一覧（1本目をハイライト表示）
3-4. データ提示（数字・事実で実態を示す）
5-7. 3つのポイント（What の箇条書き要約を各1スライドに展開）
8-9. 現場への影響（So What? から: 経営・連携・チャンス）
10. まとめ（3つの要点）
11-12. 有料版への誘導（2本目以降の紹介 + メンバーシップ案内）
13. 引用・出典
14. エンディング（「いってらっしゃい！」）
```

## 2本目以降（有料パート: 各8〜10枚）
```
1. タイトルスライド（○本目: ニュースタイトル）
2. データ提示または問題提起
3-5. 3つのポイント（各1スライド）
6-7. 現場への影響（So What? / Next Step から）
8. まとめ
```

# スライド作成ルール
- **形式**: Marp形式のMarkdown（必ず `---` でスライドを区切る）
- **1スライド = 1メッセージ**（伝えたいことは1つに絞る）
- **箇条書き**: 1スライドあたり3〜5項目まで
- **デザイン**: シンプル（馬田隆明さんスタイル）
- 重要な数字や言葉は **太字**
- 前置きや挨拶（「はい、作成します」等）は一切不要
- 絵文字は使用しないでください

# Marpヘッダー（必ず冒頭に付ける）
---
marp: true
theme: default
paginate: true
backgroundColor: #fff
---

# 出力
Marp形式のMarkdownをそのまま出力してください（コードブロックで囲まない）。

---

# 元となる週刊レポート

{report_content}
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
    blocks: list[dict] = []
    for line in markdown_text.splitlines():
        stripped = line.rstrip()
        if not stripped:
            if blocks and not (
                blocks[-1]["type"] == "paragraph"
                and not any(
                    rt["text"]["content"]
                    for rt in blocks[-1]["paragraph"]["rich_text"]
                    if rt.get("type") == "text"
                )
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

    while blocks and blocks[-1]["type"] == "paragraph" and not any(
        rt["text"]["content"]
        for rt in blocks[-1]["paragraph"]["rich_text"]
        if rt.get("type") == "text"
    ):
        blocks.pop()
    return blocks


def extract_title_from_markdown(content: str) -> tuple[str, str]:
    for i, line in enumerate(content.splitlines()):
        if line.startswith("# "):
            title = line[2:].strip()
            body  = "\n".join(content.splitlines()[i + 1:]).strip()
            return title, body
    lines = content.splitlines()
    return (lines[0].strip() if lines else "無題"), content


# ──────────────────────────────────────────────
# Notion API クライアント
# ──────────────────────────────────────────────
class NotionAPI:
    BASE        = "https://api.notion.com/v1"
    BLOCK_LIMIT = 100

    def __init__(self, api_key: str, database_id: str):
        self.database_id = database_id
        self.headers = {
            "Authorization":  f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type":   "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        r = requests.post(
            f"{self.BASE}{path}", headers=self.headers, json=payload, timeout=30
        )
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict) -> dict:
        r = requests.patch(
            f"{self.BASE}{path}", headers=self.headers, json=payload, timeout=30
        )
        r.raise_for_status()
        return r.json()

    def query_weekly_articles(self) -> list[dict]:
        """WeeklyReport? = "Yes" の記事一覧を取得"""
        payload = {
            "filter": {
                "property": "WeeklyReport?",
                "status":   {"equals": "Yes"},
            }
        }
        try:
            return self._post(f"/databases/{self.database_id}/query", payload).get("results", [])
        except Exception as e:
            logger.error(f"DB クエリエラー: {e}")
            return []

    def get_property(self, page: dict, name: str) -> Optional[str]:
        try:
            prop = page["properties"].get(name, {})
            t    = prop.get("type")
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
        """親ページ下に子ページを作成してページIDを返す（100ブロック制限対応）"""
        first_batch = blocks[: self.BLOCK_LIMIT]
        payload = {
            "parent":     {"page_id": parent_page_id},
            "properties": {"title": {"title": [{"text": {"content": title[:2000]}}]}},
            "children":   first_batch,
        }
        try:
            result  = self._post("/pages", payload)
            page_id = result["id"]
            logger.info(f"  子ページ作成完了: {title[:60]} (ID: {page_id[:8]}...)")
        except Exception as e:
            logger.error(f"  子ページ作成失敗 [{title[:40]}]: {e}")
            return None

        remaining = blocks[self.BLOCK_LIMIT:]
        for i in range(0, len(remaining), self.BLOCK_LIMIT):
            batch = remaining[i: i + self.BLOCK_LIMIT]
            try:
                self._patch(f"/blocks/{page_id}/children", {"children": batch})
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"  ブロック追加失敗 (batch {i // self.BLOCK_LIMIT + 2}): {e}")

        return page_id

    def update_properties(self, page_id: str, properties: dict) -> bool:
        try:
            self._patch(f"/pages/{page_id}", {"properties": properties})
            return True
        except Exception as e:
            logger.error(f"  プロパティ更新失敗 ({page_id[:8]}...): {e}")
            return False

    def set_child_page_link(
        self, page_id: str, property_name: str, child_page_id: str
    ) -> bool:
        """プロパティに子ページURLを設定（URL型 → rich_text型の順に試行）"""
        notion_url = f"https://www.notion.so/{child_page_id.replace('-', '')}"
        try:
            self.update_properties(page_id, {property_name: {"url": notion_url}})
            logger.info(f"  {property_name}: URL 型で更新完了")
            return True
        except Exception:
            pass
        try:
            self.update_properties(page_id, {
                property_name: {
                    "rich_text": [{
                        "type": "text",
                        "text": {"content": "リンクを開く", "link": {"url": notion_url}},
                    }]
                }
            })
            logger.info(f"  {property_name}: rich_text 型で更新完了")
            return True
        except Exception as e:
            logger.warning(f"  {property_name} 更新失敗: {e}")
            return False

    def update_weekly_report_status(self, page_id: str, status_name: str) -> bool:
        return self.update_properties(
            page_id,
            {"WeeklyReport?": {"status": {"name": status_name}}},
        )


# ──────────────────────────────────────────────
# Gemini コンテンツ生成
# ──────────────────────────────────────────────
def generate_weekly_report(
    articles: list[dict],
    vol_number: int,
    issue_date: str,
) -> tuple[Optional[str], Optional[str]]:
    """
    週次レポート本文と Marp スライドアウトラインを Gemini で生成する。

    Returns:
        (report_content, slide_content) — 失敗時は (None, None)
    """
    client = _gemini_client

    parts = []
    for i, art in enumerate(articles, 1):
        parts.append(
            f"## 記事{i}: {art['title']}\n"
            f"引用元: {art['url']}\n\n"
            f"{art['content']}"
        )
    articles_text = "\n\n---\n\n".join(parts)

    report_prompt = PROMPT_WEEKLY_REPORT_TEMPLATE.format(
        article_count=len(articles),
        vol_number=vol_number,
        issue_date=issue_date,
        articles_text=articles_text,
    )

    try:
        logger.info("  [Step 1] 週次レポート本文を生成中...")
        report_resp = _gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[report_prompt],
            config=genai_types.GenerateContentConfig(temperature=0.7),
        )
        report_content = report_resp.text.strip()
        logger.info(f"  週次レポート生成完了 ({len(report_content)} 文字)")
        time.sleep(2)

        slide_prompt = PROMPT_WEEKLY_SLIDE_TEMPLATE.format(
            vol_number=vol_number,
            issue_date=issue_date,
            article_count=len(articles),
            report_content=report_content,
        )
        logger.info("  [Step 2] Marp スライドアウトラインを生成中...")
        slide_resp = _gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[slide_prompt],
            config=genai_types.GenerateContentConfig(temperature=0.7),
        )
        slide_content = slide_resp.text.strip()
        logger.info(f"  Marp スライド生成完了 ({len(slide_content)} 文字)")

    except Exception as e:
        logger.error(f"  Gemini 生成エラー: {e}")
        return None, None

    return report_content, slide_content


# ──────────────────────────────────────────────
# Marpスライドをファイルに保存
# ──────────────────────────────────────────────
def save_slide_file(slide_content: str, vol_number: int, issue_date: str) -> Optional[Path]:
    """output/slides/ に Marp スライドを .md ファイルとして保存する"""
    try:
        SLIDES_DIR.mkdir(parents=True, exist_ok=True)
        date_tag  = issue_date.replace("年", "").replace("月", "").replace("日", "")
        filename  = f"{date_tag}_Vol{vol_number}_slides.md"
        filepath  = SLIDES_DIR / filename
        filepath.write_text(slide_content, encoding="utf-8")
        logger.info(f"  Marp スライド保存完了: {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"  Marp スライドの保存失敗: {e}")
        return None


# ──────────────────────────────────────────────
# タイトル候補抽出
# ──────────────────────────────────────────────
def extract_title_candidates(report_content: str) -> list[str]:
    """レポート本文からタイトル候補（1〜5番）を抽出する"""
    candidates: list[str] = []
    # "## タイトル候補" セクション以降の番号付きリストを探す
    in_section = False
    for line in report_content.splitlines():
        if re.match(r"##\s*タイトル候補", line):
            in_section = True
            continue
        if in_section:
            m = re.match(r"^\d+\.\s+(.+)$", line.strip())
            if m:
                candidates.append(m.group(1).strip())
            elif line.startswith("##") and candidates:
                break  # 次のセクションに入ったら終了
    return candidates[:5]


# ──────────────────────────────────────────────
# メール送信
# ──────────────────────────────────────────────
def send_completion_email(
    vol_number: int,
    issue_date: str,
    decided_title: str,
    candidates: list[str],
    notion_page_id: Optional[str],
    slide_filepath: Optional[Path],
) -> None:
    """完了メールを Gmail で送信する"""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        logger.warning("  Gmail 設定が未完了のためメール送信をスキップします")
        return

    notion_url = (
        f"https://www.notion.so/{notion_page_id.replace('-', '')}"
        if notion_page_id else "（Notionリンク取得失敗）"
    )
    slide_info = str(slide_filepath) if slide_filepath else "（スライド保存失敗）"

    candidates_text = "\n".join(
        f"  {i+1}. {c}" for i, c in enumerate(candidates)
    ) if candidates else "  （候補の抽出に失敗しました）"

    body = f"""\
【医療政策ウォッチャー】Vol.{vol_number} 週次レポートが自動生成されました。

■ 発行日
  {issue_date}号

■ タイトル候補（Gemini が提案した5案）
{candidates_text}

■ 自動決定タイトル
  {decided_title}

  ▶ タイトルを変更したい場合は、Notion のページタイトルを直接編集してください。

■ Notion レポートページ
  {notion_url}

■ Marp スライドファイル
  GitHub リポジトリ: output/slides/{slide_filepath.name if slide_filepath else ""}

以上、ご確認をお願いします。
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【Vol.{vol_number}】週次レポート生成完了 - タイトル確認のお願い（{issue_date}号）"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = NOTIFY_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_TO, msg.as_string())
        logger.info(f"  完了メール送信完了: {NOTIFY_TO}")
    except Exception as e:
        logger.error(f"  メール送信失敗: {e}")


# ──────────────────────────────────────────────
# GitHub Actions 用: 環境変数ファイルに値を出力
# ──────────────────────────────────────────────
def export_to_github_env(vol_number: int, issue_date: str) -> None:
    """GitHub Actions の GITHUB_ENV ファイルに変数を書き出す（ワークフロー間の値共有用）"""
    github_env = os.environ.get("GITHUB_ENV", "")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as f:
            f.write(f"WEEKLY_VOL_NUMBER={vol_number}\n")
            f.write(f"WEEKLY_ISSUE_DATE={issue_date}\n")
        logger.info(f"  GITHUB_ENV に Vol.{vol_number} / {issue_date} を書き出しました")


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def main() -> None:
    logger.info("=" * 60)
    logger.info("  Health Policy Watcher - 週次レポート自動生成")
    logger.info("=" * 60)

    notion = NotionAPI(NOTION_API_KEY, NOTION_DATABASE_ID)

    # ── 1. Vol番号を読み込む ─────────────────
    vol_number = read_vol_number()
    now_jst    = datetime.now(timezone.utc).astimezone()
    issue_date = f"{now_jst.year}年{now_jst.month}月{now_jst.day}日"
    logger.info(f"\n[1] Vol.{vol_number} / {issue_date}号 として生成します")
    export_to_github_env(vol_number, issue_date)

    # ── 2. WeeklyReport? = "Yes" の記事を取得 ─
    logger.info("\n[2] WeeklyReport? = 'Yes' の記事を検索中...")
    pages = notion.query_weekly_articles()
    logger.info(f"  {len(pages)} 件の記事が見つかりました")

    if not pages:
        logger.info("  対象記事がありません。終了します。")
        return
    if len(pages) < 2:
        logger.warning(f"  記事が {len(pages)} 本のみです（推奨: 4本）。このまま続行します。")

    # ── 3. URL(Source) から直接本文をスクレイピング ──
    logger.info("\n[3] 各記事の本文を URL(Source) から直接取得中...")
    article_data: list[dict] = []

    for page in pages:
        page_id    = page["id"]
        title      = notion.get_property(page, "Title") or "タイトルなし"
        source_url = notion.get_property(page, "URL(Source)") or ""

        logger.info(f"  処理中: {title[:60]}")

        if not source_url:
            logger.warning(f"  URL(Source) が空のためスキップ: {title[:40]}")
            continue

        try:
            art     = scrape_article(source_url)
            content = art["content"][:30000]  # Gemini のコンテキスト制限に配慮
            logger.info(f"  スクレイピング完了: {len(content)} 文字")
        except Exception as e:
            logger.warning(f"  スクレイピング失敗 ({source_url}): {e}")
            content = f"（本文取得失敗。元URL: {source_url}）"

        article_data.append({
            "title":   title,
            "url":     source_url,
            "content": content,
            "page_id": page_id,
        })
        time.sleep(1)

    if not article_data:
        logger.error("  記事データの取得に失敗しました。終了します。")
        return

    # ── 4. Gemini で週次レポートを生成 ──────
    logger.info("\n[4] Gemini で週次レポートを生成中...")
    report_content, slide_content = generate_weekly_report(
        article_data, vol_number, issue_date
    )

    if not report_content or not slide_content:
        logger.error("  コンテンツ生成に失敗しました。終了します。")
        return

    # ── 5. タイトル情報を抽出 ───────────────
    title_match   = re.search(r"##\s*決定タイトル[:：]\s*(.+)", report_content)
    decided_title = title_match.group(1).strip() if title_match else f"Vol.{vol_number}週次レポート"
    candidates    = extract_title_candidates(report_content)
    logger.info(f"  決定タイトル: {decided_title}")

    # ── 6. Notion に週次レポートを保存 ───────
    logger.info("\n[5] Notion に週次レポートを保存中...")
    report_page_title = f"【Vol.{vol_number}】{decided_title}（{issue_date}号）"
    _, report_body    = extract_title_from_markdown(report_content)
    report_blocks     = markdown_to_notion_blocks(report_body)
    report_page_id    = notion.create_child_page(
        WEEKLY_REPORT_PARENT_PAGE_ID, report_page_title, report_blocks
    )

    if not report_page_id:
        logger.error("  Notion ページの作成に失敗しました。終了します。")
        return

    # ── 7. Marpスライドをファイルに保存 ──────
    logger.info("\n[6] Marp スライドをファイルに保存中...")
    slide_filepath = save_slide_file(slide_content, vol_number, issue_date)

    # ── 8. ソース記事を更新 ──────────────────
    logger.info("\n[7] ソース記事を更新中...")
    for art in article_data:
        pid = art["page_id"]
        notion.set_child_page_link(pid, "Article(WeeklyReport)", report_page_id)
        if notion.update_weekly_report_status(pid, "完了"):
            logger.info(f"  ✓ WeeklyReport? → 完了: {art['title'][:50]}")
        else:
            logger.error(f"  ✗ ステータス更新失敗: {art['title'][:50]}")
        time.sleep(0.5)

    # ── 9. Vol番号をインクリメント ───────────
    logger.info("\n[8] Vol番号を更新中...")
    write_next_vol_number(vol_number)

    # ── 10. 完了メールを送信 ─────────────────
    logger.info("\n[9] 完了メールを送信中...")
    send_completion_email(
        vol_number, issue_date, decided_title,
        candidates, report_page_id, slide_filepath
    )

    # ── 完了サマリー ──────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("  週次レポート生成完了!")
    logger.info(f"  Vol.{vol_number} / {issue_date}号")
    logger.info(f"  タイトル: {decided_title}")
    logger.info(f"  ソース記事数: {len(article_data)} 本")
    logger.info(f"  Notion: {report_page_id}")
    logger.info(f"  スライド: {slide_filepath}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
