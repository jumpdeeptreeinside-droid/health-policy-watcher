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
4. 🔴 **この中で commit & push する**。push が通ってから Notion に書く（2026-10-04）
   旧実装は「JSONを書く→Notionを完了にする」を先にやり、commit & push を
   GitHub Actions の別の段に任せていた。2026-09-23 15:08 の実行で push 段が落ち、
   **Notionは完了・URL(Web)も入っているのに記事はrepoに無い**状態の行が3つ残った。
   次の実行は「完了」を見て作り直さないので、恒久的に穴になった。
   さらに番号(pid)は既存ファイルの有無で決めるため、消えた01/02は後の記事に再利用され
   **配信済みの説明欄が別の記事を指す**状態になった（404にならないので気づけない）。
5. 成功時: URL(Web)=サイトURL / PodcastDescription更新 / Status(Web)=完了 / Date(Web)記録

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
import subprocess
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


def send_site_notification(uploaded: list, title_warnings: list = None) -> None:
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
    # 題に英単語が残った分は必ず知らせる（2026-10-04・直すのは人。公開は止めていない）
    if title_warnings:
        body += ("\n■ ⚠️ 題の確認が必要（" + str(len(title_warnings)) + "件）\n"
                 + "\n".join(f"  [{w['pid']}] {w['why']}\n      {w['title']}"
                              for w in title_warnings) + "\n")
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
        logger.info("サイト公開通知メール送信: 1件")
    except Exception as e:
        logger.error(f"メール送信失敗: {e}")


