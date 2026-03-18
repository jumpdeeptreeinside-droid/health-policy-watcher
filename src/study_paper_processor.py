#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StudyArticleDB 論文メタデータ自動処理スクリプト

機能:
  1. Notion StudyArticleDB を監視
  2. 「DOI / URL」が入力済みで「Authors」が空のエントリを検出
  3. CrossRef / PubMed / arXiv / スクレイピングでメタデータを取得
  4. Gemini API でアブストラクト日本語訳・AI要約・キーワード・関連度スコアを生成
  5. Notion ページを更新
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
# 設定読み込み（環境変数 → config.py の順）
# ──────────────────────────────────────────────
def _load_config() -> tuple[str, str, str, str]:
    """(NOTION_API_KEY, STUDY_DATABASE_ID, GEMINI_API_KEY, GEMINI_MODEL) を返す"""
    notion_key   = os.environ.get("NOTION_API_KEY")
    study_db     = os.environ.get("STUDY_DATABASE_ID")
    gemini_key   = os.environ.get("GEMINI_API_KEY")
    gemini_model = os.environ.get("GEMINI_MODEL")

    if not (notion_key and study_db and gemini_key):
        try:
            src_dir = Path(__file__).parent
            sys.path.insert(0, str(src_dir))
            import config as cfg
            notion_key   = notion_key   or cfg.NOTION_API_KEY
            study_db     = study_db     or getattr(cfg, "STUDY_DATABASE_ID", None)
            gemini_key   = gemini_key   or cfg.GEMINI_API_KEY
            if not gemini_model:
                gemini_model = getattr(cfg, "GEMINI_MODEL_NAME", None)
            logger.info("config.py から設定を読み込みました")
        except ImportError:
            logger.error("環境変数と config.py のどちらも見つかりません。")
            sys.exit(1)

    if not gemini_model:
        gemini_model = "gemini-2.0-flash"

    missing = [k for k, v in {
        "NOTION_API_KEY":    notion_key,
        "STUDY_DATABASE_ID": study_db,
        "GEMINI_API_KEY":    gemini_key,
    }.items() if not v]
    if missing:
        logger.error(f"必須設定が不足: {', '.join(missing)}")
        sys.exit(1)

    logger.info(f"使用モデル: {gemini_model}")
    return notion_key, study_db, gemini_key, gemini_model


NOTION_API_KEY, STUDY_DATABASE_ID, GEMINI_API_KEY, GEMINI_MODEL = _load_config()

HEADERS = {
    "User-Agent": (
        "StudyPaperProcessor/1.0 (health-policy-watcher; "
        "mailto:jump.deep.tree.inside@gmail.com)"
    )
}

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
# Gemini プロンプト
# ──────────────────────────────────────────────
PROMPT_STUDY_ANALYSIS = """\
あなたは医療・薬学分野の論文を専門とする研究アシスタントです。

以下の論文情報をもとに、指定のJSON形式で出力してください。
前置きや説明文は一切不要です。JSONのみを出力してください。

## 出力形式（strict JSON）
{{
  "abstract_ja":  "アブストラクトの日本語訳（500字以内）",
  "summary_ja":   "薬局薬剤師・医療政策担当者向けの日本語要約（200〜400文字）",
  "keywords":     ["キーワード1", "キーワード2"],
  "relevance":    3,
  "title_ja":     "論文タイトルの日本語訳（タイトルが日本語の場合は原文をそのまま）"
}}

## 関連度スコア基準（整数 1〜5）
- 5: 保険薬局・医薬品政策・診療報酬に直接関連
- 4: 医療制度・医療経済・薬物療法に関連
- 3: 疾患管理・治療効果研究（薬局業務に応用可能）
- 2: 基礎医学・間接的な関連
- 1: 関連性低

## keywords の指針
- 日本語で記載、5〜10個
- 論文の主要概念・対象疾患・介入・アウトカムを含める
- 「研究」「論文」などの一般語は除く

## 入力論文情報
タイトル: {title}
著者: {authors}
雑誌: {journal}
年: {year}
アブストラクト:
{abstract}
"""

