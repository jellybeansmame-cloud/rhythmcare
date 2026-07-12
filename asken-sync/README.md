# あすけん自動同期（GitHub Actions）

PC を常時起動せず、毎日あすけんのデータをリズムケアに反映します。

## 流れ

```
GitHub Actions（毎日 1:00 JST）
  → あすけんから前日分を取得
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
| `ASKEN_EMAIL` | あすけんのログインメールアドレス |
| `ASKEN_PASSWORD` | あすけんのパスワード |

### 4. あすけんログイン情報の登録（初回）

**推奨: メールアドレス + パスワード**

1. PC のリズムケア → 設定 →「あすけんログイン（自動同期用）」に入力
2. 「ログイン情報を保存」
3. 同じ値を GitHub Secrets の `ASKEN_EMAIL` / `ASKEN_PASSWORD` にも登録

Google ログインのみの場合は `refresh-cookies.bat` を使います（予備）。

### 5. 動作確認

GitHub → Actions → **Asuken Sync** → **Run workflow**

成功後、スマホでリズムケアを開くと自動で反映されます。

## ログインエラー時

GitHub Actions が連続で失敗する場合:

1. PC で `refresh-cookies.bat` をダブルクリック（あすけんに Chrome でログインした状態で実行）
2. リズムケア設定 →「あすけんログイン」でパスワードを確認・再保存
3. GitHub Secrets の `ASKEN_EMAIL` / `ASKEN_PASSWORD` が最新か確認
4. Actions → **Asuken Sync** → **Run workflow** で手動再実行

失敗理由はリズムケア設定の「あすけん同期ステータス」にも表示されます。

## 過去データの一括取得

PC で日付範囲を指定して Firestore に送信します（1日あたり数ページを取得するため時間がかかります）。

```powershell
cd asken-sync
python sync_day.py --from 2026-01-01 --to 2026-06-07 --push
```

スマホでリズムケアを開くと `asken_inbox` から順に取り込まれます。  
取り込みが途中で止まった場合は「あすけん受信箱を今すぐ取り込む」を再度実行してください。

## 同期されるデータ（v2）

| あすけん | リズムケア |
|----------|------------|
| 食事メニュー・カロリー | ごはんログ（テキスト） |
| 1日の健康度・PFC（アドバイスページ） | ごはんログ先頭に表示 |
| 各食のPFC（食事別アドバイス `/3` `/4` `/5`） | ごはんログの各食セクション |
| 間食のPFC | 1日合計から朝昼夕を差し引いて算出 |
| 運動内容 | メモ（設定で変更可） |
| 運動あり | 運動チェック項目にチェック |
| 体重・お通じ・生理 | 各紐づけ項目 |

## ローカル開発用

`firebase_config.json` を作成（`firebase_config.json.example` 参照）:

```powershell
pip install -r requirements.txt
playwright install chromium
python sync_day.py --push
```

`--connect` は Cookie 取得時のみ使用します。
