#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中医協 構造化DB（Phase2・2026-07-07深夜 着手）。

~/chuikyo_archive/ の議事録466会合を「発言」単位に分解する:
  誰が（発言者・肩書・区分）／どの会合で（回・日付）／何を言ったか（発言本文）
→ ~/crosshealth_search.db の chuikyo_utt テーブル＋FTS5（trigram）

発言の区切り＝「○発言者名」だけの行（中医協議事録の標準書式・第108回〜第650回で共通）。
区分: 委員系（会長/委員/専門委員/部会長/委員長等）・事務局（〜課長/企画官/管理官等）・参考人。

使い方: python3 src/chuikyo_structurize.py [--rebuild]
"""
import glob
import json
import os
import re
import sqlite3
import sys

DB = os.path.expanduser("~/crosshealth_search.db")
ARCHIVE = os.path.expanduser("~/chuikyo_archive")

# 発言者行のパターン。「○城山会長」「○清原薬剤管理官」「○事務局（宇都宮企画官）」等。
ROLE_TAIL = ("委員長代理", "会長代理", "専門委員", "部会長", "小委員長", "委員長", "会長",
             "委員", "参考人", "構成員", "課長補佐", "企画官", "審議官", "管理官", "課長",
             "室長", "局長", "参事官", "技官", "薬剤管理官", "歯科医療管理官", "数理官")
SPEAKER_RE = re.compile(r"^○([^\s　○]{1,24})$")

KIND_JIMU = ("課長", "企画官", "審議官", "管理官", "室長", "局長", "参事官", "技官", "補佐", "事務局")


def classify(speaker: str) -> tuple:
    """発言者トークン→(氏名, 肩書, 区分)"""
    if speaker.startswith("事務局"):
        m = re.search(r"（(.+?)）", speaker)
        inner = m.group(1) if m else ""
        return (re.sub(r"(課長補佐|企画官|審議官|管理官|課長|室長|局長|参事官|技官).*$", "", inner) or "事務局",
                inner or "事務局", "事務局")
    for tail in ROLE_TAIL:
        if speaker.endswith(tail):
            name = speaker[: -len(tail)]
            kind = "事務局" if any(k in tail for k in KIND_JIMU) else (
                "参考人" if tail in ("参考人", "構成員") else "委員")
            return (name or speaker, tail, kind)
    return (speaker, "", "その他")


def is_speaker_line(line: str) -> bool:
    s = line.strip()
    m = SPEAKER_RE.match(s)
    if not m:
        return False
    tok = m.group(1)
    if tok in ("日時", "場所", "出席者", "議題", "議事"):
        return False
    # 議題行（「○医薬品の薬価収載について」等）を除外＝肩書で終わるか事務局のみ発言者
    return tok.startswith("事務局") or any(tok.endswith(t) for t in ROLE_TAIL)


def parse_date(text: str, ctx: str) -> str:
    z = str.maketrans("０１２３４５６７８９", "0123456789")
    blob = (text[:800] + " " + (ctx or "")).translate(z)
    m = re.search(r"(令和|平成)(\d+|元)年(\d+)月(\d+)日", blob)
    if m:
        base = {"令和": 2018, "平成": 1988}[m.group(1)]
        y = 1 if m.group(2) == "元" else int(m.group(2))
        return f"{base + y}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
    m = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", blob)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def parse_meeting(rec: dict) -> list:
    lines = rec["text"].split("\n")
    date = parse_date(rec["text"], rec.get("ctx", ""))
    utts, cur_speaker, buf, seq = [], None, [], 0
    for line in lines:
        if is_speaker_line(line):
            if cur_speaker and buf:
                body = "\n".join(buf).strip()
                if len(body) >= 10:
                    seq += 1
                    name, role, kind = classify(cur_speaker)
                    utts.append((rec.get("kai"), date, name, role, kind, seq, len(body), body))
            cur_speaker = line.strip()[1:]
            buf = []
        elif cur_speaker is not None:
            buf.append(line)
    if cur_speaker and buf:
        body = "\n".join(buf).strip()
        if len(body) >= 10:
            seq += 1
            name, role, kind = classify(cur_speaker)
            utts.append((rec.get("kai"), date, name, role, kind, seq, len(body), body))
    return utts


def main():
    rebuild = "--rebuild" in sys.argv
    db = sqlite3.connect(DB)
    if rebuild:
        db.execute("DROP TABLE IF EXISTS chuikyo_utt")
        db.execute("DROP TABLE IF EXISTS chuikyo_utt_fts")
    db.execute("""CREATE TABLE IF NOT EXISTS chuikyo_utt (
        id INTEGER PRIMARY KEY, kai INTEGER, date TEXT, speaker TEXT, role TEXT,
        kind TEXT, seq INTEGER, chars INTEGER, body TEXT)""")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chuikyo_utt_fts USING fts5(body, content=chuikyo_utt, tokenize='trigram')")
    done_kai = {r[0] for r in db.execute("SELECT DISTINCT kai FROM chuikyo_utt")}
    added = meetings = 0
    for f in glob.glob(os.path.join(ARCHIVE, "*.json")):
        rec = json.load(open(f, encoding="utf-8"))
        if rec.get("kai") in done_kai and not rebuild:
            continue
        utts = parse_meeting(rec)
        if not utts:
            continue
        db.executemany(
            "INSERT INTO chuikyo_utt (kai,date,speaker,role,kind,seq,chars,body) VALUES (?,?,?,?,?,?,?,?)",
            utts)
        added += len(utts)
        meetings += 1
    if added:
        db.execute("INSERT INTO chuikyo_utt_fts(chuikyo_utt_fts) VALUES('rebuild')")
    db.commit()
    n, sp = db.execute("SELECT COUNT(*), COUNT(DISTINCT speaker) FROM chuikyo_utt").fetchone()
    print(f"✓ chuikyo_utt: 追加{added}発言/{meetings}会合（計{n:,}発言・話者{sp}人）")
    db.close()


if __name__ == "__main__":
    main()
