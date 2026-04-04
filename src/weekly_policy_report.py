#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
医療政策ウィークリーレポート自動生成スクリプト (Weekly Policy Report Generator)

機能:
  1. Notion DB から Status(コンテンツ作成)="完了" かつ直近7日間の記事を取得
  2. 各記事の Article(Web) Notion子ページから本文を取得
  3. Gemini API で1〜3行の客観的要約 + トピックタグを生成
  4. Gemini API で要約のファクトチェックレポートを生成（Article(Web)本文と比較）
  5. 要約 + タグ + ファクトチェックを Notion 子ページとして保存
     → Article(WeeklySummary) プロパティにリンクを設定
  6. 全記事を束ねた Markdown レポートを output/weekly_reports/ に保存（.md + .txt）
  7. 完了メールを送信

使用方法:
  python src/weekly_policy_report.py
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────
REPO_ROOT  = Path(__file__).parent.parent
OUTPUT_DIR = REPO_ROOT / "output" / "weekly_reports"
JST        = timezone(timedelta(hours=9))

# 発信元タグ（URLドメイン → タグ名）
SOURCE_TAG_MAP: dict[str, str] = {
    "mhlw.go.jp":    "#厚労省",
    "mof.go.jp":     "#財務省",
    "cao.go.jp":     "#内閣府",
    "meti.go.jp":    "#経産省",
    "who.int":       "#WHO",
    "worldbank.org": "#国際",
    "fip.org":       "#国際",
    "news.un.org":   "#国際",
    "hgpi.org":      "#日本医療政策機構",
    "med.or.jp":     "#日本医師会",
}

# トピックタグ候補（15個）
TOPIC_TAGS = [
    "#薬価", "#後発品・BS", "#承認・審査",
    "#診療報酬", "#保険制度", "#医療財政",
    "#医師働き方", "#薬局・調剤", "#看護・介護",
    "#規制改革", "#医療DX",
    "#感染症・ワクチン", "#予防・健康増進",
    "#ライフサイエンス産業", "#国際・WHO",
]
_TOPIC_TAGS_STR = " / ".join(TOPIC_TAGS)

WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]

NOTIFY_TO = "jump.deep.tree.inside@gmail.com"

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
# 設定読み込み（環境変数 → config.py の順）
# ──────────────────────────────────────────────
def _load_config() -> tuple[str, str, str, str, str, str]:
    """(notion_key, notion_db, gemini_key, gemini_model, gmail_address, gmail_pass) を返す"""
    notion_key    = os.environ.get("NOTION_API_KEY")
    notion_db     = os.environ.get("NOTION_DATABASE_ID")
    gemini_key    = os.environ.get("GEMINI_API_KEY")
    gemini_model  = os.environ.get("GEMINI_MODEL")
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
            if not gmail_address:
                gmail_address = getattr(cfg, "GMAIL_ADDRESS", None)
            if not gmail_pass:
                gmail_pass = getattr(cfg, "GMAIL_APP_PASSWORD", None)
            logger.info("config.py から設定読み込み完了")
        except ImportError:
            logger.error("環境変数と config.py のどちらも見つかりません。")
            sys.exit(1)

    if not gemini_model:
        gemini_model = "gemini-2.0-flash"

    missing = [k for k, v in {
        "NOTION_API_KEY":     notion_key,
        "NOTION_DATABASE_ID": notion_db,
        "GEMINI_API_KEY":     gemini_key,
    }.items() if not v]
    if missing:
        logger.error(f"必須設定が不足: {', '.join(missing)}")
        sys.exit(1)

    logger.info(f"使用モデル: {gemini_model}")
    return (notion_key, notion_db, gemini_key, gemini_model,
            gmail_address or "", gmail_pass or "")


# ──────────────────────────────────────────────
# プロンプト定義
# ──────────────────────────────────────────────

