#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notion → crosshealthjp 公式サイト 自動公開（2026-07-06 オペ改訂）

WordPress版（notion_wordpress_uploader.py）の置き換え。tekutekuradio への投稿は停止し、
記事は公式サイト https://www.crosshealthjp.org/articles/ に直接公開する。

フロー:
1. Status(Web) = 「投稿待ち」のページを検出（従来と同じ）
2. Article(Web) リンク先の Notion ページ本文を取得 → Markdown → HTML（従来のコンバータを再利用）
3. crosshealthjp の src/articles-data/<pid>.json を生成（SITE_ARTICLES_DIR 配下）
   → 呼び出し元の GitHub Actions が crosshealthjp へ commit & push → Cloudflare Pages が自動デプロイ
4. 成功時: URL(Web)=サイトURL / PodcastDescription更新 / Status(Web)=完了 / Date(Web)記録（従来と同じ）

必要な環境変数:
  NOTION_API_KEY, NOTION_DATABASE_ID（従来どおり）
  SITE_ARTICLES_DIR = チェックアウト済み crosshealthjp/src/articles-data のパス（必須）
  SITE_BASE_URL     = 省略時 https://www.crosshealthjp.org
"""
import glob
import html as html_lib
import json
import os
import re
import sys
from datetime import datetime

# notion_wordpress_uploader はモジュール読込時に WordPress 設定が無いと exit(1) するため、
# 使わないWP変数にダミーを入れてから import する（WordPress API は一切呼ばない）。
os.environ.setdefault('WORDPRESS_URL', 'https://unused.invalid')
os.environ.setdefault('WORDPRESS_USERNAME', 'unused')
os.environ.setdefault('WORDPRESS_APP_PASSWORD', 'unused')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from notion_wordpress_uploader import (  # noqa: E402
    NotionWordPressUploader,
    logger,
    _JST,
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD_WP,
    NOTIFY_TO_WP,
)

SITE_BASE_URL = os.environ.get('SITE_BASE_URL', 'https://www.crosshealthjp.org').rstrip('/')
SITE_ARTICLES_DIR = os.environ.get('SITE_ARTICLES_DIR', '')


def _to_text(h: str) -> str:
    """HTML→プレーンテキスト（summary用）"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_lib.unescape(h))).strip()


