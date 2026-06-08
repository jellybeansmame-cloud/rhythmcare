# あすけん自動同期（GitHub Actions）

PC を常時起動せず、毎日あすけんのデータをリズムケアに反映します。

## 流れ

```
GitHub Actions（毎日 22:00 JST）
  → Firestore asken_config/cookies を読み込み
  → あすけんから1日分を取得
  → Firestore asken_inbox/{日付} へ送信
  → asken_config/status に結果を記録

スマホ リズムケア（起動・画面復帰時）
  → asken_inbox を自動取り込み
  → 記録に反映
```

## 初回セットアップ

### 1. Firebase サービスアカウント

1. Firebase コンソール → プロジェクト設定 → サービスアカウント
2. 「新しい秘密鍵の生成」で JSON をダウンロード
3. リズムケア設定画面の **ユーザーID** を控える

### 2. Firestore セキュリティルール

リポジトリ直下の `firestore.rules.example` を参考に、以下を許可してください。

- `users/{uid}/asken_inbox` … 読み取り・削除（ユーザー）
- `users/{uid}/asken_config` … 読み書き（ユーザー）

### 3. GitHub Secrets

リポジトリ → Settings → Secrets and variables → Actions

| Secret | 内容 |
|--------|------|
| `FIREBASE_UID` | リズムケア設定に表示されるユーザーID |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | サービスアカウント JSON の**全文**（1行で貼り付け） |

### 4. あすけん Cookie の登録（初回・月1回程度）

**かんたん（推奨）:** `asken-sync\refresh-cookies.bat` をダブルクリック

1. Chrome が開く → あすけんに Google ログイン
2. 黒い画面に戻って Enter
3. 完了

PC のリズムケア設定画面にも同じ手順が表示されます。

事前に `firebase_config.json` と `serviceAccountKey.json` を `asken-sync` フォルダに置いてください。

### 5. 動作確認

GitHub → Actions → **Asuken Sync** → **Run workflow**

成功後、スマホでリズムケアを開くと自動で反映されます。

## Cookie 期限切れ時

- スマホ: 画面上部に警告バナーが表示されます
- PC: リズムケア設定の手順に従い、`refresh-cookies.bat` をダブルクリック
- 翌日 22:00 の自動同期、または Actions の手動実行で復旧します

## ローカル開発用

`firebase_config.json` を作成（`firebase_config.json.example` 参照）:

```powershell
pip install -r requirements.txt
playwright install chromium
python sync_day.py --push
```

`--connect` は Cookie 取得時のみ使用します。