# [Step 1] ブログ記事本文 → 要約 + トピックタグ
PROMPT_SUMMARY_TAGS = """\
あなたは医療政策ニュースの中立的な編集者です。

## タスク
以下のブログ記事について、要約とトピックタグを JSON 形式で出力してください。

### 要約ルール
- 1〜3行（100文字以内）
- 客観的・中立的なトーン（事実のみ・現場への影響や解釈は含めない）
- 原文を直接引用せず、必ず言い換える

### トピックタグルール
- 以下の15個のリストから1〜3個を選ぶ:
  {topic_tags}
- リスト外で特に重要なタグが1つあれば extra_tag に追加（なければ null）

## 出力形式（JSONのみ・前置き不要）
{{
  "summary": "要約テキスト（100文字以内）",
  "topic_tags": ["#薬価"],
  "extra_tag": null
}}

## ブログ記事

タイトル: {title}

{blog_content}
"""

# [Step 2] ブログ記事本文 vs 生成した要約 → ファクトチェックレポート
PROMPT_FACTCHECK_SUMMARY = """\
以下の「ブログ記事（原文）」と「生成された要約」を比較し、
ファクトチェックレポートを日本語で作成してください。

## チェック項目

1. **数字・日付の照合**: ブログ記事の数字・パーセンテージ・日付が要約で正確に使われているか
2. **固有名詞の照合**: 人名・組織名・地名・法律名等が正確か
3. **情報の歪曲**: ブログ記事にない内容・解釈・推測が要約に含まれていないか

## 出力形式（Markdownのみ・前置き不要）

## ファクトチェックレポート

### 総合評価

[問題なし / 要確認 / 問題あり] 一行コメント

### 確認済み（正確）

- **数字・日付**: チェックした数字・日付の一覧（なければ「なし」）
- **固有名詞**: チェックした固有名詞の一覧（なければ「なし」）

### 要確認・修正箇所

なし / 問題点の説明

### コメント

補足・注意事項があれば記載（なければ省略可）

---

## ブログ記事（原文）

{blog_content}

---

## 生成された要約

{summary}
"""


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

    def query_completed_articles(self, days: int = 7) -> list[dict]:
        """
        Status(コンテンツ作成)="完了" かつ直近 days 日以内の記事を返す。
        Date(Search) の降順でソートし、最新記事が先頭になる。
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        payload: dict = {
            "filter": {
                "and": [
                    {
                        "property": "Status(コンテンツ作成)",
                        "status":   {"equals": "完了"},
                    },
                    {
                        "property": "Date(Search)",
                        "date":     {"on_or_after": since},
                    },
                ]
            },
            "sorts": [{"property": "Date(Search)", "direction": "ascending"}],
        }
        try:
            results: list[dict] = []
            cursor: Optional[str] = None
            while True:
                if cursor:
                    payload["start_cursor"] = cursor
                data = self._post(f"/databases/{self.database_id}/query", payload)
                results.extend(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
            return results
        except Exception as e:
            logger.error(f"DB クエリエラー: {e}")
            return []

    def get_property(self, page: dict, name: str) -> Optional[str]:
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
            if t == "date":
                d = prop.get("date")
                return d["start"] if d else None
        except Exception:
            pass
        return None

    def get_page_text(self, page_id: str) -> str:
        """Notion ページの全ブロックをプレーンテキストとして返す"""
        try:
            blocks: list[dict] = []
            cursor: Optional[str] = None
            while True:
                params: dict = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                r = requests.get(
                    f"{self.BASE}/blocks/{page_id}/children",
                    headers=self.headers, params=params, timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                blocks.extend(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
            lines: list[str] = []
            for b in blocks:
                btype = b.get("type", "")
                rich_text = b.get(btype, {}).get("rich_text", [])
                text = "".join(rt.get("plain_text", "") for rt in rich_text)
                if text:
                    lines.append(text)
            return "\n\n".join(lines)
        except Exception as e:
            logger.warning(f"  Notion ページ取得失敗 ({page_id[:8]}...): {e}")
            return ""

    def create_child_page(
        self, parent_page_id: str, title: str, blocks: list[dict]
    ) -> Optional[str]:
        """親ページ下に子ページを作成してページIDを返す（100ブロック制限対応）"""
        first_batch = blocks[:self.BLOCK_LIMIT]
        try:
            result = self._post("/pages", {
                "parent":     {"page_id": parent_page_id},
                "properties": {"title": {"title": [{"text": {"content": title[:2000]}}]}},
                "children":   first_batch,
            })
            page_id = result["id"]
            logger.info(f"  子ページ作成完了: {title[:50]} ({page_id[:8]}...)")
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

    def set_weekly_summary_link(self, page_id: str, child_page_id: str) -> bool:
        """Article(WeeklySummary) プロパティに子ページURLを設定する（URL型→rich_text型の順で試行）"""
        notion_url = f"https://www.notion.so/{child_page_id.replace('-', '')}"
        # URL型で試行
        try:
            self._patch(f"/pages/{page_id}", {
                "properties": {"Article(WeeklySummary)": {"url": notion_url}}
            })
            logger.info("  Article(WeeklySummary): URL型で更新完了")
            return True
        except Exception:
            pass
        # rich_text型で試行
        try:
            self._patch(f"/pages/{page_id}", {
                "properties": {
                    "Article(WeeklySummary)": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": "リンクを開く", "link": {"url": notion_url}},
                        }]
                    }
                }
            })
            logger.info("  Article(WeeklySummary): rich_text型で更新完了")
            return True
        except Exception as e:
            logger.warning(f"  Article(WeeklySummary) 更新失敗: {e}")
            return False


# ──────────────────────────────────────────────
# Notion ブロック生成ユーティリティ
# ──────────────────────────────────────────────
def _make_rich_text(text: str, bold: bool = False) -> dict:
    obj: dict = {"type": "text", "text": {"content": text[:2000]}}
    if bold:
        obj["annotations"] = {"bold": True}
    return obj


def _parse_inline(text: str) -> list[dict]:
    """**bold** を含むインラインテキストをリッチテキスト配列に変換する"""
    parts: list[dict] = []
    for seg in re.split(r"(\*\*[^*]+\*\*)", text):
        if not seg:
            continue
        if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
            parts.append(_make_rich_text(seg[2:-2], bold=True))
        else:
            parts.append(_make_rich_text(seg))
    return parts or [_make_rich_text("")]


def _block(btype: str, rich_text: list[dict]) -> dict:
    return {"object": "block", "type": btype, btype: {"rich_text": rich_text}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def markdown_to_notion_blocks(markdown_text: str) -> list[dict]:
    """ファクトチェックレポートのMarkdownをNotionブロックに変換する"""
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
        elif re.match(r"^[-*] ", stripped):
            blocks.append(_block("bulleted_list_item", _parse_inline(stripped[2:])))
        elif re.match(r"^[-*_]{3,}$", stripped):
            blocks.append(_divider())
        else:
            blocks.append(_block("paragraph", _parse_inline(stripped)))

    # 末尾の空段落を除去
    while blocks and blocks[-1]["type"] == "paragraph" and not any(
        rt["text"]["content"]
        for rt in blocks[-1]["paragraph"]["rich_text"]
        if rt.get("type") == "text"
    ):
        blocks.pop()
    return blocks


# ──────────────────────────────────────────────
# 発信元タグ判定
# ──────────────────────────────────────────────
def get_source_tag(url: str) -> str:
    """URL ドメインから発信元タグを返す。未知ドメインは #業界紙。"""
    url_lower = url.lower()
    for domain, tag in SOURCE_TAG_MAP.items():
        if domain in url_lower:
            return tag
    return "#業界紙"


