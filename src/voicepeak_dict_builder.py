#!/usr/bin/env python3
"""VOICEPEAK ユーザー辞書ビルダー（医療政策ウォッチャー改訂・サク依頼2026-06-23 の🔴律速の本丸）。

狙い：音声原稿の総量が増えても、緑さんの「読み間違い修正」を増やさない。
固有名詞（WHO・各審議会名・専門用語・略語・難読語）を VOICEPEAK のユーザー辞書
（src/podcast_dictionary.json ＝ [{"sur": 表層, "pron": カタカナ読み}, ...]）へ
半自動で蓄積する。原稿が出るたびに候補を抽出→Geminiが読みを提案→**緑さんが確認**→辞書へマージ。

設計の肝（サクレビュー2026-06-23 反映）：
  - 🔴**Geminiの読みは自動採用しない（緑さん確認を既定動作に）**。Geminiは専門用語・機関名の読みを誤りうり、
    誤読が辞書に入ると「常に間違って読む＝体系的誤読」＝緑さんの都度修正より悪い。よって --llm は
    **提案ファイル（*_proposals.tsv）を書くだけで辞書は変更しない**。緑さんが読みを確認/修正→ --apply で本採用。
  - 🔴**既存の人手読みは絶対に上書きしない**（マージは sur 重複時に既存 pron を温存）。
  - オフライン候補抽出は鍵不要。**漢字候補は既定OFF**（読み自明な普通名詞のノイズ源＝難読はGeminiに委ねる）。
    **数字+単位（2日間/5疾病）は辞書化しない**（数字が変わると別エントリ要＝辞書膨張＝VOICEPEAKの固定sur→pronと不適合）。
    辞書は固有名詞・略語・難読語（不変のもの）に集中。
  - 既存パイプライン非破壊（podcast_dictionary.json は現状孤立ファイル）。台本生成直後フックは承認後（末尾note）。

使い方：
  # 鍵不要：要登録候補の洗い出し（辞書は変更しない）
  python src/voicepeak_dict_builder.py --script-file 台本.txt --no-llm
  # Geminiで読みを"提案"（辞書は変更しない・提案tsvを出力）→ 緑さんが確認/修正
  python src/voicepeak_dict_builder.py --script-file 台本.txt --llm
  # 緑さん確認済みの提案tsvを辞書へ本採用（pron空行は不採用）
  python src/voicepeak_dict_builder.py --apply podcast_dictionary_proposals.tsv [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DICT_PATH = Path(__file__).parent / "podcast_dictionary.json"

# --- TTSが誤読しやすく機械的に拾える型（辞書化に向く＝不変の固有名詞・略語） ---
# 英字略語/頭字語・英数字混在の固有名詞（WHO, NDB, FIP, SDGs, G20, COVID-19）＝数字を含んでも語として不変
_RE_ALNUM = re.compile(r"[A-Za-zＡ-Ｚａ-ｚ][A-Za-z0-9Ａ-Ｚａ-ｚ０-９\-]{1,}")
# 連続漢字（2字以上）＝難読の候補源。ただし普通名詞のノイズが多いので既定OFF（include_kanji=Trueで有効）
_RE_KANJI = re.compile(r"[一-龥々]{2,}")
# ※数字+単位（2日間・5疾病）は辞書化しない（サク条件3）。読み揺れは原稿生成側で吸収する。


def load_dict(path: Path = DICT_PATH) -> list[dict]:
    """辞書JSONを読み込む。無ければ空。"""
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [e for e in data if isinstance(e, dict) and e.get("sur")]


def known_surfaces(entries: list[dict]) -> set[str]:
    return {e["sur"] for e in entries if e.get("sur")}


def extract_candidates(text: str, known: set[str], include_kanji: bool = False) -> list[str]:
    """原稿から『辞書未登録で読みを固定したい』表層候補を抽出（出現順・重複排除）。
    既定は英字略語/英数字の固有名詞のみ（確度高）。include_kanji=Trueで難読漢字源も拾うがノイズ増。"""
    seen: dict[str, None] = {}
    pats = [_RE_ALNUM] + ([_RE_KANJI] if include_kanji else [])
    for pat in pats:
        for m in pat.finditer(text or ""):
            sur = m.group(0).strip("-")
            if not sur or sur in known or sur in seen:
                continue
            if len(sur) < 2 or sur.isdigit():     # 1文字・純数字は除外
                continue
            seen[sur] = None
    return list(seen.keys())


def merge_entries(existing: list[dict], new_entries: list[dict]) -> tuple[list[dict], int, int]:
    """new_entries を existing にマージ。既存 sur は読みを温存（上書きしない）。
    返り値: (マージ後リスト, 追加数, スキップ数[既存と重複])。"""
    existing_surs = {e["sur"] for e in existing}     # 1回だけ構築（O(n)・nitのO(n²)解消）
    by_sur = {e["sur"]: e for e in existing}
    added = skipped = 0
    for ne in new_entries:
        sur, pron = ne.get("sur"), ne.get("pron")
        if not sur or not pron:
            continue
        if sur in by_sur:
            skipped += 1                              # 既存＝人手読みを尊重して触らない
            continue
        by_sur[sur] = {"sur": sur, "pron": pron}
        added += 1
    merged = existing + [by_sur[s] for s in by_sur if s not in existing_surs]
    return merged, added, skipped


def save_dict(entries: list[dict], path: Path = DICT_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------- 提案tsv（緑さん確認用）の読み書き ----------------
def write_proposals(proposals: list[dict], path: Path) -> None:
    """提案を tsv（sur<TAB>pron）で出力。緑さんが pron を確認/修正し、不採用は行削除 or pron空に。"""
    lines = ["# 緑さん確認用：読み(pron)を確認・修正してください。不採用は行削除 or pron空欄。確認後 --apply で本採用。",
             "# 表層(sur)\t読み(pron・全角カタカナ)"]
    lines += [f"{p['sur']}\t{p.get('pron','')}" for p in proposals]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_proposals(path: Path) -> list[dict]:
    """緑さん確認済み tsv を読む。'#'始まり・pron空は不採用として無視。"""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
            out.append({"sur": parts[0].strip(), "pron": parts[1].strip()})
    return out


# ---------------- Gemini パス（読みの"提案"のみ・要鍵） ----------------
_PROMPT_READING = """あなたは日本語音声合成（VOICEPEAK）の読み辞書を整備する校正者です。
次の音声原稿に出てくる固有名詞・専門用語・略語・難読語のうち、
TTSが誤読しやすく読みを固定すべきものだけを抜き出し、カタカナ読みを付けてください。

