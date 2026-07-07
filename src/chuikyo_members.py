#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中医協 発言者のフルネーム解決＋側（公益/支払側/診療側）付与（Phase2・2026-07-08未明）。

発言行は姓のみ（○森委員）だが出席者欄はフルネーム（森昌平委員）。
会合ごとに出席者欄をパースして姓→フルネームを解決する＝同姓でも時代が違えば別人として
正しく扱える（例: 松本吉郎=診療側/日医 と 松本真人=支払側/健保連）。

側の対応表はフルネーム単位のシード（確実な主要委員のみ・段階拡充）。未収載は未分類のまま表示しない。
使い方: python3 src/chuikyo_members.py
"""
import glob
import json
import os
import re
import sqlite3

DB = os.path.expanduser("~/crosshealth_search.db")
ARCHIVE = os.path.expanduser("~/chuikyo_archive")

# 側の対応表（フルネーム→側）。確実に分かる主要委員からシード（2026-07-08時点・順次拡充）。
# 公益＝会長・学識、支払側＝1号側（保険者・被保険者代表）、診療側＝2号側（医療提供者代表）。
MEMBER_SIDE = {
    # 歴代会長（公益）
    "土田武史": "公益", "遠藤久夫": "公益", "森田朗": "公益", "田辺国昭": "公益",
    "小塩隆士": "公益", "城山英明": "公益",
    # 公益委員（学識）
    "飯塚敏晃": "公益", "野口晴子": "公益", "中村洋": "公益", "関ふ佐子": "公益",
    "荒井耕": "公益", "牛丸聡": "公益", "小林麻理": "公益", "関原健夫": "公益",
    "印南一路": "公益", "西村万里子": "公益", "松原由美": "公益", "永瀬伸子": "公益",
    "笠木映里": "公益", "本田文子": "公益", "菅原琢磨": "公益", "井深陽子": "公益",
    "安川文朗": "公益", "秋山美紀": "公益", "大野博": "公益",
    # 支払側（健保連・協会けんぽ・連合・患者代表等）
    "白川修二": "支払側", "幸野庄司": "支払側", "松本真人": "支払側", "佐竹陽一": "支払側",
    "吉森俊和": "支払側", "安藤伸樹": "支払側", "鳥潟美夏子": "支払側",
    "平川則男": "支払側", "間宮清": "支払側", "小林剛": "支払側", "中島圭子": "支払側",
    "勝村久司": "支払側", "北村光一": "支払側", "田中伸一": "支払側", "伊藤文郎": "支払側",
    "宮近清文": "支払側", "松浦満晴": "支払側", "眞田享": "支払側", "末松則子": "支払側",
    "高町晃司": "支払側", "奥田好秀": "支払側", "鈴木順三": "支払側", "伊藤徳宇": "支払側",
    "永井幸子": "支払側", "袖井孝子": "支払側", "対馬忠明": "支払側", "小島茂": "支払側",
    # 診療側（日医・日歯・日薬・病院団体）
    "中川俊男": "診療側", "松本吉郎": "診療側", "今村聡": "診療側", "城守国斗": "診療側",
    "猪口雄二": "診療側", "島弘志": "診療側", "長島公之": "診療側", "江澤和彦": "診療側",
    "茂松茂人": "診療側", "黒瀨巌": "診療側", "太田圭洋": "診療側", "大杉和司": "診療側",
    "小阪真二": "診療側", "池端幸彦": "診療側", "鈴木邦彦": "診療側", "安達秀樹": "診療側",
    "嘉山孝正": "診療側", "西澤寛俊": "診療側", "邉見公雄": "診療側", "万代恭嗣": "診療側",
    "中村利仁": "診療側", "松原謙二": "診療側", "竹嶋康弘": "診療側", "藤原淳": "診療側",
    "鈴木満": "診療側", "西村博明": "診療側",
    # 診療側・歯科（日歯）
    "遠藤秀樹": "診療側", "堀憲郎": "診療側", "渡辺三雄": "診療側", "林正純": "診療側",
    "山科透": "診療側", "中村春基": "診療側",
    # 診療側・薬剤師（日薬）
    "森昌平": "診療側", "安部好弘": "診療側", "三浦洋嗣": "診療側", "有澤賢二": "診療側",
    "山本信夫": "診療側",
    # 第2次シード（2026-07-08・発言数上位の未付与から確実なもの）
    "松本純一": "診療側",  # 日医
    "長瀬輝諠": "診療側",  # 日本精神科病院協会
    "花井十伍": "支払側", "花井圭子": "支払側", "佐保昌一": "支払側", "石山惠司": "支払側",
}

NAME_RE = re.compile(r"([一-鿿々]{1,4}[一-鿿々ぁ-ゖァ-ヶ]{1,6})(会長代理|会長|委員長|専門委員|委員|参考人)")


def parse_attendees(text: str) -> dict:
    """出席者欄から {姓側の前方一致キー: フルネーム} を作る"""
    i = text.find("出席者")
    if i < 0:
        return {}
    j = text.find("議題", i)
    seg = text[i:j if j > i else i + 1200]
    full = {}
    for m in NAME_RE.finditer(seg):
        name = m.group(1)
        # 「事務局」等の誤マッチ除外
        if name.endswith(("事務")) or len(name) < 2:
            continue
        full[name] = m.group(2)
    return full  # {フルネーム: 肩書}


def main():
    db = sqlite3.connect(DB)
    cols = [r[1] for r in db.execute("PRAGMA table_info(chuikyo_utt)")]
    if "full_name" not in cols:
        db.execute("ALTER TABLE chuikyo_utt ADD COLUMN full_name TEXT")
    if "side" not in cols:
        db.execute("ALTER TABLE chuikyo_utt ADD COLUMN side TEXT")

    resolved = ambiguous = 0
    for f in glob.glob(os.path.join(ARCHIVE, "*.json")):
        rec = json.load(open(f, encoding="utf-8"))
        kai = rec.get("kai")
        if kai is None:
            continue
        attendees = parse_attendees(rec["text"])
        if not attendees:
            continue
        speakers = [r[0] for r in db.execute(
            "SELECT DISTINCT speaker FROM chuikyo_utt WHERE kai=? AND kind IN ('委員','参考人')", (kai,))]
        for s in speakers:
            matches = [fn for fn in attendees if fn.startswith(s)]
            if len(matches) == 1:
                fn = matches[0]
                side = MEMBER_SIDE.get(fn)
                db.execute("UPDATE chuikyo_utt SET full_name=?, side=? WHERE kai=? AND speaker=?",
                           (fn, side, kai, s))
                resolved += 1
            elif len(matches) > 1:
                ambiguous += 1
    db.commit()
    n_full, n_side = db.execute(
        "SELECT COUNT(*), SUM(side IS NOT NULL) FROM chuikyo_utt WHERE kind='委員'").fetchone()
    covered = db.execute(
        "SELECT COUNT(*) FROM chuikyo_utt WHERE kind='委員' AND full_name IS NOT NULL").fetchone()[0]
    print(f"✓ フルネーム解決: 話者×会合 {resolved}件（同姓同席で保留{ambiguous}件）")
    print(f"✓ 委員発言{n_full:,}のうち フルネーム付与{covered:,}（{100*covered/n_full:.0f}%）・側付与{n_side:,}（{100*n_side/n_full:.0f}%）")
    db.close()


if __name__ == "__main__":
    main()
