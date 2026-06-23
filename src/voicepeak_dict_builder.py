#!/usr/bin/env python3
"""VOICEPEAK ユーザー辞書ビルダー（医療政策ウォッチャー改訂・サク依頼2026-06-23 の🔴律速の本丸）。

狙い：音声原稿の総量が増えても、緑さんの「読み間違い修正」を増やさない。
固有名詞（WHO・各審議会名・専門用語・略語・難読漢字）を VOICEPEAK のユーザー辞書
（src/podcast_dictionary.json ＝ [{"sur": 表層, "pron": カタカナ読み}, ...]）へ
半自動で蓄積する。原稿が出るたびに候補を抽出→（任意でGeminiが読みを付与）→既存辞書へマージ。

設計の肝：
  - **既存の人手読みは絶対に上書きしない**（pronが既にある sur はマージ時に温存）。
  - オフラインのヒューリスティック抽出（英字略語・英数字・数字+単位など TTS が誤読しやすい型）は鍵不要で動く。
    難読漢字の「読み」確定は Gemini パス（--llm）か人手に委ねる（オフラインは候補出しまで）。
  - 既存パイプラインを壊さない孤立モジュール（podcast_dictionary.json は現状どのコードからも未参照）。
    台本生成直後にフックする想定だが、自動配線はサク／木内さんのレビュー後に行う（本ファイル末尾の note 参照）。

使い方：
  # オフライン（鍵不要）：原稿から要登録候補を洗い出し、読み未定のテンプレを出力（辞書は変更しない）
  python src/voicepeak_dict_builder.py --script-file script.txt --no-llm
  # Geminiで読みを付与して辞書へマージ（要 GEMINI_API_KEY / config.py）
  python src/voicepeak_dict_builder.py --script-file script.txt --llm
  # 変更内容だけ確認（書き込まない）
  python src/voicepeak_dict_builder.py --script-file script.txt --llm --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DICT_PATH = Path(__file__).parent / "podcast_dictionary.json"

# --- TTS が誤読しやすく、かつ機械的に拾える型（オフライン抽出の対象） ---
# 英字略語・頭字語（WHO, NDB, FIP, COVID）／英数字混在（G20, SDGs, COVID-19）
_RE_ALNUM = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ][A-Za-z0-9Ａ-Ｚａ-ｚ０-９\-]{1,}")
# 数字+単位（2日間, 3割, 5疾病 など 読みが揺れやすい）
_RE_NUMUNIT = re.compile(r"[0-9０-９]+[年月日割兆億万人件床例週日間期次種剤]+")
# 連続漢字（2字以上）＝難読の候補源（読みはオフラインでは確定しない＝Gemini/人手へ）
_RE_KANJI = re.compile(r"[一-龥々]{2,}")


def load_dict(path: Path = DICT_PATH) -> list[dict]:
    """辞書JSONを読み込む。無ければ空。"""
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [e for e in data if isinstance(e, dict) and e.get("sur")]


def known_surfaces(entries: list[dict]) -> set[str]:
    return {e["sur"] for e in entries if e.get("sur")}


def extract_candidates(text: str, known: set[str], include_kanji: bool = True) -> list[str]:
    """原稿から『辞書未登録で、読みを固定しておきたい』表層候補を抽出（出現順・重複排除）。
    英字略語/英数字/数字+単位は確度高め。漢字連語は候補源（読み確定はLLM/人手）。"""
    seen: dict[str, None] = {}
    pats = [_RE_ALNUM, _RE_NUMUNIT] + ([_RE_KANJI] if include_kanji else [])
    for pat in pats:
        for m in pat.finditer(text or ""):
            sur = m.group(0).strip("-")
            if not sur or sur in known or sur in seen:
                continue
            # 1文字英字・純数字のみは除外（誤読しない/きりがない）
            if len(sur) < 2 or sur.isdigit():
                continue
            seen[sur] = None
    return list(seen.keys())


def merge_entries(existing: list[dict], new_entries: list[dict]) -> tuple[list[dict], int, int]:
    """new_entries を existing にマージ。既存の sur は読みを温存（上書きしない）。
    返り値: (マージ後リスト, 追加数, スキップ数[既存と重複])。"""
    by_sur = {e["sur"]: e for e in existing}
    added = skipped = 0
    for ne in new_entries:
        sur, pron = ne.get("sur"), ne.get("pron")
        if not sur or not pron:
            continue
        if sur in by_sur:
            skipped += 1            # 既存＝人手読みを尊重して触らない
            continue
        by_sur[sur] = {"sur": sur, "pron": pron}
        added += 1
    # 既存の順序を保ち、新規は末尾へ追記
    merged = existing + [by_sur[s] for s in by_sur if s not in {e["sur"] for e in existing}]
    return merged, added, skipped


def save_dict(entries: list[dict], path: Path = DICT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------- Gemini パス（任意・要鍵） ----------------
_PROMPT_READING = """あなたは日本語音声合成（VOICEPEAK）の読み辞書を整備する校正者です。
次の音声原稿に出てくる固有名詞・専門用語・略語・難読語のうち、
TTSが誤読しやすく読みを固定すべきものだけを抜き出し、カタカナ読みを付けてください。

