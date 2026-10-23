# -*- coding: utf-8 -*-
"""メールの送り方（host・port・SSL・timeout）を**1か所**に置く。

🔴 2026-10-23 の実測：587（STARTTLS）は、このMacの既定の経路で塞がれていて
   `[Errno 60] Operation timed out` が 2026-09-21 から151回出ていた。
   ・587 … 8〜10秒で時間切れ（IPv4・IPv6ともに）／25 … 同じ／**465 … 0.05〜0.11秒でつながる**
   ・en0 を名指しして（トンネルを通らずに）叩くと587も通る＝塞いでいるのは回線ではなくトンネル側
   その10日間、通知メール（試聴依頼・公開完了）はほぼ届いていなかった。
   ログに「✉️ 通知メール送信」と出ていても、送れたことの証拠ではなかった（例外を飲んでいた）。

🔴 timeout を必ず渡す。無人ジョブは「落ちる」より「固まる」が質が悪い（launchd は次を起動しない）。
🔴 相手が本物かを確かめる。context を渡さないと `ssl._create_stdlib_context()` が使われ、
   実測で check_hostname=False・verify_mode=CERT_NONE＝**暗号化はされるが相手は検証されない**。
   鍵（アプリパスワード）を、SMTPを現に塞いでいるトンネル越しに渡すので、既定の context を明示する
   （ミハル 2026-10-23 の指摘。~/sakuma の送信4本はすでに context を明示している）。
🔴 ここに鍵の値も宛先も書かない。呼ぶ側が引数で渡す。
🔴 送り方を変えるときは**このファイルだけ**を直す（同じ判定を11か所に置かない）。
   そのために smtplib を import するのはこのファイルだけ、という決まりにする。
"""
import ssl
import smtplib

HOST = "smtp.gmail.com"
PORT = 465          # SMTP_SSL（465）。587・25 は塞がれていることがある＝上の注
TIMEOUT = 20        # 秒。超えたら例外で返る＝固まらない
BLOCKED_PORTS = (25, 587)


def send(msg, addr, pw, recipients=None, *, host=HOST, port=PORT, timeout=TIMEOUT, context=None):
    """送れたら True を返す。送れなければ**例外を投げる**（🔴 ここで飲まない）。

    recipients を渡したら sendmail（宛先を明示）、渡さなければ msg の To で send_message。
    🔴 host・port・timeout・context は**キーワードでしか渡せない**。位置で渡せるようにすると、
       `mailer.send(msg, a, p, to, h, 587)` のように書けてしまい、いま潰した障害が戻る
       （ミハル 2026-10-23 の指摘：既定引数の穴）。
    """
    if not timeout or timeout <= 0:
        raise ValueError("timeout を必ず正の秒数で渡してください（無人ジョブが固まります）")
    if port in BLOCKED_PORTS:
        raise ValueError(f"port {port} は塞がれていることがあります（{PORT} を使ってください）")
    with smtplib.SMTP_SSL(host, port, timeout=timeout,
                          context=context or ssl.create_default_context()) as srv:
        srv.login(addr, pw)
        if recipients is not None:
            # 🔴 空リストで黙って msg["To"] 経路に落ちないように、None かどうかで分ける
            srv.sendmail(addr, recipients, msg.as_string())
        else:
            srv.send_message(msg)
    return True