class SiteUploader(NotionWordPressUploader):
    """WordPress の代わりに crosshealthjp の articles-data JSON を生成する"""

    @staticmethod
    def _git(args: list, cwd: str) -> tuple:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def _commit_and_push(self, repo: str, n: int) -> bool:
        """🔴 push が通って初めて True。通らなければ Notion を一切触らない。"""
        rc, out = self._git(["status", "--porcelain", "src/articles-data"], repo)
        if rc != 0:
            logger.error(f"  ❌ git status が失敗: {out[:200]}"); return False
        if not out.strip():
            logger.error("  ❌ 書いたはずの記事が git から見えません（置き場所が違う）"); return False
        for args in (["config", "user.name", "CrossHealth Watcher Bot"],
                     ["config", "user.email", "info@crosshealthjp.org"],
                     ["add", "src/articles-data"],
                     ["commit", "-m", f"watcher: 記事の自動公開（{n}本）"]):
            rc, out = self._git(args, repo)
            if rc != 0:
                logger.error(f"  ❌ git {args[0]} が失敗: {out[:300]}"); return False
        # 🔴 他のセッション/自動化が先に push していると素の push は落ちる（実際に落ちた）
        rc, out = self._git(["pull", "--rebase", "--autostash"], repo)
        if rc != 0:
            logger.error(f"  ❌ git pull --rebase が失敗: {out[:300]}"); return False
        rc, out = self._git(["push"], repo)
        if rc != 0:
            logger.error(f"  ❌ git push が失敗: {out[:300]}")
            logger.error("     → Notion は「投稿待ち」のままにします。次の実行でやり直されます。")
            return False
        logger.info(f"  ✅ crosshealthjp へ push（{n}本）。Cloudflare Pages が数分でデプロイします")
        return True

    # 日本語の題に英単語の前置詞が残る生成事故（2026-10-04 -06の指摘・実例
    # 「令和8年度診療報酬改定と指導・監査 of 最新動向」）。公開は止めない（記事が無いほうが害が大きい）が
    # 必ずログと通知に出す。
    _EN_PREP = re.compile(r"(?<![A-Za-z])(of|in|on|for|with|to|from|by|at|and|the|as|through)"
                          r"(?![A-Za-z])", re.I)
    _JP_CH = r"\u3040-\u30ff\u4e00-\u9fff\uff01-\uff60"

    # 🔴 文字化けの検出（2026-10-04 -06の指摘）。実例＝厚労省のPDF名が
    #    「(з—…йҷўгҒ®иҖҗйңҮ…pdf, Page 1)」。**Notionの原文の時点で化けている**ので
    #    変換の encoding を直しても消えない（908本中1本だけ・キリル文字で見分けられる）。
    #    日本語の記事にキリル文字が出ることは無いので、出たら化けと断じてよい。
    _MOJI = re.compile(r"[\u0400-\u04ff\u0500-\u052f]")

    @classmethod
    def _content_warnings(cls, title: str, html: str) -> list:
        out = list(cls._title_warnings(title))
        n = len(cls._MOJI.findall(html or ""))
        if n:
            out.append(f"本文に文字化けの疑い（キリル文字が{n}個）。原文のPDF名が化けている例があります")
        return out

    @classmethod
    def _title_warnings(cls, title: str) -> list:
        """🔴 判定は『日本語の字数』ではなく『前置詞が日本語の字に隣接しているか』。
        字数で切ると「WHOがin日本で」のような短い題を取りこぼす（2026-10-04 の試験で発覚）。
        英語だけの題（海外ヘッドラインの原題）は隣に日本語が来ないので当たらない。"""
        hits = []
        for m in cls._EN_PREP.finditer(title):
            left = title[:m.start()].rstrip()
            right = title[m.end():].lstrip()
            touches_jp = bool(re.search(f"[{cls._JP_CH}]$", left)) or \
                         bool(re.match(f"[{cls._JP_CH}]", right))
            if touches_jp:
                hits.append(m.group(0).lower())
        if not hits:
            return []
        return [f"日本語の題に英単語が残っています: {sorted(set(hits))}"]

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
        pending: list = []          # push が通ってから Notion に書く分
        title_warnings: list = []   # 題に英単語が残った分（公開は止めない）

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
            # tekutekuradio時代のWordPressショートコード残骸([temp id=N]等)を除去（2026-07-07 木内さん指摘）
            md_content = re.sub(r"\[temp[^\]]*\]", "", md_content)
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
            for w in self._content_warnings(title, html_content):
                logger.warning(f"  ⚠️  {w}")
                title_warnings.append({"pid": pid, "title": title, "why": w})

            # 🔴 ここでは Notion を触らない。push が通ってから下でまとめて書く（2026-10-04）
            pending.append({"page_id": page_id, "pid": pid, "path": out_path,
                            "site_url": site_url, "title": title, "today": today})
            continue


        # ───────── push が通ってから Notion に書く（2026-10-04 の作り直し） ─────────
        if not pending:
            logger.info("書き出した記事がありません。Notion は触りません。")
            return 0
        repo = os.path.abspath(os.path.join(SITE_ARTICLES_DIR, "..", ".."))
        if not self._commit_and_push(repo, len(pending)):
            # 🔴 push できなかったら書いたファイルを消す。
            #    残すと次の実行の _next_pid が番号を進め、消えた番号が別記事に再利用される
            #    （2026-09-23 に起きたのがこれ）。
            for it in pending:
                try:
                    os.remove(it["path"])
                    logger.info(f"  片付け: {os.path.basename(it['path'])} を消しました")
                except OSError as e:
                    logger.warning(f"  片付けに失敗: {e}")
            logger.error(f"❌ push できなかったので {len(pending)} 本を公開しませんでした"
                         "（Notion は「投稿待ち」のまま＝次の実行でやり直します）")
            return 0

        for it in pending:
            logger.info("-" * 40)
            logger.info(f"Notion に書き戻し: {it['title'][:50]}")
            if self._write_back(it):
                success_count += 1
                uploaded.append({"title": it["title"], "url": it["site_url"]})

        if uploaded:
            send_site_notification(uploaded, title_warnings)
        return success_count

    def _write_back(self, it: dict) -> bool:
        """push 済みの1本について Notion を更新する。"""
        page_id, site_url = it["page_id"], it["site_url"]
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
                json={"properties": {"Date(Web)": {"date": {"start": it["today"]}}}},
                timeout=30,
            )
            resp.raise_for_status()
            logger.info(f"  ✅ Date(Web) 記録: {it['today']}")
        except Exception as e:
            logger.warning(f"  ⚠️  Date(Web) 記録失敗: {e}")
        return True



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