ルール:
- 一般的で誤読しない語（普通名詞・ひらがな語・読みが自明な語）は出さない。
- 数字を含む語は文脈に沿った日本語読み（例 G20→ジートゥエンティー、2日間→フツカカン）。
- 英字略語は通用読み（WHO→ダブリューエイチオー、SDGs→エスディージーズ）。
- 出力は JSON 配列のみ。各要素 {"sur": 表層そのまま, "pron": 全角カタカナ}。説明文は不要。

参考（既に辞書にある語＝重複して出さない）:
%(known)s

抽出候補（この中から本当に必要なものだけ・候補外でも原稿にあれば追加可）:
%(cands)s

音声原稿:
---
%(script)s
---
"""


def generate_readings_with_gemini(script_text: str, candidates: list[str], known: set[str]) -> list[dict]:
    """Gemini で表層→カタカナ読みを生成。要 GEMINI_API_KEY（環境変数 or config.py）。"""
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        print("[ERROR] google-genai 未導入: pip install google-genai", file=sys.stderr)
        return []
    key, model = _load_gemini_config()
    client = genai.Client(api_key=key)
    prompt = _PROMPT_READING % {
        "known": "、".join(sorted(known))[:4000],
        "cands": "、".join(candidates[:200]) or "（候補なし・原稿から判断）",
        "script": (script_text or "")[:12000],
    }
    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.1),
    )
    return _parse_json_array(resp.text)


def _parse_json_array(text: str) -> list[dict]:
    """LLM応答から JSON 配列を頑健に取り出す（```json フェンスや前後文を許容）。"""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for e in arr:
        if isinstance(e, dict) and e.get("sur") and e.get("pron"):
            out.append({"sur": str(e["sur"]), "pron": str(e["pron"])})
    return out


def _load_gemini_config() -> tuple[str, str]:
    """既存スクリプト（github_content_generator._load_config）と同じ流儀：環境変数→config.py。"""
    import os
    key = os.environ.get("GEMINI_API_KEY")
    model = os.environ.get("GEMINI_MODEL")
    if not key or not model:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import config as cfg
            key = key or getattr(cfg, "GEMINI_API_KEY", None)
            model = model or getattr(cfg, "GEMINI_MODEL_NAME", None)
        except ImportError:
            pass
    if not key:
        print("[ERROR] GEMINI_API_KEY が環境変数にも config.py にもありません。", file=sys.stderr)
        sys.exit(1)
    return key, model or "gemini-3-flash-preview"


# ---------------- パイプライン連携用の公開関数 ----------------
def update_dictionary_from_script(script_text: str, *, use_llm: bool = True,
                                  dict_path: Path = DICT_PATH, dry_run: bool = False) -> dict:
    """台本テキスト1本から辞書を更新する。台本生成直後のフック用（自動配線はレビュー後）。
    返り値: {"added":n, "skipped":n, "candidates":[...], "new":[...]}。"""
    entries = load_dict(dict_path)
    known = known_surfaces(entries)
    candidates = extract_candidates(script_text, known)
    new_entries = generate_readings_with_gemini(script_text, candidates, known) if use_llm else []
    merged, added, skipped = merge_entries(entries, new_entries)
    if added and not dry_run:
        save_dict(merged, dict_path)
    return {"added": added, "skipped": skipped, "candidates": candidates, "new": new_entries}


def main():
    ap = argparse.ArgumentParser(description="VOICEPEAK ユーザー辞書を音声原稿から半自動更新")
    ap.add_argument("--script-file", help="音声原稿テキストファイル", required=True)
    ap.add_argument("--dict", default=str(DICT_PATH), help="辞書JSON（既定 src/podcast_dictionary.json）")
    ap.add_argument("--llm", dest="llm", action="store_true", help="Geminiで読みを付与してマージ")
    ap.add_argument("--no-llm", dest="llm", action="store_false", help="候補出しのみ（辞書は変更しない）")
    ap.add_argument("--dry-run", action="store_true", help="書き込まずに結果のみ表示")
    ap.set_defaults(llm=True)
    args = ap.parse_args()

    script = Path(args.script_file).read_text(encoding="utf-8")
    dict_path = Path(args.dict)

    if not args.llm:
        # オフライン：候補を読み未定テンプレで出力（人手 or 後でLLM）
        entries = load_dict(dict_path)
        cands = extract_candidates(script, known_surfaces(entries))
        out = dict_path.with_name(dict_path.stem + "_candidates.txt")
        out.write_text("\n".join(f"{c}\t" for c in cands) + "\n", encoding="utf-8")
        print(f"候補 {len(cands)}件（辞書未登録）→ {out}")
        print("  例:", "、".join(cands[:15]))
        print("  ※読み（pron）を付けて --llm で再実行するか、手で辞書へ追記してください。")
        return

    res = update_dictionary_from_script(script, use_llm=True, dict_path=dict_path, dry_run=args.dry_run)
    tag = "（dry-run・未書込）" if args.dry_run else ""
    print(f"追加 {res['added']}件 / 既存温存 {res['skipped']}件{tag}")
    for e in res["new"]:
        mark = "＋" if e["sur"] not in {x for x in []} else "＝"
        print(f"  {mark} {e['sur']} → {e['pron']}")


if __name__ == "__main__":
    main()