def send_site_notification(uploaded: list) -> None:
    """サイト公開完了の通知メール"""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD_WP:
        logger.warning("Gmail未設定のため完了通知メールをスキップします")
        return
    lines = [f"  [{i+1}] {a['title']}\n      {a['url']}" for i, a in enumerate(uploaded)]
    body = (
        "【医療政策ウォッチャー】公式サイト公開のお知らせ\n\n"
        f"以下の記事が https://www.crosshealthjp.org に自動公開されました（数分でデプロイ反映）。\n\n"
        f"■ 公開記事（{len(uploaded)}件）\n" + "\n".join(lines) + "\n"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"【完了】サイト自動公開 {len(uploaded)}件"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = NOTIFY_TO_WP
    msg.attach(MIMEText(body, "plain", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD_WP)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_TO_WP, msg.as_string())
        logger.info(f"サイト公開通知メール送信: {NOTIFY_TO_WP}")
    except Exception as e:
        logger.error(f"メール送信失敗: {e}")


class SiteUploader(NotionWordPressUploader):
    """WordPress の代わりに crosshealthjp の articles-data JSON を生成する"""

    def _next_pid(self) -> str:
        """pid = YYYYMMDDNN（日付+連番2桁）。既存ファイルと衝突しない番号を返す"""
        today = datetime.now(_JST).strftime('%Y%m%d')
        seq = 1
        while os.path.exists(os.path.join(SITE_ARTICLES_DIR, f"{today}{seq:02d}.json")):
            seq += 1
        return f"{today}{seq:02d}"

    def _get_source_url(self, page: dict) -> str:
        """URL(Source) プロパティ（情報源URL）を取得"""
        try:
            prop = page.get('properties', {}).get('URL(Source)', {})
            if prop.get('type') == 'url':
                return prop.get('url') or ''
            val = self.get_property_value(page, 'URL(Source)')
            return val or ''
        except Exception:
            return ''

    def process(self) -> int:
        if not SITE_ARTICLES_DIR or not os.path.isdir(SITE_ARTICLES_DIR):
            logger.error(
                f"SITE_ARTICLES_DIR が未設定か存在しません: {SITE_ARTICLES_DIR!r}\n"
                "GitHub Actions で crosshealthjp をチェックアウトし、"
                "src/articles-data のパスを環境変数で渡してください。"
            )
            return 0

        logger.info("Status(Web) が「投稿待ち」のページを検索中...")
        pages = self.query_database({
            "property": "Status(Web)",
            "status": {"equals": "投稿待ち"}
        })
        logger.info(f"{len(pages)} 件のページが見つかりました")
        if not pages:
            logger.info("処理対象なし。終了します。")
            return 0

        success_count = 0
        uploaded: list = []

        for page in pages:
            page_id = page.get('id')
            logger.info("\n" + "=" * 55)
            db_title = (
                self.get_property_value(page, 'Title(Web)')
                or self.get_property_value(page, 'Title')
                or 'タイトルなし'
            )
            logger.info(f"処理中 (DB): {db_title[:60]}")

            article_page_id = self.get_article_linked_page_id(page)
            if not article_page_id:
                logger.error("  ❌ スキップ: Article(Web) にNotionページへのリンクがありません")
                continue

            title = self.fetch_page_title(article_page_id) or db_title
            blocks = self.fetch_page_blocks(article_page_id)
            if not blocks:
                logger.error("  ❌ スキップ: 記事ページが空、またはブロック取得失敗")
                continue

            blocks = self._truncate_at_factcheck(blocks)
            md_content = self.converter.convert(blocks)
            html_content = self._markdown_to_html(md_content)
            if not html_content.strip():
                logger.error("  ❌ スキップ: HTML変換後のコンテンツが空です")
                continue

            # ── articles-data JSON 生成（サイトの Article 型に合わせる）
            pid = self._next_pid()
            today = datetime.now(_JST).strftime('%Y-%m-%d')
            rec = {
                "pid": pid,
                "title": title,
                "date": today,
                "summary": _to_text(html_content)[:140],
                "source": self._get_source_url(page),
                "tags": [],
                "origUrl": "",
                "html": html_content,
            }
            out_path = os.path.join(SITE_ARTICLES_DIR, f"{pid}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            site_url = f"{SITE_BASE_URL}/articles/{pid}/"
            logger.info(f"  ✅ 記事JSON生成: {os.path.basename(out_path)} → {site_url}")

            # ── Notion 書き戻し（従来と同じ流儀）
            if self.update_notion_url_web(page_id, site_url):
                logger.info(f"  ✅ Notion URL(Web) 更新: {site_url}")
            if self.update_podcast_description(page_id, site_url):
                logger.info("  ✅ Notion PodcastDescription 更新完了")
            if self.update_notion_status(page_id, "完了"):
                logger.info("  ✅ Notion ステータス更新: 投稿待ち → 完了")

            import requests as _rq
            try:
                resp = _rq.patch(
                    f"{self.notion_base}/pages/{page_id}",
                    headers=self.notion_headers,
                    json={"properties": {"Date(Web)": {"date": {"start": today}}}},
                    timeout=30,
                )
                resp.raise_for_status()
                logger.info(f"  ✅ Date(Web) 記録: {today}")
            except Exception as e:
                logger.warning(f"  ⚠️  Date(Web) 記録失敗: {e}")

            success_count += 1
            uploaded.append({"title": title, "url": site_url})

        if uploaded:
            send_site_notification(uploaded)
        return success_count


def main():
    logger.info("=" * 55)
    logger.info("Notion → 公式サイト(crosshealthjp) 自動公開を開始します")
    logger.info("=" * 55)
    try:
        count = SiteUploader().process()
        logger.info("\n" + "=" * 55)
        logger.info(f"処理完了 / サイト公開: {count} 件")
        logger.info("=" * 55)
    except Exception as e:
        logger.error(f"予期せぬエラー: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
