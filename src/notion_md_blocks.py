#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""要約の .md を Notion の行に載せる形へ割り、Mac 側で元に戻す（2026-10-21・オト）。

なぜ必要か:
  中医協ウォッチの要約は、いまメール添付でしか手元に来ない（人が見落とすと連載が止まる）。
  Notion の行に本文を書き戻せば Mac が自分で受け取れる。GitHub の artifact と
  リポジトリ置きは **repo が PUBLIC なので有料の中身が外から読める** ため採らない。

守っている決まり:
  🔴 **割る側（Actions）と戻す側（Mac）で同じ判定を2回書かない**＝この1本を両方が import する。
  🔴 ブロックの中身は、**先頭と末尾に空白・改行を置かない**。
     区切り（改行いくつでつながるか）は本文でなく `seps` に持つ。
     こうすると Notion が端の空白を落としても、戻した文は変わらない。
  🔴 戻せたことは思い込みでなく **sha256（正規形）** で見る。合わなければ組まない。

正規形（canon）:
  改行を \\n に／各行の**末尾**の空白を落とす／先頭と末尾の空行を落とす。
  行の**先頭**の空白は意味があるので残す（markdown の字下げ）。
  🔴 末尾の空白を落とすのは、Notion を通すと消える恐れがあるため。
     ここで落としておけば「消えたのに気づかない」ではなく「最初から無い」になる。

使い方:
  python3 notion_md_blocks.py --self-test              # 検査（本物の中身は使わない）
  python3 notion_md_blocks.py --round-trip <md のパス>  # 実物1本で往復を測る
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys

# Notion の rich_text は1つ2,000字まで。余裕を取る。
# 🔴 2,000 が UTF-16 の数え方（絵文字が2つ分）である場合に備えて 1800 にしている
LIMIT = 1800


def canon(s: str) -> str:
    """正規形。両側（割る側・戻す側）が必ずこれを通す。"""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    return s.strip()


def sha(s: str) -> str:
    """正規形の sha256。これが Notion のプロパティに入る値。"""
    return hashlib.sha256(canon(s).encode("utf-8")).hexdigest()


def _pack(units: list[str], sep: str) -> list[list[str]]:
    """units を sep でつなぎながら LIMIT を超えない塊にまとめる。"""
    out: list[list[str]] = []
    cur: list[str] = []
    for u in units:
        trial = sep.join(cur + [u])
        if cur and len(trial) > LIMIT:
            out.append(cur)
            cur = [u]
        else:
            cur.append(u)
    if cur:
        out.append(cur)
    return out


def split_blocks(md: str) -> tuple[list[str], list[str]]:
    """(blocks, seps) を返す。canon(md) == blocks[0]+seps[0]+blocks[1]+… が必ず成り立つ。

    seps は len(blocks)-1 本。段落の境目なら "\\n\\n"、段落の中で割ったら "\\n"。
    """
    c = canon(md)
    if not c:
        return [], []
    blocks: list[str] = []
    seps: list[str] = []
    paras = c.split("\n\n")
    for pi, para in enumerate(paras):
        if pi:
            seps.append("\n\n")
        if len(para) <= LIMIT:
            blocks.append(para)
            continue
        # 長い段落は行で割る（段落の中なので区切りは改行1つ）
        groups = _pack(para.split("\n"), "\n")
        for gi, g in enumerate(groups):
            if gi:
                seps.append("\n")
            chunk = "\n".join(g)
            if len(chunk) <= LIMIT:
                blocks.append(chunk)
                continue
            # 🔴 改行の無い1行が LIMIT を超える形（行では割れない）。
            #    字で割り、区切りは "" にする（区切りは控えに持つので曖昧さは無い）。
            for ci in range(0, len(chunk), LIMIT):
                if ci:
                    seps.append("")
                blocks.append(chunk[ci:ci + LIMIT])
    blocks, seps = _move_edge_space(blocks, seps)
    return blocks, seps

