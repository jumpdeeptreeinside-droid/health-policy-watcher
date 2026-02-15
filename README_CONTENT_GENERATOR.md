# Notion監視型コンテンツ自動生成システム

Notionデータベースを監視して、コンテンツ生成を自動実行するシステム

## 📋 概要

`notion_content_generator.py` は、Notionデータベースを定期的に監視し、以下の処理を自動実行します：

### 自動処理フロー

```
1. Notionで「執筆待ち(PDF)」or「執筆待ち(URL)」にステータス変更
   ↓
2. スクリプトが自動検知（GitHub Actionsで定期実行）
   ↓
3. コンテンツ生成
   - 執筆待ち(PDF): PDFダウンロード → Gemini API実行
   - 執筆待ち(URL): Webスクレイピング → Gemini API実行
   ↓
4. 既存の出力先にファイル保存
   - output/blog/ にブログ記事(.md)
   - output/script/ にPodcast台本(.md)
   ↓
5. Notionステータスを「ファクトチェック待ち」に自動更新
   ↓
6. 手動でファクトチェック
   ↓
7. 手動で Status → 「完了」に変更
   ↓
8. 自動で Status(Web/Podcast) 更新（既存スクリプト）
```

## 🚀 機能

### 1. **PDF処理** (Status = 執筆待ち(PDF))
- NotionからPDFのURLを取得
- PDFを `~/Downloads/Auto_MHLW/` にダウンロード
- Gemini APIで分析
- ブログ記事とPodcast台本を生成
- `_Output/blog/` と `_Output/script/` に保存
- Statusを「ファクトチェック待ち」に更新

### 2. **URL処理** (Status = 執筆待ち(URL))
- NotionからURLを取得
- Webページをスクレイピング
- Gemini APIで分析
- ブログ記事とPodcast台本を生成
- `_Output/blog/` と `_Output/script/` に保存
- Statusを「ファクトチェック待ち」に更新

## 📁 出力ファイル

生成されたファイルは既存のワークフローと統一されています：

```
C:\Users\jumpd\OneDrive\Documents\Obsidian_Main\_Output\
├── blog\
│   └── 20260215_213045_介護保険部会議事録_blog.md
└── script\
    └── 20260215_213045_介護保険部会議事録_script.md
```

### ファイル命名規則

`{タイムスタンプ}_{タイトル}_{種別}.md`

- タイムスタンプ: `YYYYMMDD_HHMMSS`
- タイトル: 記事タイトルの一部（30文字まで）
- 種別: `blog` または `script`

### Frontmatter

生成されたMarkdownファイルには以下のFrontmatterが含まれます：

```yaml
---
title: "記事のタイトル"
date: 2026-02-15
source_url: https://example.com/source
---
```

## 🔧 使い方

### ローカルで手動実行

```bash
cd health-policy-watcher
.\venv\Scripts\python.exe src\notion_content_generator.py
```

### GitHub Actionsで自動実行（推奨）

1. `.github/workflows/content_generator.yml` をGitHubにプッシュ
2. GitHub Secretsが設定済みであることを確認
3. 完了！定期的に自動実行されます

**実行スケジュール**: 1日2回（JST 10:00, 22:00）

## ⚙️ 設定

### 必要な環境変数 / config.py設定

```python
NOTION_API_KEY = "your_notion_api_key"
NOTION_DATABASE_ID = "your_database_id"
GEMINI_API_KEY = "your_gemini_api_key"
GEMINI_MODEL_NAME = "gemini-1.5-flash"  # or "gemini-3-pro-preview"
BLOG_OUTPUT_DIR = r"C:\Users\...\Obsidian_Main\_Output\blog"
SCRIPT_OUTPUT_DIR = r"C:\Users\...\Obsidian_Main\_Output\script"
DOWNLOAD_DIR = r"C:\Users\...\Downloads\Auto_MHLW"
```

## 📊 実行例

