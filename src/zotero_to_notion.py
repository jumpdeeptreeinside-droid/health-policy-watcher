#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zotero CSV → Notion StudyArticleDB 一括インポートスクリプト

使い方:
    python src/zotero_to_notion.py path/to/zotero_export.csv

Zoteroからのエクスポート手順:
    1. Zoteroで全論文を選択（Ctrl+A）
    2. 右クリック → 「選択したアイテムをエクスポート」
    3. フォーマット: CSV を選択 → 保存

注意:
    - AI処理（要約・翻訳）はしません。メタデータのみインポートします。
    - AI処理は重要な論文を「重要」ステータスにしてから手動で実行してください。
    - 既にNotionにあるDOI/URLは重複チェックでスキップします。
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

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
def _load_config() -> tuple[str, str]:
    """(NOTION_API_KEY, STUDY_DATABASE_ID) を返す"""
    notion_key = os.environ.get("NOTION_API_KEY")
    study_db   = os.environ.get("STUDY_DATABASE_ID")

    if not (notion_key and study_db):
        try:
            src_dir = Path(__file__).parent
            sys.path.insert(0, str(src_dir))
            import config as cfg
            notion_key = notion_key or cfg.NOTION_API_KEY
            study_db   = study_db   or getattr(cfg, "STUDY_DATABASE_ID", None)
            logger.info("config.py から設定を読み込みました")
        except ImportError:
            logger.error("config.py が見つかりません。")
            sys.exit(1)

    missing = [k for k, v in {
        "NOTION_API_KEY":    notion_key,
        "STUDY_DATABASE_ID": study_db,
    }.items() if not v]
    if missing:
        logger.error(f"必須設定が不足: {', '.join(missing)}")
        sys.exit(1)

    return notion_key, study_db


NOTION_API_KEY, STUDY_DATABASE_ID = _load_config()

# ──────────────────────────────────────────────
# Notion API
# ──────────────────────────────────────────────
class NotionStudyAPI:
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

    def fetch_all_urls(self) -> set[str]:
        """DB内の全 DOI/URL を取得して重複チェック用セットを返す"""
        logger.info("既存エントリの DOI/URL を取得中...")
        existing: set[str] = set()
        payload: dict = {"page_size": 100}
        cursor = None
        while True:
            if cursor:
                payload["start_cursor"] = cursor
            result = self._post(f"/databases/{self.database_id}/query", payload)
            for page in result.get("results", []):
                url_prop = page["properties"].get("DOI / URL", {})
                url_val  = url_prop.get("url")
                if url_val:
                    existing.add(url_val.strip())
            if result.get("has_more"):
                cursor = result["next_cursor"]
            else:
                break
        logger.info(f"既存エントリ: {len(existing)} 件")
        return existing

    def create_page(self, properties: dict) -> Optional[str]:
        """Notion にページを作成してページIDを返す"""
        try:
            result = self._post("/pages", {
                "parent": {"database_id": self.database_id},
                "properties": properties,
            })
            return result["id"]
        except Exception as e:
            logger.error(f"  ページ作成エラー: {e}")
            return None


# ──────────────────────────────────────────────
# Zotero CSV パース
# ──────────────────────────────────────────────

# Zotero CSV のカラム名（バージョンによって微妙に異なるため柔軟に対応）
COL_TITLE       = ["Title"]
COL_AUTHOR      = ["Author", "Authors"]
COL_YEAR        = ["Publication Year", "Year"]
COL_JOURNAL     = ["Publication Title", "Publication", "Journal"]
COL_DOI         = ["DOI"]
COL_URL         = ["Url", "URL"]
COL_ABSTRACT    = ["Abstract Note", "Abstract"]
COL_TAGS_MANUAL = ["Manual Tags"]
COL_TAGS_AUTO   = ["Automatic Tags"]
COL_ITEM_TYPE   = ["Item Type"]


def _find_col(row: dict, candidates: list[str]) -> str:
    """候補カラム名から最初に見つかった値を返す"""
    for col in candidates:
        if col in row and row[col].strip():
            return row[col].strip()
    return ""


def _normalize_doi_url(doi: str, url: str) -> Optional[str]:
    """DOIまたはURLをNotionのURL型として正規化"""
    if doi:
        doi = doi.strip()
        if doi.startswith("http"):
            return doi
        return f"https://doi.org/{doi}"
    if url:
        url = url.strip()
        if url.startswith("http"):
            return url
    return None


def _parse_authors(raw: str) -> str:
    """'Last, First; Last2, First2' 形式をそのまま返す（最大2000文字）"""
    return raw[:2000] if raw else ""


def _parse_tags(manual: str, auto: str) -> list[str]:
    """Manual Tags と Automatic Tags をマージして重複除去"""
    tags: list[str] = []
    for raw in [manual, auto]:
        if not raw:
            continue
        for tag in raw.split(";"):
            tag = tag.strip()
            if tag and tag not in tags:
                tags.append(tag[:100])
    return tags[:10]  # Notion multi_select は多すぎると遅いため最大10個


