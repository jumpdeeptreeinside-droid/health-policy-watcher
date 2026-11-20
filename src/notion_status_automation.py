#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Notionデータベースのステータス自動更新スクリプト

機能:
  Status(コンテンツ作成)が「完了」の場合:
   - Status(Web)を「-」（デフォルト）→「投稿待ち」に自動変更
   - Status(Podcast)を「-」（デフォルト）→「音声化待ち」に自動変更

🔴 2026-11-20 改訂（イロハ）
  ・呼び手が2つになった：GitHub Actions（notion_automation.yml・毎時の予定だが実測は中央値7時間おき）と、
    Mac の launchd（mac_audio_pipeline.py の冒頭・毎時15分・欠けずに動いている）。
    二重に走っても壊れないように：
      - 進めるのは「-」の欄だけ（plan_advance）。進めた後は計画が空になる＝同じ記事を2回進めない
      - 書く直前にそのページを取り直して計画を作り直す（もう一方が先に進めていたら書かない）。
        これが無いと、検索の結果が古いまま「音声化待ち」を書き、音声側が先に進めた「試聴待ち」を
        巻き戻しうる
  ・日付の自動記録（Date(Select)/Date(W-complete)/Date(P-complete)）は外した。
    3欄とも DB に無く、毎回 400 を出していたのに run は success だった。
    公開日は site_uploader が Date(Web) に書いている（読む側は daily_report が Date(Web) を見る）。
  ・Notion の 400 は黙って0件にしない：検索の失敗は例外、書き込みの失敗は件数に数え、
    main は1件でも失敗があれば exit 1（＝Actions の失敗通知が飛ぶ）。
  ・network は必ず timeout。
  ・「音声化待ち記事のお知らせ」メールは廃止（2026-11-20、試聴依頼メールが代わり）。

実行方法:
    python notion_status_automation.py             # 進める（GitHub Actions はこれ）
    python notion_status_automation.py --dry-run   # Notion に書かずに「進めるはずの件数」を数える
    python notion_status_automation.py --self-test # 偽の行で判定を確かめる（network なし）