ルール:
- 一般的で誤読しない語（普通名詞・ひらがな語・読みが自明な語）は出さない。
- 対象は「不変の固有名詞・機関名・略語・難読語」。**数字+単位（2日間/5疾病など可変の語）は出さない**。
- 英字略語は通用読み（WHO→ダブリューエイチオー、SDGs→エスディージーズ、G20→ジートゥエンティー）。
- 確信が持てない読みは出さない（誤読を辞書に入れない方が、空欄で人に委ねるより安全）。
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


def propose_readings_with_gemini(script_text: str, candidates: list[str], known: set[str]) -> list[dict]:
    """Gemini で表層→カタカナ読みを"提案"（辞書には書かない）。既存surは除外。要 GEMINI_API_KEY。"""
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
    return [e for e in _parse_json_array(resp.text) if e["sur"] not in known]


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
    return [{"sur": str(e["sur"]), "pron": str(e["pron"])}
            for e in arr if isinstance(e, dict) and e.get("sur") and e.get("pron")]


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
def propose_from_script(script_text: str, *, use_llm: bool = True,
                        dict_path: Path = DICT_PATH) -> dict:
    """台本1本から『辞書追加の提案』を作る（🔴辞書は変更しない・緑さん確認用）。台本生成直後フック用。
    返り値: {"candidates":[...表層], "proposals":[{sur,pron}], "proposals_path": str|None}。
    proposals は緑さんが確認後、apply_proposals() で本採用する。"""
    entries = load_dict(dict_path)
    known = known_surfaces(entries)
    candidates = extract_candidates(script_text, known)              # 既定：英字略語/英数字のみ
    proposals = propose_readings_with_gemini(script_text, candidates, known) if use_llm else []
    ppath = None
    if proposals:
        ppath = Path(dict_path).with_name(Path(dict_path).stem + "_proposals.tsv")
        write_proposals(proposals, ppath)
    return {"candidates": candidates, "proposals": proposals, "proposals_path": str(ppath) if ppath else None}