```
2026-02-15 21:24:34 - INFO - ==================================================
2026-02-15 21:24:34 - INFO - Notion監視型コンテンツ自動生成を開始します
2026-02-15 21:24:34 - INFO - ==================================================
2026-02-15 21:24:34 - INFO - 
「執筆待ち(PDF)」のページを検索中...
2026-02-15 21:24:35 - INFO - 2 件のページが見つかりました

2026-02-15 21:24:35 - INFO - 処理中: 第132回社会保障審議会介護保険部会議事録...
2026-02-15 21:24:35 - INFO - PDF downloaded: Auto_MHLW\第132回社会保障審議会...pdf
2026-02-15 21:24:38 - INFO - PDFをアップロード中...
2026-02-15 21:24:40 - INFO - ブログ記事を生成中...
2026-02-15 21:24:55 - INFO - Podcast台本を生成中...
2026-02-15 21:25:10 - INFO -   ✅ コンテンツ生成成功
2026-02-15 21:25:10 - INFO -   保存先:
2026-02-15 21:25:10 - INFO -     - Blog: 20260215_213045_介護保険部会_blog.md
2026-02-15 21:25:10 - INFO -     - Script: 20260215_213045_介護保険部会_script.md
2026-02-15 21:25:11 - INFO -   ✅ ステータス更新: 執筆待ち(PDF) → ファクトチェック待ち

2026-02-15 21:25:11 - INFO - ==================================================
2026-02-15 21:25:11 - INFO - 処理完了
2026-02-15 21:25:11 - INFO - PDF処理: 2 件
2026-02-15 21:25:11 - INFO - URL処理: 1 件
2026-02-15 21:25:11 - INFO - ==================================================
```

## ❗ トラブルシューティング

### Geminiモデルが見つからないエラー

**エラー**: `404 models/gemini-xxx is not found`

**対策**: `config.py` の `GEMINI_MODEL_NAME` を確認
- 推奨: `"gemini-1.5-flash"` または `"gemini-1.5-pro"`
- 最新モデル名は [Google AI Studio](https://ai.google.dev/) で確認

### PDFダウンロードエラー

**原因**: URL(Source)が無効、またはPDFファイルでない

**対策**: Notionの URL(Source) プロパティを確認

### コンテンツ生成が遅い

**原因**: Gemini APIの処理時間（通常1-2分/ページ）

**対策**: 正常動作です。複数ページある場合は順次処理されます

### 「執筆待ち」ページが見つからない

**原因**: Notionで Status(コンテンツ作成) を手動変更していない

**対策**: Notionで対象ページのステータスを「執筆待ち(PDF)」または「執筆待ち(URL)」に変更

## 🔗 関連システム

このスクリプトは以下のワークフローに統合されます：

1. **ニュース収集** (`fetch_news_to_notion.py`) - 毎日JST 9:00
   - 情報源からNotionにURL自動登録

2. **コンテンツ生成** (`notion_content_generator.py`) ← このスクリプト - 1日2回
   - 「執筆待ち」を検知してコンテンツ自動生成

3. **ステータス更新** (`notion_status_automation.py`) - 毎時0分
   - 「完了」→「投稿待ち」等のステータス自動更新

4. **WordPress投稿** (今後実装)
   - 「スケジュール待ち」を検知してWordPress自動投稿

## 📝 既存ワークフローとの統合

このスクリプトは、既存の手動実行フローを自動化したものです：

### 以前（手動）

```bash
# 1. PDFダウンロード
python crawl_mhlw.py

# 2. コンテンツ生成
python analyze_pdf.py

# 3. WordPress投稿
cd _Output/blog
python upload_to_wordpress_with_config.py
```

### 現在（自動）

```
1. Notionでステータス変更のみ
   ↓
2. 全自動実行（GitHub Actions）
```

## ✅ チェックリスト

スクリプトが正常に動作することを確認：

- [ ] ローカルで手動実行が成功する
- [ ] 生成されたMarkdownファイルが `_Output/blog/` と `_Output/script/` に保存される
- [ ] Notionのステータスが「ファクトチェック待ち」に更新される
- [ ] Frontmatterが正しく含まれている
- [ ] GitHub Actionsでの実行が成功する

すべてチェックできたら、完全自動化の達成です！🎉
