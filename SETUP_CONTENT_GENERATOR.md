# コンテンツ自動生成 - GitHub Actions セットアップ手順

## 概要

このガイドでは、Notionのステータスを監視してブログ記事とPodcast台本を自動生成し、
Notionに保存するシステムをGitHub Actionsで動かす手順を説明します。

**完成後の動作:**
- 毎日 JST 9:00 に自動実行（PCの電源状態に関係なく）
- Notionで「執筆待ち(PDF)」「執筆待ち(URL)」に設定されたページを検出
- Gemini AIでブログ記事・Podcast台本を生成
- 元ページの子ページとして保存
- ステータスを「ファクトチェック待ち」に自動更新

---

## ステップ 1: Notionデータベースの準備

`HealthPolicyWatcherDB` に以下のプロパティが必要です。
まだ存在しない場合は、Notion上で手動追加してください。

| プロパティ名 | 型 | 用途 |
|---|---|---|
| `Status(コンテンツ作成)` | ステータス | 実行トリガー |
| `URL(Source)` | URL | 処理対象のURL |
| `Title` | タイトル | ページタイトル |
| `Article(Web)` | URL または リッチテキスト | ブログ記事子ページへのリンク |
| `Script(Podcast)` | URL または リッチテキスト | Podcast台本子ページへのリンク |

**ステータスの種類（必須）:**
- `執筆待ち(PDF)` — MHLWページからPDFをダウンロードして処理
- `執筆待ち(URL)` — Webページをスクレイピングして処理
- `ファクトチェック待ち` — 処理完了後に自動設定

---

## ステップ 2: GitHubリポジトリへのアップロード

以下のファイルをGitHubリポジトリにプッシュしてください。

**アップロードするファイル:**
```
.github/workflows/content_generator.yml   ← 新規（今回作成）
src/github_content_generator.py            ← 新規（今回作成）
requirements.txt                           ← 更新済み
```

**アップロードしてはいけないファイル:**
```
src/config.py   ← API キーが含まれているため絶対にアップロード禁止
```

### GitHub Desktop を使う場合

1. GitHub Desktop を開く
2. 左サイドバーで `health-policy-watcher` を選択
3. 変更されたファイルにチェックを入れる（`config.py` は外す）
4. コミットメッセージを入力して「Commit to master」
5. 「Push origin」でアップロード

---

## ステップ 3: GitHub Secrets の設定

GitHub Actions からAPIに接続するための認証情報を登録します。

1. GitHubリポジトリページを開く
2. 「Settings」タブ → 「Secrets and variables」→「Actions」
3. 「New repository secret」で以下を追加:

| Secret名              | 値                                                    |
| -------------------- | ---------------------------------------------------- |
| `NOTION_API_KEY`     | `ntn_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`（Notionインテグレーションのトークン） |
| `NOTION_DATABASE_ID` | `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`（NotionデータベースのID） |
| `GEMINI_API_KEY`     | `AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXX`（Google AI StudioのAPIキー） |
| `GEMINI_MODEL`       | `gemini-3-pro-preview`（ローカルと同品質にする場合）                |

> **品質について**: `GEMINI_MODEL` を設定しない場合は `gemini-2.0-flash` が使用されます。
> ローカル版と同じ品質にするには `gemini-3-pro-preview` を設定してください。

---

## ステップ 4: 手動実行テスト

1. GitHubリポジトリの「Actions」タブを開く
2. 左サイドバーの「コンテンツ自動生成 (Health Policy Watcher)」をクリック
3. 「Run workflow」→「Run workflow」をクリック
4. 実行が完了するまで待つ（最大60分、通常は数分〜十数分）

**結果の確認:**
- ✅ 緑のチェックマーク → 正常完了
- ❌ 赤いバツ印 → エラー発生（ログを確認）

---

## 動作の詳細

### 処理フロー