def apply_proposals(proposals: list[dict], *, dict_path: Path = DICT_PATH, dry_run: bool = False) -> dict:
    """緑さん確認済みの提案を辞書へマージ（人手読みは温存）。返り値: {"added","skipped"}。"""
    entries = load_dict(dict_path)
    merged, added, skipped = merge_entries(entries, proposals)
    if added and not dry_run:
        save_dict(merged, dict_path)
    return {"added": added, "skipped": skipped}


def main():
    ap = argparse.ArgumentParser(description="VOICEPEAK ユーザー辞書を音声原稿から半自動更新（読みは緑さん確認が既定）")
    ap.add_argument("--script-file", help="音声原稿テキストファイル（--no-llm / --llm 用）")
    ap.add_argument("--apply", metavar="TSV", help="緑さん確認済みの提案tsvを辞書へ本採用")
    ap.add_argument("--dict", default=str(DICT_PATH), help="辞書JSON（既定 src/podcast_dictionary.json）")
    ap.add_argument("--llm", dest="llm", action="store_true", help="Geminiで読みを提案（辞書は変更しない）")
    ap.add_argument("--no-llm", dest="llm", action="store_false", help="候補出しのみ")
    ap.add_argument("--include-kanji", action="store_true", help="オフライン候補に難読漢字源も含める（ノイズ増）")
    ap.add_argument("--dry-run", action="store_true", help="--apply時に書き込まず結果のみ表示")
    ap.set_defaults(llm=True)
    args = ap.parse_args()
    dict_path = Path(args.dict)

    # 本採用モード：緑さん確認済み提案 → 辞書マージ
    if args.apply:
        proposals = read_proposals(Path(args.apply))
        res = apply_proposals(proposals, dict_path=dict_path, dry_run=args.dry_run)
        tag = "（dry-run・未書込）" if args.dry_run else ""
        print(f"本採用 {res['added']}件 / 既存温存スキップ {res['skipped']}件{tag}（確認済 {len(proposals)}件）")
        return

    if not args.script_file:
        ap.error("--script-file か --apply のいずれかが必要です")
    script = Path(args.script_file).read_text(encoding="utf-8")
    known = known_surfaces(load_dict(dict_path))

    if not args.llm:
        cands = extract_candidates(script, known, include_kanji=args.include_kanji)
        out = dict_path.with_name(dict_path.stem + "_candidates.txt")
        out.write_text("\n".join(f"{c}\t" for c in cands) + "\n", encoding="utf-8")
        print(f"候補 {len(cands)}件（辞書未登録）→ {out}")
        print("  例:", "、".join(cands[:15]) or "（なし）")
        print("  ※読み(pron)を付けて手で辞書へ、または --llm で提案を作成。")
        return

    # --llm：読みを"提案"するだけ（🔴辞書は変更しない）。緑さん確認→ --apply で本採用。
    res = propose_from_script(script, use_llm=True, dict_path=dict_path)
    print(f"Gemini提案 {len(res['proposals'])}件（辞書は未変更）")
    for e in res["proposals"]:
        print(f"  ・{e['sur']} → {e['pron']}")
    if res["proposals_path"]:
        print(f"\n→ 提案を {res['proposals_path']} に出力。")
        print(f"  緑さんが読みを確認/修正後：python {Path(__file__).name} --apply {res['proposals_path']}")


if __name__ == "__main__":
    main()
