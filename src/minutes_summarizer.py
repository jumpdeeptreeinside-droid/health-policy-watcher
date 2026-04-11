#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
議事録要約スクリプト (Minutes Summarizer)

機能:
  1. Notion DB から Status(議事録)="要約待ち" のページを取得
  2. URL(Source) から議事録HTMLをスクレイピング
  3. Gemini API で2,000文字の構造化要約を生成
  4. ファクトチェックレポートを生成
  5. Markdownレポートをメールで送信
  6. Status(議事録) を "完了" に更新

使用方法:
  python src/minutes_summarizer.py
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
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────
JST        = timezone(timedelta(hours=9))
OUTPUT_DIR = Path(__file__).parent.parent / "output" / "minutes_summaries"
HEADERS  = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}
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
# 設定読み込み
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
            notion_key    = notion_key   or cfg.NOTION_API_KEY
            notion_db     = notion_db    or cfg.NOTION_DATABASE_ID
            gemini_key    = gemini_key   or cfg.GEMINI_API_KEY
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
PROMPT_SUMMARY = """\
あなたは医療政策の専門的な編集者です。

## 絶対に守るルール（最重要）
- **提供されたテキストに書かれていることのみを使用してください**
- **外部知識・推測・補完は一切禁止です**
- テキストに記載がない数字・人名・発言内容・決定事項は絶対に含めないこと
- テキストが途中で終わっている議題は「（本文に記載なし）」と明記する
- 不確かな場合は書かない。「〜と考えられる」「〜の可能性がある」も使わない

## タスク
以下の審議会議事録テキストを読み、構造化された要約を JSON 形式で出力してください。

### 要約ルール
- 合計2,000文字程度（±200文字）
- 以下の構成で Markdown 形式で作成:
  - `## 会議概要` ── テキストに記載された日時・参加者・議題（150文字程度）
  - `## 議題① ○○について` ── 各議題の要点（議題数に応じて配分）
    - テキストに記載された議論の核心を簡潔に
    - テキストに記載された各立場の主な主張（診療側・支払側など）
    - テキストに記載された決定事項・結論
  - `## 今回のポイント` ── テキストから読み取れる注目点（200文字程度）
- 客観的・中立的なトーン
- 冗長な表現を避け、簡潔にまとめる

## 出力形式（JSONのみ・前置き不要）
{{
  "summary": "## 会議概要\\n..."
}}

## 議事録本文

タイトル: {title}

{text}
"""

PROMPT_FACTCHECK = """\
以下の「議事録原文」と「生成された要約」を比較し、
ファクトチェックレポートを日本語で作成してください。

## チェック項目

1. **数字・日付の照合**: 原文の数字・パーセンテージ・日付が要約で正確に使われているか
2. **固有名詞の照合**: 人名・組織名・法律名・制度名等が正確か
3. **情報の歪曲**: 原文にない内容・解釈・推測が要約に含まれていないか
4. **立場の正確性**: 各委員・各側の主張が正しく帰属されているか

## 出力形式（Markdownのみ・前置き不要）

## ファクトチェックレポート

### 総合評価

[問題なし / 要確認 / 問題あり] 一行コメント

### 確認済み（正確）

- **数字・日付**: チェックした数字・日付（なければ「なし」）
- **固有名詞**: チェックした固有名詞（なければ「なし」）

### 要確認・修正箇所

なし / 問題点の説明

### コメント

補足・注意事項があれば記載（なければ省略可）

---

## 議事録原文（抜粋）

{text}

---

## 生成された要約

{summary}
"""


