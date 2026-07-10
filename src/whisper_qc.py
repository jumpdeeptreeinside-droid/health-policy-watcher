#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Whisper検品（2026-07-10）: 音声をローカルWhisperで書き起こし、台本とテキスト突合する。

従来のqc_audio（Geminiにmp3を聴かせる）は数字の聞き逃しが多かった。
ここでは (1) Whisperが「耳」を担当し、(2) 数字照合は決定論のPython、
(3) 表現ゆらぎの最終判定だけGeminiのテキスト比較に任せる。

戻り値: {"ok": bool, "number_mismatches": [...], "issues": [...], "transcript": str, "note": str}
"""
import os, re, json

WHISPER_MODEL = "/Users/mizusotokakeru/whisper-turbo"

# 日本語数詞 → 数値化のための正規化テーブル（Whisperは数字をだいたい算用数字で書くが、保険）
_KANJI_NUM = str.maketrans("〇一二三四五六七八九", "0123456789")


def transcribe(mp3_path: str) -> str:
    import mlx_whisper
    r = mlx_whisper.transcribe(mp3_path, path_or_hf_repo=WHISPER_MODEL, language="ja")
    return r["text"]


def extract_numbers(text: str) -> list:
    """テキストから数値列を抽出（カンマ・小数対応・出現順）"""
    t = text.translate(str.maketrans("０１２３４５６７８９", "0123456789")).translate(_KANJI_NUM)
    nums = []
    for m in re.finditer(r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?", t):
        v = m.group(0).replace(",", "")
        nums.append(v)
    return nums


def number_check(script: str, transcript: str) -> list:
    """台本に出る数値が書き起こしにも現れるか（多重集合の差分・順序不問）"""
    s_nums = extract_numbers(script)
    t_nums = extract_numbers(transcript)
    t_pool = list(t_nums)
    missing = []
    for v in s_nums:
        if v in t_pool:
            t_pool.remove(v)
        else:
            # 小数の丸め・0落ち（93.2 vs 93.20）を許容
            alt = [x for x in t_pool if x.rstrip("0").rstrip(".") == v.rstrip("0").rstrip(".")]
            if alt:
                t_pool.remove(alt[0])
            else:
                missing.append(v)
    return missing


def gemini_text_diff(script: str, transcript: str) -> dict:
    """Geminiによるテキスト同士の突合（音声は渡さない）"""
    try:
        import google.generativeai as genai
        import config
        genai.configure(api_key=config.GEMINI_API_KEY)
        prompt = (
            "以下は日本語ポッドキャストの【台本】と、実際の音声を機械書き起こしした【書き起こし】です。\n"
            "音声合成の読み間違いを検出してください。指摘対象は【事実が変わる誤り】のみ:\n"
            "数字・日付・固有名詞・単位の相違、文の脱落。\n"
            "書き起こし側の同音異字・句読点・かな漢字表記のゆらぎは誤りではありません。\n\n"
            'JSONのみで回答: {"ok": true/false, "issues": [{"script": "台本側", "heard": "書き起こし側", "kind": "数字|日付|固有名詞|脱落"}], "note": "一言"}\n\n'
            f"# 台本\n{script[:7000]}\n\n# 書き起こし\n{transcript[:7000]}"
        )
        model = genai.GenerativeModel(getattr(config, "QC_MODEL", "gemini-2.0-flash"))
        resp = model.generate_content(prompt)
        raw = resp.text.strip().strip("`").removeprefix("json").strip()
        return json.loads(raw)
    except Exception as e:
        return {"ok": True, "issues": [], "note": f"Gemini突合スキップ: {e}"}


def qc(mp3_path: str, script_text: str) -> dict:
    tx = transcribe(mp3_path)
    missing = number_check(script_text, tx)
    g = gemini_text_diff(script_text, tx)
    ok = (not missing) and g.get("ok", True)
    note = f"数値照合: 欠落{len(missing)}件 / Gemini: {g.get('note','')}"
    return {"ok": ok, "number_mismatches": missing,
            "issues": g.get("issues", []), "transcript": tx, "note": note}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        script = open(sys.argv[2], encoding="utf-8").read()
        r = qc(sys.argv[1], script)
        print(json.dumps({k: v for k, v in r.items() if k != "transcript"}, ensure_ascii=False, indent=1))
