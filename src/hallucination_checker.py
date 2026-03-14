#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
コンテンツ生成ハルシネーションチェッカー (Hallucination Checker)

機能:
  1. Notion DB で Status(コンテンツ作成)="ファクトチェック待ち" の記事を取得
  2. Article(Web) Notion子ページから生成ブログ記事の本文を取得
  3. URL(Source) から元記事を再スクレイピング
  4. Gemini API で元記事 vs 生成記事を構造的に比較し、ハルシネーションを検出
  5. 検出結果を Notion 子ページとして保存
  6. PASS（スコア >= 85 かつ HIGH issues = 0）: Status を "完了" に自動更新
     WARN / FAIL: Status 維持 + 問題点の詳細をメールで通知

使用方法:
  python src/hallucination_checker.py
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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

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
# ハルシネーション検出プロンプト
# ──────────────────────────────────────────────
PROMPT_HALLUCINATION_CHECK = """\
あなたは医療政策コンテンツの品質管理エキスパートです。
以下の「元記事（原文）」と「生成ブログ記事」を詳細に比較し、
ハルシネーション（事実誤認・捏造・不正確な情報）を検出してください。

## チェック項目

1. 数字・統計の正確性: パーセンテージ・金額・件数・年月日
2. 固有名詞の正確性: 人名・組織名・法律名・制度名
3. 因果関係の歪曲: 元記事にない因果関係・含意の付加
4. 重要情報の欠落: 重要な条件・例外・留保事項の省略
5. 推測の事実化: 元記事にない推測が事実として記述されている

## 判定基準

- PASS: score >= 85 かつ HIGH severity の issues が 0件
- WARN: score 60〜84 または MEDIUM severity が1件以上
- FAIL: score < 60 または HIGH severity が1件以上

## 出力形式（JSONのみ・前置き不要）

{{
  "verdict": "PASS",
  "score": 92,
  "issues": [
    {{
      "severity": "LOW",
      "category": "因果関係の歪曲",
      "original": "元記事の記述（引用）",
      "generated": "生成記事の記述（引用）",
      "description": "問題点の説明（日本語）"
    }}
  ],
  "summary": "全体評価の一言コメント（日本語）"
}}

issues が空の場合は空配列 [] を出力してください。

## 元記事（原文）

{original_content}

---

## 生成ブログ記事

注意: 末尾に自動生成されたファクトチェックセクションが含まれる場合がありますが、
チェック対象はブログ記事の本文部分のみとしてください。

{generated_content}
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

    def query_factcheck_pending(self) -> list[dict]:
        """Status(コンテンツ作成) = "ファクトチェック待ち" の記事を取得"""
        payload: dict = {
            "filter": {
                "property": "Status(コンテンツ作成)",
                "status":   {"equals": "ファクトチェック待ち"},
            }
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

    def update_status(self, page_id: str, status_name: str) -> bool:
        """Status(コンテンツ作成) を更新する"""
        try:
            self._patch(f"/pages/{page_id}", {
                "properties": {
                    "Status(コンテンツ作成)": {"status": {"name": status_name}}
                }
            })
            return True
        except Exception as e:
            logger.warning(f"  ステータス更新失敗 ({page_id[:8]}...): {e}")
            return False

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
            logger.info(f"  チェックレポートページ作成完了: {title[:50]} ({page_id[:8]}...)")
        except Exception as e:
            logger.error(f"  子ページ作成失敗: {e}")
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


# ──────────────────────────────────────────────
# Notion ブロック生成ユーティリティ
# ──────────────────────────────────────────────
def _rt(text: str, bold: bool = False) -> dict:
    obj: dict = {"type": "text", "text": {"content": text[:2000]}}
    if bold:
        obj["annotations"] = {"bold": True}
    return obj


def _block(btype: str, text: str, bold: bool = False) -> dict:
    return {"object": "block", "type": btype, btype: {"rich_text": [_rt(text, bold)]}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


# ──────────────────────────────────────────────
# Web スクレイパー
# ──────────────────────────────────────────────
_SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def scrape_original_content(url: str) -> str:
    """元記事 URL から本文テキストを取得する。失敗時は空文字列を返す。"""
    if url.lower().endswith(".pdf"):
        # PDF は直接スクレイピング不可 → HTMLランディングページを試みる
        try:
            landing_url = url.rsplit("/", 1)[0] + "/"
            resp = requests.get(landing_url, headers=_SCRAPE_HEADERS, timeout=15)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
            soup = BeautifulSoup(resp.text, "lxml")
            og = (
                soup.find("meta", property="og:description")
                or soup.find("meta", attrs={"name": "description"})
            )
            return og.get("content", "").strip() if og else ""
        except Exception:
            return ""

    try:
        resp = requests.get(url, headers=_SCRAPE_HEADERS, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")

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
            for p in el.find_all(["p", "h1", "h2", "h3", "li"]):
                t = p.get_text(strip=True)
                if t and len(t) > 15:
                    parts.append(t)
        content = "\n".join(parts)

        if len(content) < 100:
            og = (
                soup.find("meta", property="og:description")
                or soup.find("meta", attrs={"name": "description"})
            )
            if og:
                content = og.get("content", "").strip()

        if len(content) < 80:
            try:
                jina = requests.get(
                    f"https://r.jina.ai/{url}", headers=_SCRAPE_HEADERS, timeout=20
                )
                jina.raise_for_status()
                content = jina.text[:10000]
                logger.debug(f"  Jina AI Reader にフォールバック")
            except Exception:
                pass

        return content[:8000]
    except Exception as e:
        logger.debug(f"  元記事スクレイピング失敗 ({url}): {e}")
        return ""


def extract_notion_page_id(url_or_id: str) -> Optional[str]:
    clean = url_or_id.replace("-", "")
    m = re.search(r"([0-9a-f]{32})", clean, re.IGNORECASE)
    return m.group(1) if m else None


# ──────────────────────────────────────────────
# Gemini: ハルシネーション検出
# ──────────────────────────────────────────────
def run_hallucination_check(
    original: str,
    generated: str,
    gemini_client,
    model: str,
) -> dict:
    """
    元記事 vs 生成記事を比較してハルシネーションを検出する。

    元記事が取得できなかった場合は WARN を返す（自動完了させない）。

    Returns:
        {"verdict": "PASS"|"WARN"|"FAIL", "score": int, "issues": list, "summary": str}
    """
    if not original:
        logger.warning("  元記事を取得できなかったため自動チェックをスキップ（WARN）")
        return {
            "verdict": "WARN",
            "score":   0,
            "issues":  [],
            "summary": "元記事を取得できなかったため自動チェックを実行できませんでした。手動確認をお願いします。",
        }

    prompt = PROMPT_HALLUCINATION_CHECK.format(
        original_content=original[:6000],
        generated_content=generated[:6000],
    )

    try:
        from google.genai import types as genai_types
        resp = gemini_client.models.generate_content(
            model=model,
            contents=[prompt],
            config=genai_types.GenerateContentConfig(temperature=0.1),
        )
        raw = resp.text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)
        result = json.loads(raw.strip())
        result.setdefault("verdict", "WARN")
        result.setdefault("score",   0)
        result.setdefault("issues",  [])
        result.setdefault("summary", "")
        return result
    except json.JSONDecodeError as e:
        logger.warning(f"  JSON パース失敗: {e}")
        return {
            "verdict": "WARN",
            "score":   0,
            "issues":  [],
            "summary": f"チェック結果のパースに失敗しました: {e}",
        }
    except Exception as e:
        logger.error(f"  Gemini 呼び出し失敗: {e}")
        return {
            "verdict": "WARN",
            "score":   0,
            "issues":  [],
            "summary": f"Gemini API エラー: {e}",
        }


# ──────────────────────────────────────────────
# Notion チェックレポートページ生成
# ──────────────────────────────────────────────
_VERDICT_LABEL = {
    "PASS": "PASS - 自動承認（スコア閾値クリア）",
    "WARN": "WARN - 手動確認が必要です",
    "FAIL": "FAIL - 修正が必要です",
}
_SEVERITY_LABEL = {
    "HIGH":   "[HIGH]   重大",
    "MEDIUM": "[MEDIUM] 中程度",
    "LOW":    "[LOW]    軽微",
}


def build_check_report_blocks(
    title: str,
    url: str,
    verdict_data: dict,
    checked_at: str,
) -> list[dict]:
    verdict = verdict_data.get("verdict", "WARN")
    score   = verdict_data.get("score",   0)
    issues  = verdict_data.get("issues",  [])
    summary = verdict_data.get("summary", "")

    blocks: list[dict] = [
        _block("heading_2", "ハルシネーションチェック結果"),
        _block("paragraph", f"実行日時: {checked_at}"),
        _block("paragraph", f"対象記事: {title}"),
        _block("paragraph", f"元記事URL: {url}"),
        _divider(),
        _block("heading_3", "判定結果"),
        _block("paragraph", _VERDICT_LABEL.get(verdict, verdict), bold=True),
        _block("paragraph", f"正確性スコア: {score} / 100"),
        _block("paragraph", f"総評: {summary}"),
        _divider(),
    ]

    if issues:
        blocks.append(
            _block("heading_3", f"検出された問題点（{len(issues)}件）")
        )
        for idx, issue in enumerate(issues, 1):
            sev  = _SEVERITY_LABEL.get(issue.get("severity", "LOW"), issue.get("severity", ""))
            cat  = issue.get("category", "")
            desc = issue.get("description", "")
            orig = issue.get("original", "")
            gen  = issue.get("generated", "")
            blocks += [
                _block("heading_3", f"問題 {idx}: {sev} - {cat}"),
                _block("paragraph", f"説明: {desc}"),
                _block("quote",     f"元記事: {orig}"),
                _block("quote",     f"生成記事: {gen}"),
            ]
    else:
        blocks.append(_block("paragraph", "問題点は検出されませんでした。"))

    return blocks


# ──────────────────────────────────────────────
# 結果メール送信
# ──────────────────────────────────────────────
def send_result_email(
    results: list[dict],
    gmail_address: str,
    gmail_pass: str,
) -> None:
    if not gmail_address or not gmail_pass:
        logger.warning("  Gmail 設定未完了のためメール送信スキップ")
        return

    passed = [r for r in results if r["verdict"] == "PASS"]
    warned = [r for r in results if r["verdict"] == "WARN"]
    failed = [r for r in results if r["verdict"] == "FAIL"]

    subject = (
        f"【ハルシネーションチェック完了】"
        f"PASS:{len(passed)} / WARN:{len(warned)} / FAIL:{len(failed)}"
    )

    lines = [
        "ハルシネーションチェックが完了しました。",
        "",
        "■ 結果サマリー",
        f"  PASS（自動完了）  : {len(passed)}件",
        f"  WARN（手動確認要）: {len(warned)}件",
        f"  FAIL（修正要）    : {len(failed)}件",
        "",
    ]

    for r in warned + failed:
        label = "WARN" if r["verdict"] == "WARN" else "FAIL"
        lines += [
            f"── [{label}] {r['title']} ──",
            f"  スコア: {r['score']}/100",
            f"  総評  : {r['summary']}",
        ]
        for issue in r.get("issues", [])[:3]:
            sev = issue.get("severity", "")
            cat = issue.get("category", "")
            desc = issue.get("description", "")
            lines.append(f"  [{sev}] {cat}: {desc}")
        lines.append("")

    if passed:
        lines.append("── PASS（自動完了済み）記事 ──")
        for r in passed:
            lines.append(f"  ・{r['title']} (スコア: {r['score']}/100)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = NOTIFY_TO
    msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as srv:
            srv.starttls()
            srv.login(gmail_address, gmail_pass)
            srv.sendmail(gmail_address, NOTIFY_TO, msg.as_string())
        logger.info(f"  結果メール送信: {NOTIFY_TO}")
    except Exception as e:
        logger.error(f"  メール送信失敗: {e}")


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def main() -> None:
    logger.info("=" * 60)
    logger.info("  コンテンツ ハルシネーションチェッカー")
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

    notion    = NotionAPI(notion_key, notion_db)
    now_jst   = datetime.now(JST)
    checked_at = now_jst.strftime("%Y年%m月%d日 %H:%M JST")

    # ── 1. ファクトチェック待ち記事を取得 ──────────────
    logger.info("\n[1] ファクトチェック待ち記事を取得中...")
    pages = notion.query_factcheck_pending()
    logger.info(f"  {len(pages)} 件取得")

    if not pages:
        logger.info("  チェック対象なし。終了します。")
        return

    # ── 2. 各記事のハルシネーションチェック ────────────
    results: list[dict] = []

    for i, page in enumerate(pages, 1):
        page_id     = page["id"]
        title       = notion.get_property(page, "Title") or "タイトルなし"
        source_url  = notion.get_property(page, "URL(Source)") or ""
        article_web = notion.get_property(page, "Article(Web)") or ""

        logger.info(f"\n[{i}/{len(pages)}] {title[:60]}")

        # ── 生成コンテンツ取得（Article(Web) Notion子ページ）──
        generated_content = ""
        if article_web:
            pid = extract_notion_page_id(article_web)
            if pid:
                generated_content = notion.get_page_text(pid)
                logger.info(f"  生成記事取得: {len(generated_content)} 文字")

        if not generated_content:
            logger.warning("  Article(Web) が空のためスキップ")
            continue

        # ── 元記事取得（URL(Source) を再スクレイピング）──
        original_content = ""
        if source_url:
            original_content = scrape_original_content(source_url)
            if original_content:
                logger.info(f"  元記事取得: {len(original_content)} 文字")
            else:
                logger.warning("  元記事取得失敗（WARN 判定となります）")

        # ── Gemini ハルシネーションチェック ──────────────
        logger.info("  Gemini チェック実行中...")
        check_result = run_hallucination_check(
            original_content, generated_content, gemini_client, gemini_model
        )
        time.sleep(2)  # API レート制限対策

        verdict = check_result["verdict"]
        score   = check_result["score"]
        logger.info(f"  判定: {verdict} (スコア: {score}/100, 問題: {len(check_result['issues'])}件)")

        # ── Notion に結果ページを作成 ─────────────────────
        page_title   = f"[{verdict}] ハルシネーションチェック - {title[:50]}"
        check_blocks = build_check_report_blocks(title, source_url, check_result, checked_at)
        notion.create_child_page(page_id, page_title, check_blocks)
        time.sleep(1)

        # ── ステータス更新（PASS のみ自動完了）────────────
        if verdict == "PASS":
            if notion.update_status(page_id, "完了"):
                logger.info("  ステータス: ファクトチェック待ち -> 完了（自動）")
            else:
                logger.error("  ステータス更新失敗")
        else:
            logger.info(f"  ステータス維持: {verdict}（手動確認が必要です）")

        results.append({
            "title":   title,
            "url":     source_url,
            "verdict": verdict,
            "score":   score,
            "summary": check_result.get("summary", ""),
            "issues":  check_result.get("issues", []),
        })

    # ── 3. 結果メール送信 ──────────────────────────────
    if results:
        logger.info("\n[3] 結果メールを送信中...")
        send_result_email(results, gmail_address, gmail_pass)

    # ── 完了サマリー ──────────────────────────────────
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    warned = sum(1 for r in results if r["verdict"] == "WARN")
    failed = sum(1 for r in results if r["verdict"] == "FAIL")

    logger.info("\n" + "=" * 60)
    logger.info("  ハルシネーションチェック完了!")
    logger.info(f"  PASS: {passed}件 / WARN: {warned}件 / FAIL: {failed}件")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
