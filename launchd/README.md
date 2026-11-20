# launchd の雛形（Mac から workflow_dispatch を送る・2026-11-20）

GitHub の schedule（cron）は実測で5〜10時間遅れて起動するため、もとの予定の時刻に Mac から
`gh workflow run` で起こす。cron は予備として残す（重なっても concurrency で順番待ち・二重公開しない）。

| plist | 送る workflow | 時刻（JST） |
|---|---|---|
| `com.crosshealth.dispatch-site-uploader.plist` | site_uploader.yml | 9:00 / 14:00 / 19:30 |
| `com.crosshealth.dispatch-daily-digest.plist` | daily_digest.yml | 19:05（送る前に状態進めを1回＝国際系を音声化待ちに） |

順番（夕方）：19:05 状態進め→まとめ作成の dispatch → 19:15 音声パイプライン（まとめを音声化）→ 19:30 サイト公開。

- 鍵はここにも plist にも置かない。gh CLI の既存ログインを使う（`env -i HOME=… PATH=…` で通ることを 2026-11-20 に確認）。
- gh には timeout 60秒。失敗しても他のジョブには触らない。失敗はログに `❌❌ DISPATCH FAILED` と残し、既存の ntfy（話題名は ~/sakuma/.env）に workflow 名と種類だけを1行（題・アカウント名は入れない）。ログは `~/health-policy-watcher/logs/dispatch_*.log`（CloudStorage は使わない）。
- 「音声化待ち記事のお知らせ」メールは廃止（2026-11-20、試聴依頼メールが代わり）。

## 配置の手順（監査と翔太さんの OK のあと）

```bash
cd ~/health-policy-watcher && git pull --ff-only
python3 src/mac_dispatch.py --self-test                 # 全件合格を確かめる
python3 src/mac_dispatch.py site_uploader --dry-run     # 送るはずのコマンドが出る（送らない）
python3 src/mac_dispatch.py daily_digest --dry-run
for n in dispatch-site-uploader dispatch-daily-digest; do
  sed "s#__HOME__#$HOME#g" launchd/com.crosshealth.$n.plist > ~/Library/LaunchAgents/com.crosshealth.$n.plist
  plutil -lint ~/Library/LaunchAgents/com.crosshealth.$n.plist
  launchctl load ~/Library/LaunchAgents/com.crosshealth.$n.plist
done
launchctl list | grep com.crosshealth.dispatch
```

最初の時刻を過ぎたら `logs/dispatch_*.log` の「✅ dispatch 送信」と、`gh run list --workflow site_uploader.yml -L 3` の event=workflow_dispatch を両方見る（出たかと届いたかは2段）。

止めるとき：`launchctl unload ~/Library/LaunchAgents/com.crosshealth.dispatch-<名前>.plist`（cron は残っているので遅れて動くだけ）。