"""

import sys
import io
import os
import logging
from typing import Callable, List, Dict, Optional

import requests

# Windows環境での文字エンコーディング問題を解決
if sys.platform == 'win32' and __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 🔴 basicConfig は main() でだけ呼ぶ。import された側（mac_audio_pipeline）のログの行き先を変えない。
logger = logging.getLogger("notion_status_automation")

HTTP_TIMEOUT = 30      # 秒。無人ジョブは「落ちる」より「固まる」が質が悪い

WEB_PROP = "Status(Web)"
PODCAST_PROP = "Status(Podcast)"
CONTENT_PROP = "Status(コンテンツ作成)"
DEFAULT = "-"
WEB_NEXT = "投稿待ち"
PODCAST_NEXT = "音声化待ち"


def _load_settings():
    """環境変数優先、なければ config.py。どちらも無ければ (None, None)。"""
    key = os.environ.get('NOTION_API_KEY')
    db = os.environ.get('NOTION_DATABASE_ID')
    if key and db:
        return key, db, "環境変数"
    try:
        import config
        return config.NOTION_API_KEY, config.NOTION_DATABASE_ID, "config.py"
    except (ImportError, AttributeError):
        return None, None, "なし"


# 🔴 2026-11-20 廃止：「音声化待ち記事のお知らせ」メール（send_podcast_notification・宛先 NOTIFY_EMAIL_STATUS）。
#    手作業で収録していた時代の文面で、いまは同じ記事が mac_audio_pipeline で音声化され
#    【試聴依頼】メールが出る＝それが代わり（翔太さん決定 11/20「a」）。
#    呼び手が2つ（GitHub / Mac）になり「どちらが先に進めたかで送ったり送らなかったり」になるため、
#    片方だけ送る形も残さずコードごと消した。戻すときは git の 3752be6 以前を見る。


def _status_of(page: Dict, prop_name: str) -> Optional[str]:
    prop = (page.get('properties') or {}).get(prop_name) or {}
    if prop.get('type') != 'status':
        return None
    st = prop.get('status')
    return st.get('name') if st else None


def _title_of(page: Dict) -> str:
    prop = (page.get('properties') or {}).get('Title') or {}
    arr = prop.get('title') or []
    return (arr[0].get('plain_text') if arr else None) or 'タイトルなし'


def plan_advance(page: Dict) -> Dict:
    """1行について「何を書くか」を返す（network なし・純粋関数）。

    進めるのは Status(コンテンツ作成)=完了 の行の、「-」（または空）の欄だけ。
    すでに進んでいる欄には触らない＝2回呼んでも2回目は空になる（冪等）。
    """
    if _status_of(page, CONTENT_PROP) != "完了":
        return {}
    props = {}
    if _status_of(page, WEB_PROP) in (None, DEFAULT):
        props[WEB_PROP] = {"status": {"name": WEB_NEXT}}
    if _status_of(page, PODCAST_PROP) in (None, DEFAULT):
        props[PODCAST_PROP] = {"status": {"name": PODCAST_NEXT}}
    return props


class NotionError(RuntimeError):
    """Notion が 2xx 以外を返した（黙って0件にしない）"""


class NotionAutomation:
    """Notionデータベースのステータス自動更新を行うクラス"""

    def __init__(self, api_key: Optional[str] = None, database_id: Optional[str] = None):
        if api_key is None or database_id is None:
            k, d, src = _load_settings()
            api_key = api_key or k
            database_id = database_id or d
            logger.info(f"Notion設定の読み込み元: {src}")
        if not api_key or not database_id:
            raise NotionError("NOTION_API_KEY / NOTION_DATABASE_ID が環境変数にも config.py にもありません")
        self.api_key = api_key
        self.database_id = database_id
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        self.base_url = "https://api.notion.com/v1"

    @staticmethod
    def _raise_for(resp, what: str) -> None:
        if resp.status_code >= 300:
            try:
                msg = resp.json().get("message", "")
            except Exception:
                msg = resp.text[:200]
            raise NotionError(f"{what}: HTTP {resp.status_code} {msg}")

    def query_database(self, filter_conditions: Optional[Dict] = None) -> List[Dict]:
        """検索（ページ送り対応）。🔴 失敗は例外＝400 を0件に見せない。"""
        url = f"{self.base_url}/databases/{self.database_id}/query"
        payload: Dict = {"page_size": 100}
        if filter_conditions:
            payload["filter"] = filter_conditions
        out: List[Dict] = []
        while True:
            resp = requests.post(url, headers=self.headers, json=payload, timeout=HTTP_TIMEOUT)
            self._raise_for(resp, "データベース検索")
            data = resp.json()
            out.extend(data.get('results', []))
            if not data.get('has_more'):
                return out
            payload["start_cursor"] = data["next_cursor"]

    def get_page(self, page_id: str) -> Dict:
        resp = requests.get(f"{self.base_url}/pages/{page_id}", headers=self.headers,
                            timeout=HTTP_TIMEOUT)
        self._raise_for(resp, "ページ取得")
        return resp.json()

    def update_page_properties(self, page_id: str, properties: Dict) -> bool:
        """ページのプロパティを更新。失敗は False（理由は ERROR で出す）。"""
        try:
            resp = requests.patch(f"{self.base_url}/pages/{page_id}", headers=self.headers,
                                  json={"properties": properties}, timeout=HTTP_TIMEOUT)
            self._raise_for(resp, "ページ更新")
            return True
        except Exception as e:
            logger.error(f"ページ更新エラー: {e}")
            return False

    def find_candidates(self) -> List[Dict]:
        """Status(コンテンツ作成)=完了 かつ Web か Podcast が「-」の行"""
        return self.query_database({
            "and": [
                {"property": CONTENT_PROP, "status": {"equals": "完了"}},
                {"or": [
                    {"property": WEB_PROP, "status": {"equals": DEFAULT}},
                    {"property": PODCAST_PROP, "status": {"equals": DEFAULT}},
                ]},
            ]
        })

    def process_content_complete_status(self, dry_run: bool = False,
                                        pages: Optional[List[Dict]] = None,
                                        fetch: Optional[Callable[[str], Dict]] = None,
                                        write: Optional[Callable[[str, Dict], bool]] = None) -> Dict:
        """完了の行の「-」を進める。

        dry_run=True … Notion に書かない。「進めるはずの件数」だけ数える。
        （メールは送らない＝お知らせメールは 2026-11-20 廃止）
        pages/fetch/write … 陽性対照（--self-test）用の差し替え口。通常は None。

        戻り値: {"found", "planned", "advanced", "stale", "failed", "podcast"}
        """
        if pages is None:
            pages = self.find_candidates()
        fetch = fetch or self.get_page
        write = write or self.update_page_properties
        r = {"found": len(pages), "planned": 0, "advanced": 0, "stale": 0, "failed": 0,
             "podcast": 0}
        logger.info(f"完了かつ未振り分けの候補: {len(pages)} 件")

        for page in pages:
            page_id = page.get('id')
            title = _title_of(page)
            plan = plan_advance(page)
            if not plan:
                logger.info(f"  ⏭ 更新不要: {title[:40]}")
                continue
            r["planned"] += 1
            names = "・".join(f"{k}→{v['status']['name']}" for k, v in plan.items())
            if dry_run:
                logger.info(f"  [dry-run] 進めるはず: {title[:40]}（{names}）")
                if PODCAST_PROP in plan:
                    r["podcast"] += 1
                continue

            # 🔴 書く直前に取り直す（もう一方の呼び手が先に進めていたら書かない）
            try:
                fresh = fetch(page_id)
            except Exception as e:
                logger.error(f"  ❌ 取り直し失敗（書かない）: {title[:40]}: {e}")
                r["failed"] += 1
                continue
            plan = plan_advance(fresh)
            if not plan:
                logger.info(f"  ⏭ もう進んでいた（別の実行が先）: {title[:40]}")
                r["stale"] += 1
                continue
            names = "・".join(f"{k}→{v['status']['name']}" for k, v in plan.items())
            if write(page_id, plan):
                logger.info(f"  ✅ 進めた: {title[:40]}（{names}）")
                r["advanced"] += 1
                if PODCAST_PROP in plan:
                    r["podcast"] += 1
            else:
                logger.error(f"  ❌ 更新失敗: {title[:40]}（{names}）")
                r["failed"] += 1

        return r


def _fake_page(pid: str, content: Optional[str], web: Optional[str], podcast: Optional[str],
               title: str = "テスト行") -> Dict:
    def st(v):
        return {"type": "status", "status": ({"name": v} if v is not None else None)}
    return {"id": pid, "properties": {
        "Title": {"type": "title", "title": [{"plain_text": title}]},
        CONTENT_PROP: st(content), WEB_PROP: st(web), PODCAST_PROP: st(podcast)}}


def self_test() -> int:
    """陽性対照：偽の行で判定と「二重に走っても進めない」を確かめる（network なし）。"""
    ok = fail = 0

    def check(name, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
            print(f"  ok  {name}")
        else:
            fail += 1
            print(f"  NG  {name}: got={got!r} want={want!r}")

    # 1) 判定（昨日の形＝完了／-／- を含む）
    # 🔴 キーだけでなく書く値まで固定する（ミハル 11/20：WEB_NEXT を「公開待ち」に変えても
    #    キーしか見ない検査は12件とも合格のままだった）。期待値は定数でなく文字列で直書きする。
    check("完了/-/- は両方を 投稿待ち・音声化待ち に進める（値まで一致）",
          plan_advance(_fake_page("a", "完了", "-", "-")),
          {"Status(Web)": {"status": {"name": "投稿待ち"}},
           "Status(Podcast)": {"status": {"name": "音声化待ち"}}})
    shapes = [("完了", w, p) for w in (None, "-", "投稿待ち", "スケジュール待ち", "完了")
              for p in (None, "-", "音声化待ち", "試聴待ち", "公開待ち", "投稿待ち", "完了")]
    written = set()
    for c, w, p in shapes:
        for k, v in plan_advance(_fake_page("s", c, w, p)).items():
            written.add((k, v["status"]["name"]))
    check("書く値は {Web→投稿待ち, Podcast→音声化待ち} の部分集合（公開待ち・試聴待ち・完了を書かない）",
          written <= {("Status(Web)", "投稿待ち"), ("Status(Podcast)", "音声化待ち")} and bool(written),
          True)
    check("完了/投稿待ち/- は Podcast だけ",
          sorted(plan_advance(_fake_page("b", "完了", "投稿待ち", "-"))), [PODCAST_PROP])
    check("完了/完了/試聴待ち は何もしない",
          plan_advance(_fake_page("c", "完了", "完了", "試聴待ち")), {})
    check("ファクトチェック待ち/-/- は何もしない",
          plan_advance(_fake_page("d", "ファクトチェック待ち", "-", "-")), {})
    check("完了/空/空 は進める（空＝既定扱い）",
          sorted(plan_advance(_fake_page("e", "完了", None, None))), sorted([WEB_PROP, PODCAST_PROP]))

    # 2) dry-run は1件以上を拾い、書かない
    auto = NotionAutomation(api_key="x", database_id="y")
    writes: list = []
    rows = [_fake_page("a", "完了", "-", "-", "昨日の形")]
    r = auto.process_content_complete_status(
        dry_run=True, pages=rows,
        fetch=lambda pid: (_ for _ in ()).throw(AssertionError("dry-run で取りに行った")),
        write=lambda pid, p: writes.append(pid) or True)
    check("dry-run: 昨日の形を1件拾う", (r["planned"], r["podcast"]), (1, 1))
    check("dry-run: 書かない", writes, [])

    # 3) 二重実行：1回目で進めた行は、2回目（同じ古い検索結果）では書かない
    db = {"a": _fake_page("a", "完了", "-", "-", "昨日の形")}

    def fake_write(pid, props):
        for k, v in props.items():
            db[pid]["properties"][k]["status"] = {"name": v["status"]["name"]}
        writes.append(pid)
        return True

    stale_rows = [_fake_page("a", "完了", "-", "-", "昨日の形")]   # 検索結果は古いまま
    r1 = auto.process_content_complete_status(pages=stale_rows,
                                              fetch=lambda pid: db[pid], write=fake_write)
    r2 = auto.process_content_complete_status(pages=stale_rows,
                                              fetch=lambda pid: db[pid], write=fake_write)
    check("1回目は進める", (r1["advanced"], r1["stale"]), (1, 0))
    check("2回目は書かない（取り直しで止まる）", (r2["advanced"], r2["stale"]), (0, 1))
    check("書いたのは1回だけ", writes, ["a"])

    # 4) 音声側が先に「試聴待ち」へ進めていたら巻き戻さない
    db["z"] = _fake_page("z", "完了", "投稿待ち", "試聴待ち")
    writes.clear()
    r3 = auto.process_content_complete_status(
        pages=[_fake_page("z", "完了", "投稿待ち", "-")],
        fetch=lambda pid: db[pid], write=fake_write)
    check("試聴待ちを音声化待ちに戻さない", (writes, _status_of(db["z"], PODCAST_PROP)), ([], "試聴待ち"))

    # 5) 書き込み失敗は failed に数える（黙って success にしない）
    r4 = auto.process_content_complete_status(
        pages=[_fake_page("f", "完了", "-", "-")],
        fetch=lambda pid: _fake_page("f", "完了", "-", "-"), write=lambda pid, p: False)
    check("書き込み失敗を数える", r4["failed"], 1)

    # 6) お知らせメールは廃止（2026-11-20）：送る道具を持たない
    import inspect
    src = inspect.getsource(NotionAutomation.process_content_complete_status)
    check("お知らせメールを送らない（mailer を持たず・処理にメールの語が無い）",
          ("mailer" in globals(), "send_" in src or "mail" in src), (False, False))

    print(f"\nself-test: {ok}件合格 / {fail}件不合格")
    return 0 if fail == 0 else 1


def main():
    """メイン実行関数。🔴 1件でも失敗があれば exit 1（Actions の失敗通知を飛ばす）。"""
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    dry_run = "--dry-run" in sys.argv

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('notion_automation.log', encoding='utf-8')
        ]
    )
    logger.info("=" * 50)
    logger.info("Notionステータス自動更新を開始します" + ("（dry-run・書かない）" if dry_run else ""))
    logger.info("=" * 50)

    try:
        r = NotionAutomation().process_content_complete_status(dry_run=dry_run)
    except Exception as e:
        logger.error(f"❌ 失敗: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    logger.info("\n" + "=" * 50)
    if dry_run:
        logger.info(f"dry-run: 候補 {r['found']} 件 / 進めるはず {r['planned']} 件"
                    f"（うち音声化待ちへ {r['podcast']} 件）")
    else:
        logger.info(f"ステータス更新: {r['advanced']} 件 / もう進んでいた {r['stale']} 件"
                    f" / 失敗 {r['failed']} 件")
    logger.info("=" * 50)
    if r["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
