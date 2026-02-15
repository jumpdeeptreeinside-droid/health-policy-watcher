# 医療政策ニュース自動収集システム

複数の情報源から医療政策関連ニュースを自動収集し、Notionデータベースに登録するシステム

## 概要

`fetch_news_to_notion.py` は、以下の情報源から最新ニュースを取得してNotionに自動登録します：

- ✅ **厚生労働省 (MHLW)**: RSS配信
- ✅ **日本医療政策機構 (HGPI)**: Webスクレイピング
- ⚠️ **WHO**: 実装済み（現在取得失敗）

## 主な機能

1. **自動ニュース収集**: 各情報源から最新ニュースのタイトルとURLを取得
2. **重複チェック**: URL(Source)プロパティを使用して既存記事をスキップ
3. **自動Notion登録**: 以下のプロパティを自動設定
   - `Title`: 記事タイトル
   - `URL(Source)`: 記事URL
   - `Date(Search)`: 収集日時
4. **エラーハンドリング**: 一部の情報源が失敗してもスクリプトは継続

## 環境構築

### 1. 必要なライブラリのインストール

```bash
# プロジェクトルートディレクトリで実行
cd health-policy-watcher
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 設定ファイルの確認

`src/config.py` に以下の設定が必要です：

```python
# Notion API 設定
NOTION_API_KEY = "your_notion_api_key_here"
NOTION_DATABASE_ID = "your_database_id_here"
```

## 使い方

### 手動実行

```bash
cd health-policy-watcher
.\venv\Scripts\python.exe src\fetch_news_to_notion.py
```

### 定期実行（推奨）

#### Windows タスクスケジューラで設定

1. タスクスケジューラを開く
2. 「基本タスクの作成」を選択
3. トリガー: 毎日または毎週
4. 操作: プログラムの開始
   - プログラム: `C:\Users\jumpd\OneDrive\Documents\Obsidian_Main\health-policy-watcher\venv\Scripts\python.exe`
   - 引数: `src\fetch_news_to_notion.py`
   - 開始: `C:\Users\jumpd\OneDrive\Documents\Obsidian_Main\health-policy-watcher`

#### または GitHub Actions で設定

`.github/workflows/fetch_news.yml` を作成して定期実行を設定（要実装）

## ログファイル

実行結果は以下のファイルに記録されます：

- `fetch_news.log`: 実行ログ（成功/失敗/スキップ情報）

## 実行例

```
2026-02-15 20:22:22 - INFO - ==================================================
2026-02-15 20:22:22 - INFO - ニュース収集を開始します
2026-02-15 20:22:22 - INFO - ==================================================
2026-02-15 20:22:22 - INFO - 厚生労働省 RSS を取得中...
2026-02-15 20:22:22 - INFO - ✅ 厚生労働省: 20 件の記事を取得
2026-02-15 20:22:23 - INFO - 日本医療政策機構 (HGPI) ニュースを取得中...
2026-02-15 20:22:26 - INFO - ✅ HGPI: 12 件の記事を取得
2026-02-15 20:22:27 - INFO - WHO ニュースを取得中...
2026-02-15 20:22:27 - INFO - ✅ WHO (スクレイピング): 0 件の記事を取得
2026-02-15 20:22:27 - INFO - ==================================================
2026-02-15 20:22:27 - INFO - 合計 32 件の記事を収集しました
2026-02-15 20:22:27 - INFO - ==================================================
...
2026-02-15 20:22:45 - INFO - アップロード完了
2026-02-15 20:22:45 - INFO - 成功: 12 件 / スキップ: 20 件 / 失敗: 0 件
2026-02-15 20:22:45 - INFO - ✅ すべての処理が完了しました！
```

## トラブルシューティング

### WHO から記事が取得できない

**原因**: WHOのサイト構造が変更された、またはRSSフィードURLが無効

**対策**:
1. WHOの公式RSSフィードURLを再調査
2. 当面はMHLWとHGPIのみで運用
3. WHO情報は手動でNotionに追加

### 重複チェックエラーが発生する

**エラー**: `重複チェックエラー: 'DatabasesEndpoint' object has no attribute 'query'`

**対策**: 実装済み（直接REST APIを使用する方式に修正済み）

### タイムアウトエラーが発生する

**原因**: ネットワーク接続が遅い、または情報源のサーバーが応答しない

**対策**: 
- `timeout=30` の値を増やす
- しばらく時間をおいて再実行

## カスタマイズ

### 取得件数を変更する

`src/fetch_news_to_notion.py` の `main()` 関数内：

```python
# デフォルト: 各情報源から20件
articles = collector.collect_all(limit_per_source=20)

# 例: 50件に変更
articles = collector.collect_all(limit_per_source=50)
```

### 情報源を追加する

1. `NewsCollector` クラスに新しい `fetch_xxx_news()` メソッドを追加
2. `collect_all()` メソッド内で新しい取得関数を呼び出す

## 関連ファイル

- `src/fetch_news_to_notion.py`: メインスクリプト
- `src/config.py`: 設定ファイル（API キーなど）
- `requirements.txt`: 必要なライブラリリスト
- `fetch_news.log`: 実行ログ

## 次のステップ

1. WHO情報源の改善
2. 定期実行の自動化設定
3. エラー通知機能の追加（メール/Slack）
4. 統計情報のダッシュボード作成
