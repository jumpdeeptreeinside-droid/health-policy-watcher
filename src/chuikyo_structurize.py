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
# 会議体ごとのアーカイブ（総会＋専門部会）。増やす時はここに足す。
BODY_DIRS = {
    "総会": ARCHIVE,
    "薬価専門部会": os.path.expanduser("~/chuikyo_bukai_archive/yakka"),
}

# 発言者行のパターン。「○城山会長」「○清原薬剤管理官」「○事務局（宇都宮企画官）」等。
ROLE_TAIL = ("委員長代理", "会長代理", "専門委員", "部会長", "小委員長", "委員長", "前会長", "会長",
             "委員", "参考人", "構成員", "課長補佐", "企画官", "審議官", "管理官", "課長",
             "室長", "局長", "参事官", "技官", "薬剤管理官", "歯科医療管理官", "数理官")
SPEAKER_RE = re.compile(r"^○([^\s　○]{1,24})$")
# インライン形式（第189回等）: 「○遠藤前会長　発言文…」＝話者と本文が同一行
INLINE_RE = re.compile(r"^○([^\s　○]{1,16})[　](\S.*)$")

KIND_JIMU = ("課長", "企画官", "審議官", "管理官", "室長", "局長", "参事官", "技官", "補佐", "事務局")
# 議題宣言（会長の「「〜」を議題といたします」）＝発言を議題に紐付けるアンカー
AGENDA_RE = re.compile(r"「(.{2,60}?)」(?:について)?を議題と")


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
            # 「委員長」＝薬価算定組織・専門組織等の委員長（総会への報告者）＝参考人扱い。
            # 中医協内部の小委員長・部会長・会長は委員。
            kind = "事務局" if any(k in tail for k in KIND_JIMU) else (
                "参考人" if tail in ("参考人", "構成員", "委員長", "委員長代理") else "委員")
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
    agenda = [None]  # 現在の議題（クロージャで更新）

    def flush():
        nonlocal seq
        if cur_speaker and buf:
            body = "\n".join(buf).strip()
            if len(body) >= 10:
                seq += 1
                m = AGENDA_RE.findall(body)
                if m:
                    agenda[0] = m[-1][:80]
                name, role, kind = classify(cur_speaker)
                utts.append((rec.get("kai"), date, name, role, kind, seq, len(body),
                             agenda[0], body))

    def is_speaker_token(tok: str) -> bool:
        return tok.startswith("事務局") or any(tok.endswith(t) for t in ROLE_TAIL)

    for line in lines:
        s = line.strip()
        im = INLINE_RE.match(s)
        if is_speaker_line(line):
            flush()
            cur_speaker = s[1:]
            buf = []
        elif im and is_speaker_token(im.group(1)) and im.group(1) not in ("日時", "場所", "出席者", "議題", "議事"):
            flush()
            cur_speaker = im.group(1)
            buf = [im.group(2)]
        elif cur_speaker is not None:
            buf.append(line)
    flush()
    return utts


def main():
    rebuild = "--rebuild" in sys.argv
    db = sqlite3.connect(DB)
    if rebuild:
        db.execute("DROP TABLE IF EXISTS chuikyo_utt")
        db.execute("DROP TABLE IF EXISTS chuikyo_utt_fts")
    db.execute("""CREATE TABLE IF NOT EXISTS chuikyo_utt (
        id INTEGER PRIMARY KEY, mtg TEXT, kai INTEGER, date TEXT, speaker TEXT, role TEXT,
        kind TEXT, seq INTEGER, chars INTEGER, agenda TEXT, body TEXT)""")
    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chuikyo_utt_fts USING fts5(body, content=chuikyo_utt, tokenize='trigram')")
    done = {(r[0], r[1]) for r in db.execute("SELECT DISTINCT mtg, kai FROM chuikyo_utt")}
    added = meetings = 0
    for mtg, d in BODY_DIRS.items():
        for f in glob.glob(os.path.join(d, "*.json")):
            rec = json.load(open(f, encoding="utf-8"))
            if (mtg, rec.get("kai")) in done and not rebuild:
                continue
            utts = parse_meeting(rec)
            if not utts:
                continue
            db.executemany(
                "INSERT INTO chuikyo_utt (mtg,kai,date,speaker,role,kind,seq,chars,agenda,body) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(mtg,) + u for u in utts])
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