```
GitHub Actions 起動
  ↓
NotionDB を確認
  ├─ 執筆待ち(PDF) のページ
  │    └─ URL(Source) から MHLW ページをクロール
  │         → PDF を一時ダウンロード
  │         → Gemini にアップロードして分析
  │         → ブログ記事・台本を生成
  │         → 元ページの子ページとして Notion に保存
  │         → Article(Web) / Script(Podcast) にリンクを設定
  │         → ステータス = ファクトチェック待ち
  │
  └─ 執筆待ち(URL) のページ
       └─ URL(Source) をスクレイピング
            → Gemini で分析
            → ブログ記事・台本を生成
            → 元ページの子ページとして Notion に保存
            → Article(Web) / Script(Podcast) にリンクを設定
            → ステータス = ファクトチェック待ち
```

### Notion への保存イメージ

```
HealthPolicyWatcherDB
  └─ 📋 [元ページ] 厚生労働省・○○に関する会議（第X回）
       ├─ Status(コンテンツ作成): ファクトチェック待ち（自動更新）
       ├─ Article(Web): https://notion.so/... （自動更新）
       ├─ Script(Podcast): https://notion.so/... （自動更新）
       ├─ 📄 [子ページ] 厚生労働省、○○で新政策を発表へ  ← ブログ記事
       └─ 📄 [子ページ] [台本] 厚生労働省、○○で新政策を発表へ  ← Podcast台本
```

---

## Geminiモデルの変更

デフォルトは `gemini-2.0-flash` です。
変更したい場合は `.github/workflows/content_generator.yml` の以下の行を編集:

```yaml
# GEMINI_MODEL: gemini-1.5-pro  ← コメントを外して値を変更
```

利用可能なモデル例:
- `gemini-2.0-flash` — 高速・バランス型（デフォルト）
- `gemini-1.5-pro` — 高精度・低速
- `gemini-2.0-flash-thinking-exp` — 推論強化型

---

## 実行スケジュールの変更

`.github/workflows/content_generator.yml` の以下の行を編集:

```yaml
schedule:
  - cron: '0 0 * * *'   # UTC 0:00 = JST 9:00（毎日）
```

例:
- `0 6 * * *`   → JST 15:00 に実行
- `0 21 * * *`  → JST 6:00 に実行
- `0 0 * * 1`   → 毎週月曜 JST 9:00 に実行

---

## トラブルシューティング

### 「処理対象がなかった」と表示される

Notionの対象ページに以下が設定されているか確認:
- `Status(コンテンツ作成)` = `執筆待ち(PDF)` または `執筆待ち(URL)`
- `URL(Source)` にURLが入力されている

### 「Article(Web) の更新に失敗しました」

`Article(Web)` / `Script(Podcast)` プロパティの型が URL または リッチテキスト 以外の場合に起こります。
Notion上でプロパティの型を「URL」または「テキスト」に変更してください。

### Gemini API エラー

`GEMINI_API_KEY` が正しく設定されているか確認してください。
また、APIの利用制限に達している可能性があります（無料枠: 1日50リクエスト）。

### PDF ダウンロードエラー

MHLWのサーバーが一時的にダウンしているか、URLが正しくない可能性があります。
`URL(Source)` に設定しているURLをブラウザで確認してください。

---

## ファイル構成

```
health-policy-watcher/
├── .github/
│   └── workflows/
│       ├── content_generator.yml   ← 今回作成（コンテンツ生成ワークフロー）
│       ├── fetch_news.yml          ← 既存（ニュース収集ワークフロー）
│       └── notion_automation.yml   ← 既存（ステータス更新ワークフロー）
├── src/
│   ├── github_content_generator.py ← 今回作成（GitHub Actions用メインスクリプト）
│   ├── notion_content_generator.py ← 既存
│   └── config.py                   ← ローカル専用（GitHubにアップ禁止）
├── notion_launcher.py              ← ローカル実行用（変更なし）
├── run_content_generator.bat       ← ローカル実行用（変更なし）
└── requirements.txt                ← 更新済み
```