# ──────────────────────────────────────────────
# Gemini 処理
# ──────────────────────────────────────────────
def extract_notion_page_id(url_or_id: str) -> Optional[str]:
    clean = url_or_id.replace("-", "")
    m = re.search(r"([0-9a-f]{32})", clean, re.IGNORECASE)
    return m.group(1) if m else None


def generate_summary_and_tags(
    title: str,
    blog_content: str,
    gemini_client,
    model: str,
) -> dict:
    """
    [Step 1] ブログ記事本文から要約 + トピックタグを生成する。
    Returns: {"summary": str, "topic_tags": list[str], "extra_tag": str|None}
    失敗時は空辞書を返す。
    """
    prompt = PROMPT_SUMMARY_TAGS.format(
        topic_tags=_TOPIC_TAGS_STR,
        title=title,
        blog_content=blog_content[:5000],
    )
    try:
        from google.genai import types as genai_types
        resp = gemini_client.models.generate_content(
            model=model,
            contents=[prompt],
            config=genai_types.GenerateContentConfig(temperature=0.2),
        )
        raw = resp.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logger.warning(f"  [Step1] JSON パース失敗: {e}")
        return {}
    except Exception as e:
        logger.warning(f"  [Step1] Gemini 呼び出し失敗: {e}")
        return {}


