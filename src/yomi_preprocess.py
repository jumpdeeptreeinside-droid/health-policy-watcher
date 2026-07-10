#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""読み上げ専用原稿への前処理（2026-07-10 木内さん発案「辞書のイタチごっこをやめる」）

方針: VOICEPEAKが読み間違える素材（数字・日付・英略語・単位）を、
      合成前にすべて「ひらがな/カタカナの読み」へ決定論的に変換する。
      LLMは使わない＝内容が変わるリスクゼロ・テスト可能。

使い方: from yomi_preprocess import to_yomi; text = to_yomi(text)
"""
import re

# ---------- 数字の読み ----------
_DIG = {"0": "ゼロ", "1": "いち", "2": "に", "3": "さん", "4": "よん",
        "5": "ご", "6": "ろく", "7": "なな", "8": "はち", "9": "きゅう"}
_D1 = {"1": "いち", "2": "に", "3": "さん", "4": "よん", "5": "ご",
       "6": "ろく", "7": "なな", "8": "はち", "9": "きゅう"}


def _yon_keta(n: int) -> str:
    """0-9999 の読み"""
    if n == 0:
        return "ゼロ"
    s = ""
    sen, n = divmod(n, 1000)
    hyaku, n = divmod(n, 100)
    juu, ichi = divmod(n, 10)
    if sen:
        s += {1: "せん", 3: "さんぜん", 8: "はっせん"}.get(sen, _D1[str(sen)] + "せん")
    if hyaku:
        s += {1: "ひゃく", 3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}.get(hyaku, _D1[str(hyaku)] + "ひゃく")
    if juu:
        s += ("じゅう" if juu == 1 else _D1[str(juu)] + "じゅう")
    if ichi:
        s += _D1[str(ichi)]
    return s


def num_to_yomi(numstr: str) -> str:
    """整数文字列（カンマ許容）→ 日本語読み（〜兆・億・万まで）"""
    n = int(numstr.replace(",", "").replace("，", ""))
    if n == 0:
        return "ゼロ"
    parts = []
    cho, n = divmod(n, 10**12)
    oku, n = divmod(n, 10**8)
    man, n = divmod(n, 10**4)
    if cho:
        parts.append(_yon_keta(cho) + "ちょう")
    if oku:
        parts.append(_yon_keta(oku) + "おく")
    if man:
        parts.append(_yon_keta(man) + "まん")
    if n:
        parts.append(_yon_keta(n))
    return "".join(parts)


def decimal_to_yomi(m_int: str, m_dec: str) -> str:
    return num_to_yomi(m_int) + "てん" + "".join(_DIG[d] for d in m_dec)


# ---------- 月日の読み ----------
_DAYS = {1: "ついたち", 2: "ふつか", 3: "みっか", 4: "よっか", 5: "いつか",
         6: "むいか", 7: "なのか", 8: "ようか", 9: "ここのか", 10: "とおか",
         14: "じゅうよっか", 20: "はつか", 24: "にじゅうよっか"}
_MONTHS = {1: "いちがつ", 2: "にがつ", 3: "さんがつ", 4: "しがつ", 5: "ごがつ", 6: "ろくがつ",
           7: "しちがつ", 8: "はちがつ", 9: "くがつ", 10: "じゅうがつ", 11: "じゅういちがつ", 12: "じゅうにがつ"}


def day_yomi(d: int) -> str:
    return _DAYS.get(d, num_to_yomi(str(d)) + "にち")


# ---------- 英略語 → カタカナ ----------
_LETTER = {"A": "エー", "B": "ビー", "C": "シー", "D": "ディー", "E": "イー", "F": "エフ",
           "G": "ジー", "H": "エイチ", "I": "アイ", "J": "ジェイ", "K": "ケー", "L": "エル",
           "M": "エム", "N": "エヌ", "O": "オー", "P": "ピー", "Q": "キュー", "R": "アール",
           "S": "エス", "T": "ティー", "U": "ユー", "V": "ブイ", "W": "ダブリュー",
           "X": "エックス", "Y": "ワイ", "Z": "ゼット"}

# 素読みしない既知語（カタカナ読み・慣用読み優先）
ACRONYMS = {
    "WHO": "ダブリューエイチオー", "iPS": "アイピーエス", "mRNA": "メッセンジャーアールエヌエー",
    "DNA": "ディーエヌエー", "RNA": "アールエヌエー", "AI": "エーアイ", "AMR": "エーエムアール",
    "PPI": "ピーピーアイ", "CKD": "シーケーディー", "COPD": "シーオーピーディー",
    "HPV": "エイチピーブイ", "HIV": "エイチアイブイ", "PCR": "ピーシーアール",
    "ICU": "アイシーユー", "NICU": "エヌアイシーユー", "GDP": "ジーディーピー",
    "OECD": "オーイーシーディー", "UNICEF": "ユニセフ", "NIH": "エヌアイエイチ",
    "CDC": "シーディーシー", "FDA": "エフディーエー", "EMA": "イーエムエー",
    "PMDA": "ピーエムディーエー", "NDB": "エヌディービー", "DPC": "ディーピーシー",
    "GLP-1": "ジーエルピーワン", "COVID-19": "コビッドナインティーン", "COVID": "コビッド",
    "M&A": "エムアンドエー", "TOB": "ティーオービー", "MBO": "エムビーオー",
    "CRO": "シーアールオー", "OTC": "オーティーシー", "DX": "ディーエックス",
    "EBM": "イービーエム", "QOL": "キューオーエル", "SNS": "エスエヌエス",
    "WHA": "ダブリューエイチエー", "GeMJ": "ジェムジェイ", "PHR": "ピーエイチアール",
    "RCT": "アールシーティー", "ChatGPT": "チャットジーピーティー",
}


def spell_acronym(word: str) -> str:
    return "".join(_LETTER.get(ch.upper(), ch) for ch in word if ch.isalpha())


def to_yomi(text: str) -> str:
    """表示用テキスト → 読み上げ用テキスト"""
    t = text
    # 全角→半角（数字・英字・記号）
    t = t.translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ％",
                                   "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ%"))

    # 既知略語（長いものから）
    for k in sorted(ACRONYMS, key=len, reverse=True):
        t = t.replace(k, ACRONYMS[k])

    # 日付: N月N日
    t = re.sub(r"([0-9]{1,2})月([0-9]{1,2})日",
               lambda m: _MONTHS[int(m.group(1))] + day_yomi(int(m.group(2))), t)
    # 年度・年（年度を先に）
    t = re.sub(r"令和([0-9]{1,2})年度", lambda m: "れいわ" + num_to_yomi(m.group(1)) + "ねんど", t)
    t = re.sub(r"([0-9]{4})年度", lambda m: num_to_yomi(m.group(1)) + "ねんど", t)
    t = re.sub(r"令和([0-9]{1,2})年", lambda m: "れいわ" + num_to_yomi(m.group(1)) + "ねん", t)
    t = re.sub(r"([0-9]{4})年", lambda m: num_to_yomi(m.group(1)) + "ねん", t)
    t = re.sub(r"令和([0-9]{1,2})", lambda m: "れいわ" + num_to_yomi(m.group(1)), t)

    # 医療カウンターの読み固定（数字が算用のうちに処理）。床=しょう（病床）。必要に応じ追記。
    t = re.sub(r"([0-9][0-9,]*)\s*床", lambda m: num_to_yomi(m.group(1)) + "しょう", t)

    # パーセント（小数対応）
    t = re.sub(r"([0-9][0-9,]*)\.([0-9]+)\s*(?:%|％|パーセント)",
               lambda m: decimal_to_yomi(m.group(1), m.group(2)) + "パーセント", t)
    t = re.sub(r"([0-9][0-9,]*)\s*(?:%|％|パーセント)",
               lambda m: num_to_yomi(m.group(1)) + "パーセント", t)

    # 小数+兆/億/万（例: 306.0兆円）
    t = re.sub(r"([0-9][0-9,]*)\.([0-9]+)(兆|億|万)",
               lambda m: decimal_to_yomi(m.group(1), m.group(2)) + {"兆": "ちょう", "億": "おく", "万": "まん"}[m.group(3)], t)

    # 小数（単位なし・％処理後）
    t = re.sub(r"([0-9][0-9,]*)\.([0-9]+)",
               lambda m: decimal_to_yomi(m.group(1), m.group(2)), t)

    # 数字+兆/億/万+円等の複合（例: 1,453億円 / 306.0兆円は小数処理済みなので残りは整数）
    t = re.sub(r"([0-9][0-9,]*)(兆|億|万)",
               lambda m: num_to_yomi(m.group(1)) + {"兆": "ちょう", "億": "おく", "万": "まん"}[m.group(2)], t)

    # 残りの整数（3桁超 or カンマ入りは読みに展開。1〜2桁は素直に読めるので温存）
    t = re.sub(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{3,}",
               lambda m: num_to_yomi(m.group(0)), t)

    # 英語の一字読みはしない（＝原稿生成側で日本語化する方針・2026-07-10 木内さん判断）。
    # 既知の頻出略語(ACRONYMS)だけカタカナ化済み。未知の英語はそのまま残し、LLM側で潰す。
    return t


def to_yomi_english_only(text: str) -> str:
    """英語・略語だけをカタカナ/一字読みに変換し、数字・日付・漢字は原文のまま残す。
    ＝VOICEPEAKの抑揚エンジンに漢字と数字の読み判断を任せ、素読みされる英語だけ潰す方式。"""
    t = text.translate(str.maketrans("ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
                                     "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    for k in sorted(ACRONYMS, key=len, reverse=True):
        t = t.replace(k, ACRONYMS[k])
    return t


if __name__ == "__main__":
    tests = [
        "残高1,453億円の余剰を財務省が指摘",
        "実施率は93.2%に上昇",
        "積立金は306.0兆円に増加",
        "2026年7月8日 世界の医療・保健ニュース16本",
        "令和8年度第1回運営委員会",
        "WHOとCDCがGLP-1とCOVID-19について報告",
        "献血推進2025が令和10年度まで延長、国内自給率100%を目指す",
        "第135回介護保険部会が開催",
        "1.0%増、10〜15歳未満の伸び率",
        "未知の略語GeMJやNCCHDも読む",
    ]
    for s in tests:
        print(f"  {s}\n→ {to_yomi(s)}\n")