# ──────────────────────────────────────────────
# Notion API クライアント
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

    def _patch(self, path: str, payload: dict) -> dict:
        r = requests.patch(
            f"{self.BASE}{path}", headers=self.headers, json=payload, timeout=30
        )
        r.raise_for_status()
        return r.json()

    def query_pending(self) -> list[dict]:
        """DOI/URL 入力済みかつ Authors が空のページ一覧を取得"""
        payload = {
            "filter": {
                "and": [
                    {"property": "DOI / URL", "url": {"is_not_empty": True}},
                    {"property": "Authors",   "rich_text": {"is_empty": True}},
                ]
            }
        }
        pages = []
        cursor = None
        while True:
            if cursor:
                payload["start_cursor"] = cursor
            try:
                result = self._post(f"/databases/{self.database_id}/query", payload)
                pages.extend(result.get("results", []))
                if result.get("has_more"):
                    cursor = result.get("next_cursor")
                else:
                    break
            except Exception as e:
                logger.error(f"DB クエリエラー: {e}")
                break
        return pages

    def get_property(self, page: dict, name: str) -> Optional[str]:
        try:
            prop = page["properties"].get(name, {})
            t = prop.get("type")
            if t == "title":
                arr = prop.get("title", [])
                return arr[0]["plain_text"] if arr else None
            if t == "url":
                return prop.get("url")
            if t == "rich_text":
                arr = prop.get("rich_text", [])
                return arr[0]["plain_text"] if arr else None
        except Exception:
            pass
        return None

    def update_paper(self, page_id: str, properties: dict) -> bool:
        try:
            self._patch(f"/pages/{page_id}", {"properties": properties})
            return True
        except Exception as e:
            logger.error(f"  Notion 更新エラー ({page_id[:8]}...): {e}")
            return False

# ──────────────────────────────────────────────
# メタデータ取得
# ──────────────────────────────────────────────

def _extract_doi(raw: str) -> Optional[str]:
    """文字列から DOI を抽出して返す。なければ None。"""
    if re.match(r"^10\.\d{4,}/\S+", raw):
        return raw
    m = re.search(r"doi\.org/(10\.\d{4,}/\S+)", raw)
    if m:
        return m.group(1).rstrip(")")  # URL末尾の括弧を除去
    return None