def _move_edge_space(blocks: list[str], seps: list[str]) -> tuple[list[str], list[str]]:
    """ブロックの端の空白を区切りへ移す。

    🔴 端に空白を残すと、Notion が落としたときに戻した文が変わる（しかも静かに）。
       区切りへ移せば、ブロックは必ず空白以外で始まり終わる＝落ちるものが無くなる。
    """
    if not blocks:
        return blocks, seps
    bs = list(blocks)
    sp = list(seps)
    # 末尾側：ブロックの終わりの空白を、その後ろの区切りの先頭へ
    for i in range(len(bs)):
        m = re.search(r"\s+$", bs[i])
        if not m:
            continue
        tail = m.group(0)
        bs[i] = bs[i][: m.start()]
        if i < len(sp):
            sp[i] = tail + sp[i]
        else:
            # canon が文書の末尾の空白を落としているので起きない。起きたら黙らない
            raise ValueError("最後のブロックの末尾に空白が残りました（canon の決まりが崩れています）")
    # 先頭側：ブロックの始まりの空白を、その前の区切りの末尾へ
    for i in range(len(bs)):
        m = re.match(r"^\s+", bs[i])
        if not m:
            continue
        head = m.group(0)
        bs[i] = bs[i][m.end():]
        if i:
            sp[i - 1] = sp[i - 1] + head
        else:
            # 移す先の区切りが無い。canon が文書の先頭の空白を落としているので起きない
            raise ValueError("先頭のブロックの頭に空白が残りました（canon の決まりが崩れています）")
    # 空になったブロックは落とし、区切りをつなぎ直す
    out_b: list[str] = []
    out_s: list[str] = []
    pend = ""
    for i, b in enumerate(bs):
        s_after = sp[i] if i < len(sp) else ""
        if not b:
            pend += s_after
            continue
        if out_b:
            out_s.append(pend)
        elif pend:
            # 先頭に落ちた空白は canon では起きない。起きたら黙らずに落とす
            raise ValueError("先頭に空白が残りました（canon の決まりが崩れています）")
        out_b.append(b)
        pend = s_after
    return out_b, out_s


def join_blocks(blocks: list[str], seps: list[str]) -> str:
    """割る前の正規形に戻す。"""
    if not blocks:
        return ""
    if len(seps) != len(blocks) - 1:
        raise ValueError(f"区切りの数が合いません（blocks={len(blocks)} seps={len(seps)}）")
    out = [blocks[0]]
    for s, b in zip(seps, blocks[1:]):
        out.append(s)
        out.append(b)
    return "".join(out)


def manifest(md: str) -> dict:
    """Notion のプロパティに入れる1つの値（JSON）。本文は入れない。"""
    blocks, seps = split_blocks(md)
    return {"sha256": sha(md), "blocks": len(blocks), "seps": seps, "limit": LIMIT,
            "chars": len(canon(md)), "v": 1}


def restore(blocks: list[str], man: dict) -> str:
    """Notion から読んだブロックを戻し、指紋が合うことを確かめてから返す。

    合わなければ例外。**合わないまま原稿を組ませない**ための門。
    """
    if len(blocks) != man["blocks"]:
        raise ValueError(f"ブロックの数が違います（Notion={len(blocks)} 控え={man['blocks']}）")
    text = join_blocks(blocks, man["seps"])
    got = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if got != man["sha256"]:
        raise ValueError(f"指紋が合いません（戻した文={got[:16]}… 控え={man['sha256'][:16]}…）")
    return text


# ───────────────────────── 検査（本物の要約は使わない） ─────────────────────────
def _cases() -> list[tuple[str, str]]:
    """(名前, 中身)。中身は作り話。**実物の要約は入れない**（PUBLIC repo のため）。"""
    long_para = "あ" * (LIMIT * 2 + 7)
    many_lines = "\n".join(f"{i}行目のことばがここに入ります。" for i in range(1, 400))
    return [
        ("空", ""),
        ("1行だけ", "だいいちぎょう"),
        ("段落2つ", "だいいち\n\nだいに"),
        ("見出しと本文", "# みだし\n配信: 2026年1月1日　原文: https://example.invalid/a.html\n\n## がいよう\nほんぶん。"),
        ("空行の連続", "あ\n\n\n\nい"),
        ("末尾に空白のある行", "あ   \nい\t\n\nう"),
        ("CRLF", "あ\r\nい\r\n\r\nう"),
        ("先頭と末尾が空行・先頭に空白", "\n\n あ\n\nい\n\n\n"),
        ("末尾がタブ", "あ\n\nい\t\t"),
        ("字下げのある行", "1. ひとつめ\n   つづき\n2. ふたつめ"),
        ("ちょうど LIMIT の段落", "か" * LIMIT),
        ("LIMIT+1 の段落", "か" * (LIMIT + 1)),
        ("改行の無い長い段落", long_para),
        ("行がたくさん", many_lines),
        ("長い段落と短い段落が混ざる", long_para + "\n\nみじかい\n\n" + many_lines),
        ("絵文字と記号", "🔴 ① （イ）〜 ＜＞ 'か' \"く\"\n\n```\nコード\n```"),
    ]