def generate_factcheck(
    blog_content: str,
    summary: str,
    gemini_client,
    model: str,
) -> str:
    """
    [Step 2] ブログ記事本文 vs 生成した要約のファクトチェックレポートを生成する。
    Returns: Markdown 形式のファクトチェックテキスト（失敗時は空文字列）
    """
    prompt = PROMPT_FACTCHECK_SUMMARY.format(
        blog_content=blog_content[:5000],
        summary=summary,
    )
    try:
        from google.genai import types as genai_types
        resp = gemini_client.models.generate_content(
            model=model,
            contents=[prompt],
            config=genai_types.GenerateContentConfig(temperature=0.1),
        )
        return resp.text.strip()
    except Exception as e:
        logger.warning(f"  [Step2] ファクトチェック生成失敗: {e}")
        return ""


# ──────────────────────────────────────────────
# Notion 子ページ: 要約 + タグ + ファクトチェック
# ──────────────────────────────────────────────
def build_summary_page_blocks(
    title: str,
    source_url: str,
    source_tag: str,
    summary: str,
    topic_tags: list[str],
    extra_tag: Optional[str],
    factcheck_md: str,
) -> list[dict]:
    """
    要約・タグ・ファクトチェックを格納する Notion ブロック一覧を生成する。
    """
    all_tags = [source_tag] + topic_tags
    if extra_tag:
        all_tags.append(extra_tag)
    tags_str = "　".join(all_tags)

    blocks: list[dict] = [
        # ── 記事情報 ──
        _block("heading_2", [_make_rich_text("要約")]),
        _block("paragraph", [_make_rich_text(summary)]),
        _block("paragraph", [_make_rich_text(tags_str)]),
        _block("paragraph", [
            _make_rich_text("元記事: "),
            {
                "type": "text",
                "text": {"content": source_url, "link": {"url": source_url}},
            }
        ] if source_url else [_make_rich_text("元記事: （URLなし）")]),
        _divider(),
    ]

    # ── ファクトチェックレポート ──
    if factcheck_md:
        blocks += markdown_to_notion_blocks(factcheck_md)
    else:
        blocks += [
            _block("heading_2", [_make_rich_text("ファクトチェックレポート")]),
            _block("paragraph", [_make_rich_text("（ファクトチェックの生成に失敗しました）")]),
        ]

    return blocks


# ──────────────────────────────────────────────
# 全体レポートフォーマット（.md / .txt 用）
# ──────────────────────────────────────────────
_CIRCLE_NUMS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def get_week_range(today: datetime) -> tuple[datetime, datetime, datetime]:
    """
    土曜日実行を想定した期間計算。
    - period_start: 1つ前の土曜日（today - 7日）
    - period_end:   今回の土曜日（today）
    - delivery:     翌日曜日（today + 1日）
    """
    prev_saturday = today - timedelta(days=7)
    next_sunday   = today + timedelta(days=1)
    return prev_saturday, today, next_sunday


def fmt_date_jp(d: datetime, with_weekday: bool = False) -> str:
    s = f"{d.year}年{d.month}月{d.day}日"
    if with_weekday:
        s += f"（{WEEKDAY_JP[d.weekday()]}）"
    return s