# ──────────────────────────────────────────────
# Notion API クライアント
# ──────────────────────────────────────────────
class NotionAPI:
    BASE = "https://api.notion.com/v1"

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

    def query_pending_minutes(self) -> list[dict]:
        """Status(議事録)="要約待ち" のページを返す"""
        payload: dict = {
            "filter": {
                "property": "Status(議事録)",
                "status":   {"equals": "要約待ち"},
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
            if t == "title":
                arr = prop.get("title", [])
                return arr[0]["plain_text"] if arr else None
            if t == "rich_text":
                arr = prop.get("rich_text", [])
                return arr[0]["plain_text"] if arr else None
            if t == "url":
                return prop.get("url")
            if t == "date":
                d = prop.get("date")
                return d["start"] if d else None
            if t == "status":
                s = prop.get("status")
                return s["name"] if s else None
        except Exception:
            pass
        return None

    def set_status_minutes(self, page_id: str, status_name: str) -> bool:
        """Status(議事録) を更新する"""
        try:
            self._patch(f"/pages/{page_id}", {
                "properties": {
                    "Status(議事録)": {"status": {"name": status_name}}
                }
            })
            logger.info(f"  Status(議事録) → {status_name}")
            return True
        except Exception as e:
            logger.warning(f"  Status(議事録) 更新失敗: {e}")
            return False


# ──────────────────────────────────────────────
# 議事録スクレイピング
# ──────────────────────────────────────────────
def scrape_minutes_text(url: str) -> str:
    """
    議事録URLからテキストを取得する。
    厚労省の議事録ページはテキストノード中心の構成のため get_text() で全文取得する。
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        # スクリプト・スタイル・ナビゲーション等を除去
        for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
            tag.decompose()

        # get_text() で全テキストを取得し、空行を整理
        raw = soup.get_text(separator="\n")
        lines = [ln.strip() for ln in raw.splitlines()]
        # 空行の連続を1行に圧縮し、短すぎる行を除去
        cleaned: list[str] = []
        prev_blank = False
        for ln in lines:
            if not ln:
                if not prev_blank:
                    cleaned.append("")
                prev_blank = True
            else:
                cleaned.append(ln)
                prev_blank = False

        text = "\n".join(cleaned).strip()
        logger.info(f"  スクレイピング完了: {len(text)} 文字")
        return text

    except Exception as e:
        logger.warning(f"  スクレイピング失敗 ({url}): {e}")
        return ""


# ──────────────────────────────────────────────
# Gemini 処理
# ──────────────────────────────────────────────
def generate_summary(
    title: str,
    text: str,
    gemini_client,
    model: str,
) -> dict:
    """
    議事録テキストから要約を生成する。
    Returns: {"summary": str}
    """
    prompt = PROMPT_SUMMARY.format(title=title, text=text)

    try:
        from google.genai import types as genai_types
        resp = gemini_client.models.generate_content(
            model=model,
            contents=[prompt],
            config=genai_types.GenerateContentConfig(temperature=0.2),
        )
        raw = resp.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$",           "", raw, flags=re.MULTILINE)
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        logger.warning(f"  [Step1] JSON パース失敗: {e}")
        return {}
    except Exception as e:
        logger.warning(f"  [Step1] Gemini 呼び出し失敗: {e}")
        return {}


def generate_factcheck(
    text: str,
    summary: str,
    gemini_client,
    model: str,
) -> str:
    """要約のファクトチェックレポートを生成する"""
    prompt = PROMPT_FACTCHECK.format(
        text=text,
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
# レポートフォーマット
# ──────────────────────────────────────────────
def format_report(
    title: str,
    source_url: str,
    pub_date: str,
    summary: str,
    factcheck_md: str,
) -> str:
    today = datetime.now(JST).strftime("%Y年%m月%d日")
    lines: list[str] = [
        f"# 【議事録要約】{title}",
        f"配信: {today}　原文: {source_url}",
        "",
        "---",
        "",
        summary,
        "",
    ]

    # ファクトチェックセクション（文字数外）
    lines += ["---", ""]
    if factcheck_md:
        lines.append(factcheck_md)
    else:
        lines += [
            "## ファクトチェックレポート",
            "（ファクトチェックの生成に失敗しました）",
        ]

    return "\n".join(lines)


# ──────────────────────────────────────────────
# メール送信
# ──────────────────────────────────────────────
def save_report(content: str, today: datetime) -> Path:
    """レポートを .md ファイルとして保存しパスを返す"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = today.strftime("%Y%m%d")
    md_path  = OUTPUT_DIR / f"{date_str}_minutes_summary.md"
    md_path.write_text(content, encoding="utf-8")
    logger.info(f"  保存完了: {md_path}")
    return md_path


def send_email(
    reports: list[dict],
    gmail_address: str,
    gmail_pass: str,
) -> None:
    if not gmail_address or not gmail_pass:
        logger.warning("  Gmail 設定未完了のためメール送信スキップ")
        return

    today     = datetime.now(JST)
    today_str = today.strftime("%Y年%m月%d日")
    count     = len(reports)
    subject   = f"【議事録要約】{today_str}（{count}件）"

    # 複数件ある場合は1つの .md ファイルにまとめる
    body_parts: list[str] = []
    for r in reports:
        body_parts.append(r["content"])
        body_parts.append("\n\n" + "=" * 60 + "\n\n")
    full_content = "".join(body_parts).rstrip("= \n")

    # .md ファイルに保存
    md_path = save_report(full_content, today)

    body = (
        f"議事録要約レポートが自動生成されました。\n\n"
        f"■ 生成日時: {today_str}\n"
        f"■ 件数: {count}件\n\n"
        f"レポート本文は添付の .md ファイルをご確認ください。"
    )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = NOTIFY_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))

    # .md ファイルを添付
    try:
        attachment = MIMEApplication(md_path.read_bytes(), Name=md_path.name)
        attachment["Content-Disposition"] = f'attachment; filename="{md_path.name}"'
        msg.attach(attachment)
    except Exception as e:
        logger.warning(f"  添付ファイル読み込み失敗（本文のみ送信）: {e}")

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(gmail_address, gmail_pass)
            srv.sendmail(gmail_address, NOTIFY_TO, msg.as_string())
        logger.info(f"  メール送信完了: {NOTIFY_TO} ({count}件)")
    except Exception as e:
        logger.error(f"  メール送信失敗: {e}")


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def main() -> None:
    logger.info("=" * 60)
    logger.info("  議事録要約 自動生成")
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

    # ── 1. 要約待ちページを取得 ────────────────────────────
    logger.info("\n[1] Status(議事録)='要約待ち' のページを取得中...")
    pages = notion.query_pending_minutes()
    logger.info(f"  {len(pages)} 件取得")

    if not pages:
        logger.info("  対象ページがありません。終了します。")
        return

    # ── 2. 各ページを処理 ──────────────────────────────────
    logger.info("\n[2] 各議事録を処理中...")
    reports: list[dict] = []

    for i, page in enumerate(pages, 1):
        page_id    = page["id"]
        title      = notion.get_property(page, "Title") or "タイトルなし"
        source_url = notion.get_property(page, "URL(Source)") or ""
        pub_date   = notion.get_property(page, "Date(Search)") or ""

        logger.info(f"\n  [{i}/{len(pages)}] {title[:70]}")

        if not source_url:
            logger.warning("  URL(Source) が空のためスキップ")
            continue

        # ── スクレイピング ─────────────────────────────────
        logger.info("  議事録テキストを取得中...")
        text = scrape_minutes_text(source_url)
        if not text:
            logger.warning("  テキスト取得失敗のためスキップ")
            continue

        # ── [Step 1] 要約生成 ─────────────────────────────
        logger.info("  [Step 1] 要約生成中...")
        result = generate_summary(title, text, gemini_client, gemini_model)
        time.sleep(2)

        summary = result.get("summary", "")

        if not summary:
            logger.warning("  要約生成失敗。スキップします。")
            continue

        logger.info(f"  要約: {len(summary)} 文字")

        # ── [Step 2] ファクトチェック ─────────────────────
        logger.info("  [Step 2] ファクトチェック中...")
        factcheck_md = generate_factcheck(text, summary, gemini_client, gemini_model)
        time.sleep(2)

        # ── レポート整形 ───────────────────────────────────
        content = format_report(
            title, source_url, pub_date,
            summary, factcheck_md,
        )

        reports.append({"title": title, "content": content})

        # ── Notion ステータス更新 ──────────────────────────
        notion.set_status_minutes(page_id, "完了")
        time.sleep(0.5)

    if not reports:
        logger.info("\n処理できた議事録がありませんでした。終了します。")
        return

    # ── 3. メール送信 ──────────────────────────────────────
    logger.info(f"\n[3] メールを送信中（{len(reports)}件）...")
    send_email(reports, gmail_address, gmail_pass)

    logger.info("\n" + "=" * 60)
    logger.info(f"  議事録要約 完了: {len(reports)} 件")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