def fetch_crossref(doi: str) -> dict:
    """CrossRef API からメタデータを取得"""
    logger.info(f"  CrossRef API: {doi}")
    resp = requests.get(
        f"https://api.crossref.org/works/{doi}",
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    msg = resp.json()["message"]

    # Abstract: JATS XML タグを除去
    abstract = msg.get("abstract", "")
    if abstract:
        abstract = re.sub(r"<[^>]+>", "", abstract).strip()

    # Authors: "Family Given" 形式でカンマ結合
    authors_list = msg.get("author", [])
    authors = ", ".join(
        f"{a.get('family', '')} {a.get('given', '')}".strip()
        for a in authors_list
        if a.get("family") or a.get("given")
    )

    # 発行年
    year = None
    dp = msg.get("published", {}).get("date-parts", [[]])
    if dp and dp[0]:
        year = int(dp[0][0])

    return {
        "title":    (msg.get("title") or [""])[0].strip(),
        "authors":  authors,
        "journal":  (msg.get("container-title") or [""])[0].strip(),
        "year":     year,
        "abstract": abstract or None,
        "url":      f"https://doi.org/{doi}",
    }


def fetch_pubmed(url: str) -> dict:
    """PubMed URL から PMID を抽出して E-utilities API でメタデータを取得"""
    m = re.search(r"/(\d+)/?", url)
    if not m:
        raise ValueError(f"PMID を抽出できませんでした: {url}")
    pmid = m.group(1)
    logger.info(f"  PubMed API: PMID={pmid}")

    resp = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract"},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    title   = root.findtext(".//ArticleTitle") or ""
    journal = (
        root.findtext(".//Journal/Title")
        or root.findtext(".//ISOAbbreviation")
        or ""
    )
    year_str = root.findtext(".//PubDate/Year") or ""
    year = int(year_str) if year_str.isdigit() else None

    authors = ", ".join(
        f"{a.findtext('LastName', '')} {a.findtext('ForeName', '')}".strip()
        for a in root.findall(".//Author")
        if a.findtext("LastName")
    )

    abstract = " ".join(
        (el.text or "") for el in root.findall(".//AbstractText")
    ).strip() or None

    return {
        "title": title, "authors": authors,
        "journal": journal, "year": year,
        "abstract": abstract,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    }


def fetch_arxiv(url: str) -> dict:
    """arXiv URL から arXiv ID を抽出して API でメタデータを取得"""
    m = re.search(r"arxiv\.org/abs/([0-9]+\.[0-9]+v?[0-9]*)", url)
    if not m:
        raise ValueError(f"arXiv ID を抽出できませんでした: {url}")
    arxiv_id = m.group(1)
    logger.info(f"  arXiv API: {arxiv_id}")

    resp = requests.get(
        "https://export.arxiv.org/api/query",
        params={"id_list": arxiv_id},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    NS = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(resp.text)
    entry = root.find("atom:entry", NS)
    if entry is None:
        raise ValueError(f"arXiv エントリが見つかりません: {arxiv_id}")

    title = re.sub(r"\s+", " ", entry.findtext("atom:title", "", NS)).strip()
    authors = ", ".join(
        el.text.strip()
        for el in entry.findall("atom:author/atom:name", NS)
        if el.text
    )
    abstract = re.sub(
        r"\s+", " ", entry.findtext("atom:summary", "", NS)
    ).strip() or None
    published = entry.findtext("atom:published", "", NS)
    year = int(published[:4]) if published else None

    return {
        "title": title, "authors": authors,
        "journal": "arXiv", "year": year,
        "abstract": abstract,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
    }


def fetch_from_url(url: str) -> dict:
    """一般URLをスクレイピングしてメタデータを取得"""
    logger.info(f"  スクレイピング: {url}")
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    soup = BeautifulSoup(resp.text, "lxml")

    # タイトル
    title_el = soup.find("h1") or soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""

    # アブストラクト: よくある class 名を探す
    abstract = ""
    for selector in [
        ("div",     {"class": re.compile(r"abstract", re.I)}),
        ("section", {"class": re.compile(r"abstract", re.I)}),
        ("p",       {"class": re.compile(r"abstract", re.I)}),
    ]:
        el = soup.find(*selector)
        if el:
            abstract = el.get_text(separator=" ", strip=True)
            break

    # og:description フォールバック
    if len(abstract) < 50:
        og = (
            soup.find("meta", property="og:description")
            or soup.find("meta", attrs={"name": "description"})
        )
        if og:
            abstract = og.get("content", "").strip()

    # 著者: meta author タグ
    author_meta = soup.find("meta", attrs={"name": re.compile(r"author", re.I)})
    authors = author_meta.get("content", "").strip() if author_meta else ""

    return {
        "title":    title,
        "authors":  authors,
        "journal":  "",
        "year":     None,
        "abstract": abstract or None,
        "url":      url,
    }


def fetch_metadata(doi_or_url: str) -> dict:
    """DOI/URL の種別を判定してメタデータを取得するディスパッチャ"""
    raw = doi_or_url.strip()

    # 1. DOI 判定（"10." で始まる or doi.org を含む）
    doi = _extract_doi(raw)
    if doi:
        meta = fetch_crossref(doi)
        # CrossRef にアブストラクトがなければURLをスクレイピング
        if not meta.get("abstract"):
            logger.info("  CrossRef にアブストラクトなし → URLスクレイピングを試行")
            try:
                scraped = fetch_from_url(meta["url"])
                if scraped.get("abstract"):
                    meta["abstract"] = scraped["abstract"]
            except Exception as e:
                logger.warning(f"  スクレイピングフォールバック失敗: {e}")
        return meta

    # 2. PubMed URL 判定
    if "pubmed.ncbi.nlm.nih.gov" in raw:
        time.sleep(0.5)  # PubMed レート制限対策
        return fetch_pubmed(raw)

    # 3. arXiv URL 判定
    if "arxiv.org/abs/" in raw:
        time.sleep(3)  # arXiv 推奨インターバル
        return fetch_arxiv(raw)

    # 4. その他URL → スクレイピング
    return fetch_from_url(raw)


# ──────────────────────────────────────────────
# Gemini AI 処理
# ──────────────────────────────────────────────
def generate_ai_fields(meta: dict) -> Optional[dict]:
    """メタデータを Gemini に渡して AI フィールドを生成"""
    prompt = PROMPT_STUDY_ANALYSIS.format(
        title    = meta.get("title")    or "不明",
        authors  = meta.get("authors")  or "不明",
        journal  = meta.get("journal")  or "不明",
        year     = meta.get("year")     or "不明",
        abstract = meta.get("abstract") or "アブストラクトなし（タイトルから推測してください）",
    )

    try:
        resp = _gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[prompt],
            config=genai_types.GenerateContentConfig(
                temperature=0.3,
                response_mime_type="application/json",
            ),
        )
        raw = resp.text.strip()
        # コードブロック除去
        raw = re.sub(r"^```json\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        ai = json.loads(raw)

        # 型ガード
        if isinstance(ai.get("keywords"), str):
            ai["keywords"] = [k.strip() for k in ai["keywords"].split(",")]
        ai["relevance"] = max(1, min(5, int(ai.get("relevance", 3))))

        return ai

    except Exception as e:
        logger.error(f"  Gemini 生成エラー: {e}")
        return None


# ──────────────────────────────────────────────
# Notion プロパティペイロード構築
# ──────────────────────────────────────────────
def _rt(text: str) -> dict:
    """rich_text プロパティを生成（2000文字制限）"""
    return {"rich_text": [{"text": {"content": text[:2000]}}]}


def build_notion_payload(meta: dict, ai: Optional[dict]) -> dict:
    """メタデータと AI 結果を Notion の properties dict に変換"""
    props: dict = {}

    # タイトル（英語の場合は日本語訳で上書き）
    title_str = meta.get("title", "")
    if ai and ai.get("title_ja") and not re.search(r"[ぁ-んァ-ン一-龯]", title_str):
        title_str = ai["title_ja"]
    if title_str:
        props["Title"] = {"title": [{"text": {"content": title_str[:2000]}}]}

    if meta.get("authors"):
        props["Authors"] = _rt(meta["authors"])
    else:
        # Authors が空だと次回も再処理対象になるため、取得失敗を記録
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        props["Authors"] = _rt(f"[取得失敗] {today}")

    if meta.get("journal"):
        props["Journal"] = {"select": {"name": meta["journal"][:100]}}

    if meta.get("year"):
        props["Year"] = {"number": meta["year"]}

    if meta.get("abstract"):
        abstract = meta["abstract"]
        if len(abstract) > 2000:
            logger.warning(f"  Abstract が 2000 文字超のため先頭 2000 文字を保存")
        props["Abstract"] = _rt(abstract)

    # Date Added を今日の日付で設定
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    props["Date Added"] = {"date": {"start": today_str}}

    # Reading Status が未設定の場合「未読」にセット
    # （既存値を上書きしないよう、ここでは常に「未読」で初期化）
    props["Reading Status"] = {"status": {"name": "未読"}}

    # AI フィールド
    if ai:
        if ai.get("abstract_ja"):
            props["Abstract (JA)"] = _rt(ai["abstract_ja"])
        if ai.get("summary_ja"):
            props["AI Summary (JA)"] = _rt(ai["summary_ja"])
        if ai.get("keywords"):
            props["Keywords"] = {
                "multi_select": [
                    {"name": kw[:100]} for kw in ai["keywords"][:10]
                ]
            }
        if ai.get("relevance"):
            props["Relevance"] = {"number": ai["relevance"]}

    return props


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def process_one(notion: NotionStudyAPI, page: dict) -> bool:
    """1 エントリを処理して Notion を更新。成功なら True を返す。"""
    page_id = page["id"]
    title   = notion.get_property(page, "Title") or "タイトルなし"
    doi_url = notion.get_property(page, "DOI / URL")

    logger.info(f"\n処理中: {title[:60]}")
    logger.info(f"  DOI / URL: {doi_url}")

    if not doi_url:
        logger.warning("  DOI / URL が空のためスキップ")
        return False

    # メタデータ取得
    try:
        meta = fetch_metadata(doi_url)
        logger.info(f"  メタデータ取得完了: {meta.get('title', '')[:60]}")
    except Exception as e:
        logger.error(f"  メタデータ取得失敗: {e}")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        notion.update_paper(page_id, {"Authors": _rt(f"[取得失敗] {today}")})
        return False

    # Gemini 処理
    logger.info("  Gemini で AI フィールドを生成中...")
    ai = generate_ai_fields(meta)
    if ai:
        logger.info(
            f"  AI 生成完了: relevance={ai.get('relevance')} "
            f"keywords={ai.get('keywords', [])[:3]}"
        )
    else:
        logger.warning("  AI 生成失敗 - メタデータのみ保存します")

    time.sleep(2)

    # Notion 更新
    payload = build_notion_payload(meta, ai)
    success = notion.update_paper(page_id, payload)

    if success:
        logger.info(f"  ✅ 更新完了: {title[:50]}")
    else:
        logger.error(f"  ❌ 更新失敗: {title[:50]}")

    return success


def main() -> None:
    logger.info("=" * 60)
    logger.info("  StudyArticleDB 論文メタデータ自動処理")
    logger.info("=" * 60)

    notion = NotionStudyAPI(NOTION_API_KEY, STUDY_DATABASE_ID)

    pages = notion.query_pending()
    logger.info(f"処理対象: {len(pages)} 件")

    if not pages:
        logger.info("処理待ちエントリなし。終了します。")
        return

    success_count = 0
    for page in pages:
        if process_one(notion, page):
            success_count += 1
        time.sleep(1)

    logger.info("\n" + "=" * 60)
    logger.info(f"  完了: {success_count} / {len(pages)} 件成功")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