def format_weekly_report(articles_data: list[dict], today: datetime) -> str:
    prev_sat, this_sat, delivery = get_week_range(today)
    period_start  = fmt_date_jp(prev_sat, with_weekday=True)
    period_end    = f"{this_sat.month}月{this_sat.day}日（{WEEKDAY_JP[this_sat.weekday()]}）"
    delivery_date = fmt_date_jp(delivery, with_weekday=True)
    n = len(articles_data)

    lines: list[str] = [
        f"【{delivery_date}】医療政策ウォッチャーズ 医療政策ウィークリーレポート",
        "",
        f"期間： {period_start}〜 {period_end}配信： {delivery_date}記事数： {n}本",
        "",
    ]

    for i, art in enumerate(articles_data):
        num        = _CIRCLE_NUMS[i] if i < len(_CIRCLE_NUMS) else f"({i + 1})"
        title      = art["title"]
        source_tag = art["source_tag"]
        pub_date   = art.get("pub_date", "")
        web_url    = art.get("web_url", "")
        summary    = art.get("summary") or "（要約生成失敗）"
        topic_tags = art.get("topic_tags") or []
        extra_tag  = art.get("extra_tag")

        meta_parts = [source_tag.lstrip("#")]
        if pub_date:
            meta_parts.append(pub_date)
        meta_str = "・".join(meta_parts)

        all_tags = [source_tag] + topic_tags
        if extra_tag:
            all_tags.append(extra_tag)
        tags_str = " ".join(all_tags)

        lines += [
            f"## {num} {title}（{meta_str}）",
            "",
            f"{summary}{tags_str}",
            "",
        ]
        if web_url:
            lines += [f"▶ 解説記事はこちら→ {web_url}", ""]

    lines += [
        "原文は各リンクよりご確認ください。",
    ]

    return "\n".join(lines)


# ──────────────────────────────────────────────
# ファイル保存
# ──────────────────────────────────────────────
def save_report(content: str, today: datetime) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = today.strftime("%Y%m%d")
    md_path  = OUTPUT_DIR / f"{date_str}_weekly_policy_report.md"
    txt_path = OUTPUT_DIR / f"{date_str}_weekly_policy_report.txt"
    md_path.write_text(content, encoding="utf-8")
    txt_path.write_text(content, encoding="utf-8")
    logger.info(f"  保存完了: {md_path}")
    return md_path, txt_path


