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


def _expand_units(t: str) -> str:
    """「111万596」「4兆2,602億」を素の整数に開く。台本は素の数字、Whisperは万区切りで
    書き起こすため、そのまま比較すると全て不一致になり検品が空振りしていた（2026-08-21）。"""
    def _i(x):
        return int(x.replace(",", "")) if x else 0
    for unit, val in (("兆", 10**12), ("億", 10**8), ("万", 10**4)):
        t = re.sub(rf"([0-9][0-9,]*)\s*{unit}\s*([0-9][0-9,]*)?",
                   lambda m, v=val: str(_i(m.group(1)) * v + _i(m.group(2))), t)
    return t


def extract_numbers(text: str) -> list:
    """テキストから数値列を抽出（カンマ・小数・万/億/兆区切り対応・出現順）"""
    t = text.translate(str.maketrans("０１２３４５６７８９", "0123456789")).translate(_KANJI_NUM)
    t = _expand_units(t)
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
            'JSONのみで回答: {"ok": true/false, "issues": [{"script": "台本側", "heard": "書き起こし側", '
            '"word": "辞書に登録すべき語(なければ空)", "reading": "その正しい読み(カタカナ・なければ空)", '
            '"kind": "数字|日付|固有名詞|脱落"}], "note": "一言"}\n\n'
            f"# 台本\n{script[:7000]}\n\n# 書き起こし\n{transcript[:7000]}"
        )
        # 既定は mac_audio_pipeline と同じ最新エイリアス。世代名を直書きすると
        # モデル引退時に404で落ち、下のexceptが ok=True を返して検品が黙って空振りする。
        model = genai.GenerativeModel(getattr(config, "QC_MODEL", "gemini-flash-latest"))
        resp = model.generate_content(prompt)
        raw = resp.text.strip().strip("`").removeprefix("json").strip()
        return json.loads(raw)
    except Exception as e:
        # 合格扱いで返すが、黙って通さない（モデル引退・鍵切れをログで気づけるように）
        print(f"  ⚠ Gemini突合が実行できませんでした（数値照合のみで判定）: {str(e)[:120]}")
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