def parse_zotero_csv(filepath: str) -> list[dict]:
    """ZoteroのCSVファイルを読み込んでパース済みレコードのリストを返す"""
    records = []

    with open(filepath, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            item_type = _find_col(row, COL_ITEM_TYPE).lower()
            # 論文系のみ対象（ウェブページ・フォーラム投稿等は除外）
            EXCLUDED_TYPES = {"webpage", "forumpost", "blogpost", "email", "instantmessage"}
            if item_type and item_type in EXCLUDED_TYPES:
                continue

            title    = _find_col(row, COL_TITLE)
            authors  = _parse_authors(_find_col(row, COL_AUTHOR))
            year_str = _find_col(row, COL_YEAR)
            journal  = _find_col(row, COL_JOURNAL)
            doi      = _find_col(row, COL_DOI)
            url      = _find_col(row, COL_URL)
            abstract = _find_col(row, COL_ABSTRACT)
            tags     = _parse_tags(
                _find_col(row, COL_TAGS_MANUAL),
                _find_col(row, COL_TAGS_AUTO),
            )

            doi_url = _normalize_doi_url(doi, url)

            year: Optional[int] = None
            if year_str.isdigit():
                year = int(year_str)

            if not title:
                continue  # タイトルなしはスキップ

            records.append({
                "title":    title,
                "authors":  authors,
                "year":     year,
                "journal":  journal,
                "doi_url":  doi_url,
                "abstract": abstract[:2000] if abstract else None,
                "tags":     tags,
            })

    return records


# ──────────────────────────────────────────────
# Notion プロパティペイロード構築
# ──────────────────────────────────────────────

def _rt(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text[:2000]}}]}


def build_payload(rec: dict) -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    props: dict = {
        "Title": {
            "title": [{"text": {"content": rec["title"][:2000]}}]
        },
        "Reading Status": {"status": {"name": "未読"}},
        "Date Added":     {"date": {"start": today_str}},
    }

    if rec.get("authors"):
        props["Authors"] = _rt(rec["authors"])

    if rec.get("journal"):
        props["Journal"] = {"select": {"name": rec["journal"][:100]}}

    if rec.get("year"):
        props["Year"] = {"number": rec["year"]}

    if rec.get("doi_url"):
        props["DOI / URL"] = {"url": rec["doi_url"]}

    if rec.get("abstract"):
        props["Abstract"] = _rt(rec["abstract"])

    if rec.get("tags"):
        props["Keywords"] = {
            "multi_select": [{"name": t} for t in rec["tags"]]
        }

    return props


# ──────────────────────────────────────────────
# 一括インポート
# ──────────────────────────────────────────────

def bulk_import(csv_path: str) -> None:
    logger.info("=" * 60)
    logger.info("  Zotero → Notion 一括インポート")
    logger.info("=" * 60)

    # CSV 読み込み
    logger.info(f"CSV を読み込み中: {csv_path}")
    try:
        records = parse_zotero_csv(csv_path)
    except FileNotFoundError:
        logger.error(f"ファイルが見つかりません: {csv_path}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"CSV 読み込みエラー: {e}")
        sys.exit(1)

    logger.info(f"読み込み完了: {len(records)} 件")

    notion = NotionStudyAPI(NOTION_API_KEY, STUDY_DATABASE_ID)

    # 重複チェック用に既存URLを取得
    existing_urls = notion.fetch_all_urls()

    # インポート対象を絞り込み（DOI/URLなしも含めて全件）
    to_import = []
    skip_dup  = 0
    no_url    = 0
    for rec in records:
        # DOI/URLがある場合のみ重複チェック
        if rec.get("doi_url"):
            if rec["doi_url"] in existing_urls:
                skip_dup += 1
                continue
        else:
            no_url += 1
        to_import.append(rec)

    logger.info(f"インポート対象: {len(to_import)} 件")
    logger.info(f"  うち DOI/URLなし: {no_url} 件（メタデータのみ保存）")
    logger.info(f"スキップ (重複): {skip_dup} 件")

    if not to_import:
        logger.info("インポート対象なし。終了します。")
        return

    # 一括作成
    success = 0
    fail    = 0
    for i, rec in enumerate(to_import, 1):
        payload = build_payload(rec)
        page_id = notion.create_page(payload)

        if page_id:
            success += 1
            if i % 50 == 0 or i == len(to_import):
                logger.info(
                    f"  進捗: {i}/{len(to_import)} 件完了 "
                    f"(成功 {success} / 失敗 {fail})"
                )
        else:
            fail += 1
            logger.warning(f"  失敗: {rec['title'][:60]}")

        time.sleep(0.4)  # Notion API レート制限対策

    logger.info("\n" + "=" * 60)
    logger.info(f"  インポート完了: 成功 {success} 件 / 失敗 {fail} 件")
    logger.info(f"  ※ DOI/URLなし {no_url} 件はメタデータのみ保存しました")
    logger.info(f"  ※ DOI/URLを後から入力するとAI要約が自動生成されます")
    logger.info("=" * 60)


# ──────────────────────────────────────────────
# エントリポイント
# ──────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python src/zotero_to_notion.py path/to/zotero_export.csv")
        sys.exit(1)

    bulk_import(sys.argv[1])