def self_test() -> int:
    ok, ng = [], []

    def eq(name, got, want):
        (ok if got == want else ng).append((name, got, want))

    for name, text in _cases():
        c = canon(text)
        blocks, seps = split_blocks(text)
        # ① 往復で正規形に戻る
        eq(f"[{name}] 戻すと正規形と同じ", join_blocks(blocks, seps), c)
        # ② 1つも LIMIT を超えない
        eq(f"[{name}] どのブロックも{LIMIT}字以内", [len(b) for b in blocks if len(b) > LIMIT], [])
        # ③ 端に空白が無い
        bad = [b[:6] for b in blocks if b != b.strip()]
        eq(f"[{name}] ブロックの端に空白が無い", bad, [])
        # ④ 空のブロックが無い（Notion では意味のない行になる）
        eq(f"[{name}] 空のブロックが無い", sum(1 for b in blocks if not b), 0)
        # ⑤ 控え（manifest）経由でも戻る
        man = manifest(text)
        if blocks:
            eq(f"[{name}] 控えの指紋で戻せる", restore(blocks, man), c)
        eq(f"[{name}] 控えのブロック数が実際と同じ", man["blocks"], len(blocks))

    # ── 陽性対照：壊したら必ず落ちること（空振りしていないことの確認）
    base = "# みだし\n\n## がいよう\nほんぶん。\n\n## 議題①\nなかみ。"
    blocks, seps = split_blocks(base)
    man = manifest(base)

    def falls(name, fn):
        try:
            fn()
        except Exception:
            ok.append((name + "（落ちた＝対照が効いている）", "落ちた", "落ちた"))
            return
        ng.append((name, "落ちなかった", "落ちるはず"))

    falls("対照: 1字変えたら指紋で落ちる",
          lambda: restore([b.replace("ほんぶん", "ほんブン") for b in blocks], man))
    falls("対照: ブロックを1つ落としたら落ちる", lambda: restore(blocks[:-1], man))
    falls("対照: ブロックの順を入れ替えたら落ちる",
          lambda: restore(list(reversed(blocks)), man) if len(blocks) > 1 else restore([], man))
    falls("対照: 区切りの数が合わなければ落ちる",
          lambda: join_blocks(["あ", "い", "う"], ["\n\n"]))
    falls("対照: 控えのブロック数が違えば落ちる",
          lambda: restore(blocks, dict(man, blocks=man["blocks"] + 1)))
    # 区切りを "\n\n"→"\n" に変えると、戻した文が変わるので指紋で落ちる
    if man["seps"]:
        falls("対照: 区切りを1つ変えたら落ちる",
              lambda: restore(blocks, dict(man, seps=["\n"] + man["seps"][1:])))

    # ── 対照が本物の形を通っていることの確認（通る側も1つ見る）
    eq("通る側: 手を入れなければ戻る", restore(blocks, man), canon(base))

    print(f"  通 {len(ok)} ／ 落 {len(ng)}")
    for name, got, want in ng:
        print(f"  ✗ {name}  得た={got!r} 欲しい={want!r}")
    print("🟢 全部通りました" if not ng else "🔴 落ちた項目があります")
    return 1 if ng else 0


def round_trip(path: str) -> int:
    """実物1本で往復を測る。**中身は出さない**（字数と指紋だけ出す）。"""
    import io
    raw = io.open(path, encoding="utf-8").read()
    c = canon(raw)
    blocks, seps = split_blocks(raw)
    man = manifest(raw)
    back = restore(blocks, man)
    print(f"  読んだ: {len(raw):,}字（正規形 {len(c):,}字・行 {c.count(chr(10))+1}）")
    print(f"  割った: {len(blocks)}ブロック（最長 {max(len(b) for b in blocks):,}字"
          f"／{LIMIT}字を超えたもの {sum(1 for b in blocks if len(b) > LIMIT)}個）")
    print(f"  区切り: " + "・".join(f"{s!r}×{seps.count(s)}" for s in sorted(set(seps))))
    print(f"  指紋  : 元 {sha(raw)[:16]}… ／ 戻した {hashlib.sha256(back.encode()).hexdigest()[:16]}…")
    same = back == c
    print(f"  往復  : {'✅ 1字も違いません' if same else '🔴 違います'}")
    if not same:
        # 🔴 中身は出さない（PUBLIC repo では Actions のログも誰でも見られる）。
        #    どこで違うかは字の位置だけで足りる。
        for i, (a, b) in enumerate(zip(c, back)):
            if a != b:
                print(f"    最初に違う所: {i}字目（中身は出しません）")
                break
        else:
            print(f"    長さが違います（元 {len(c)}字 / 戻した {len(back)}字）")
        return 1
    # 正規形で落ちたもの（元の .md との違い）を数える＝黙って変えていないことを示す
    lost = sum(1 for a, b in zip(raw.split("\n"), c.split("\n")) if a != b)
    print(f"  正規形で落ちた行（末尾の空白のみ）: {lost}行")
    print(f"  Notion に入れる控え: {json.dumps({k: v for k, v in man.items() if k != 'seps'}, ensure_ascii=False)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--round-trip", metavar="MD")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.round_trip:
        return round_trip(a.round_trip)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