# ──────────────────────────────────────────────
# 完了メール送信
# ──────────────────────────────────────────────
def send_completion_email(
    report_content: str,
    article_count: int,
    today: datetime,
    md_path: Path,
    gmail_address: str,
    gmail_pass: str,
) -> None:
    if not gmail_address or not gmail_pass:
        logger.warning("  Gmail 設定未完了のためメール送信スキップ")
        return

    subject = (
        f"【医療政策ウィークリーレポート】{fmt_date_jp(today)}号 生成完了"
        f"（{article_count}本）"
    )
    body = f"""\
医療政策ウィークリーレポートが自動生成されました。

■ 生成日時: {fmt_date_jp(today)}
■ 記事数: {article_count}本
■ 保存先: {md_path}
■ Notion: 各記事の Article(WeeklySummary) プロパティにリンクを設定済み

レポート本文は添付の .md ファイルをご確認ください。
"""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = NOTIFY_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # .md ファイルを添付
    try:
        md_bytes = md_path.read_bytes()
        attachment = MIMEApplication(md_bytes, Name=md_path.name)
        attachment["Content-Disposition"] = f'attachment; filename="{md_path.name}"'
        msg.attach(attachment)
    except Exception as e:
        logger.warning(f"  添付ファイル読み込み失敗（本文のみ送信）: {e}")

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(gmail_address, gmail_pass)
            srv.sendmail(gmail_address, NOTIFY_TO, msg.as_string())
        logger.info(f"  完了メール送信: {NOTIFY_TO}")
    except Exception as e:
        logger.error(f"  メール送信失敗: {e}")


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def main() -> None:
    logger.info("=" * 60)
    logger.info("  医療政策ウィークリーレポート 自動生成")
    logger.info("=" * 60)

    notion_key, notion_db, gemini_key, gemini_model, gmail_address, gmail_pass = (
        _load_config()
    )

    try:
        from google import genai
        gemini_client = genai.Client(api_key=gemini_key)
        logger.info("Gemini クライアント初期化完了")
    except ImportError:
        logger.error("google-genai 未インストール: pip install google-genai")
        sys.exit(1)

    notion = NotionAPI(notion_key, notion_db)
    today  = datetime.now(JST)

    # ── 1. Notion から記事取得 ─────────────────────────────
    lookup_days = int(os.environ.get("WEEKLY_REPORT_DAYS", "7"))
    logger.info(f"\n[1] Notion から完了記事を取得中（直近{lookup_days}日間）...")
    pages = notion.query_completed_articles(days=lookup_days)
    logger.info(f"  {len(pages)} 件取得")

    if not pages:
        logger.info("  対象記事がありません。終了します。")
        return

    # ── 2. 各記事の処理 ────────────────────────────────────
    logger.info("\n[2] 各記事の要約・ファクトチェック・Notion保存...")
    articles_data: list[dict] = []

    for i, page in enumerate(pages, 1):
        page_id     = page["id"]
        title       = notion.get_property(page, "Title") or "タイトルなし"
        source_url  = notion.get_property(page, "URL(Source)") or ""
        web_url     = notion.get_property(page, "URL(Web)") or ""
        article_web = notion.get_property(page, "Article(Web)") or ""
        pub_date    = notion.get_property(page, "Date(Search)") or ""

        logger.info(f"\n  [{i}/{len(pages)}] {title[:60]}")

        # ── Article(Web) 子ページから本文取得 ──────────────
        blog_content = ""
        if article_web:
            pid = extract_notion_page_id(article_web)
            if pid:
                blog_content = notion.get_page_text(pid)
                logger.info(f"    Article(Web) 取得: {len(blog_content)} 文字")

        if not blog_content:
            logger.warning("    Article(Web) が空のためスキップ")
            continue

        # ── [Step 1] 要約 + トピックタグ生成 ─────────────
        logger.info("    [Step 1] 要約・タグ生成中...")
        step1 = generate_summary_and_tags(title, blog_content, gemini_client, gemini_model)
        time.sleep(1)

        summary    = step1.get("summary", "")
        topic_tags = step1.get("topic_tags", [])
        extra_tag  = step1.get("extra_tag")
        source_tag = get_source_tag(source_url)

        if not summary:
            logger.warning("    要約生成失敗。空文字列で続行します。")

        logger.info(f"    要約: {summary[:60]}")
        logger.info(f"    タグ: {topic_tags}")

        # ── [Step 2] ファクトチェックレポート生成 ──────────
        logger.info("    [Step 2] ファクトチェック中...")
        factcheck_md = generate_factcheck(blog_content, summary, gemini_client, gemini_model)
        time.sleep(1)

        if factcheck_md:
            logger.info("    ファクトチェック完了")
        else:
            logger.warning("    ファクトチェック生成失敗")

        # ── Notion 子ページ作成 ────────────────────────────
        child_title  = f"ウィークリー要約: {title[:60]}"
        child_blocks = build_summary_page_blocks(
            title, source_url, source_tag,
            summary, topic_tags, extra_tag, factcheck_md,
        )
        child_id = notion.create_child_page(page_id, child_title, child_blocks)

        if child_id:
            notion.set_weekly_summary_link(page_id, child_id)
            time.sleep(0.5)

        articles_data.append({
            "title":      title,
            "url":        source_url,
            "web_url":    web_url,
            "pub_date":   pub_date,
            "source_tag": source_tag,
            "summary":    summary,
            "topic_tags": topic_tags,
            "extra_tag":  extra_tag,
        })

    if not articles_data:
        logger.error("  処理可能な記事がありませんでした。終了します。")
        return

    # ── 3. 全体レポート整形・保存 ──────────────────────────
    logger.info("\n[3] 全体レポートを整形・保存中...")
    report_content = format_weekly_report(articles_data, today)
    md_path, _     = save_report(report_content, today)

    # ── 4. 完了メール ──────────────────────────────────────
    logger.info("\n[4] 完了メールを送信中...")
    send_completion_email(
        report_content, len(articles_data), today,
        md_path, gmail_address, gmail_pass,
    )

    logger.info("\n" + "=" * 60)
    logger.info("  ウィークリーレポート生成完了!")
    logger.info(f"  記事数: {len(articles_data)} 本")
    logger.info(f"  保存先: {md_path}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
