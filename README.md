# 出退勤管理システム

Slackボットと連携した出退勤管理システムです。

## デプロイ状況

> **現在このアプリは稼働していません（2026-06-01 時点）。**
>
> - Render 上の Web サービス `arabesque-time`（`https://arabesque-time.onrender.com`）は **削除済み**。
> - これに伴い Slack の Event Subscriptions / OAuth の Request URL は無効になっています。
> - `DATABASE_URL` が指す外部データベースの実データは Render 削除では消えないため、別途管理が必要です。
> - シークレット類（`SLACK_BOT_TOKEN` / `SLACK_*` / `OPEN_AI` / `GITHUB_PAT` / `SECRET_KEY` など）は各サービス側でローテーション・失効していない限り有効なままです。
>
> 再稼働する場合は、ホスティング先へ再デプロイし、Slack App 側の Request URL / Redirect URL を新ドメインに更新してください。

## 機能

- Slackボットによる出退勤打刻
- Web画面での出退勤記録確認
- 管理者用の全ユーザー記録確認

## 使用方法

### ボットでの打刻

- 出勤: `出勤` または `おはよう`
- 退勤: `退勤` または `おつかれ`
- ヘルプ: `ヘルプ` または `help`

### Web画面

1. アプリにアクセス
2. 「Sign in with Slack」でログイン
3. 出退勤記録を確認

## 環境変数

```
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret
SLACK_CLIENT_ID=your-client-id
SLACK_CLIENT_SECRET=your-client-secret
DATABASE_URL=your-database-url
ADMIN_USER_ID=your-admin-slack-user-id
SECRET_KEY=your-secret-key-for-session
```

## トラブルシューティング

### 1. ボットがDMに応答しない場合

**問題**: ボットにメッセージを送っても応答がない

**解決方法**:

1. **Slack App の Event Subscriptions を確認**:
   - Slack App の管理画面で「Event Subscriptions」を有効化
   - Request URL を `https://your-domain.com/slack/events` に設定
   - 「Subscribe to bot events」で以下を追加:
     - `message.im` (Direct Message)
     - `message.channels` (チャンネル内メッセージ)
     - `message.groups` (プライベートチャンネル)

2. **Bot Token Scopes を確認**:
   - `chat:write` (メッセージ送信)
   - `users:read` (ユーザー情報読み取り)

3. **アプリをワークスペースに再インストール**:
   - 設定変更後は必ずアプリを再インストールしてください

### 2. 「Sign in with Slack」エラーの場合

**問題**: OAuth認証でエラーが発生する

**解決方法**:

1. **OAuth & Permissions の設定**:
   - Redirect URLs に `https://your-domain.com/callback` を追加
   - User Token Scopes に以下を追加:
     - `identity.basic` (基本情報のみ)

2. **Client ID と Client Secret の確認**:
   - 環境変数の値が正しいか確認
   - 特殊文字がエスケープされていないか確認

**注意**: `identity.email`スコープは無効です。`identity.basic`のみを使用してください。

### 3. 405 エラーの場合

**問題**: SlackからのPOSTリクエストが405エラー

**解決方法**:

1. **Request URL の確認**:
   - Slack App の Event Subscriptions で
   - Request URL を `https://your-domain.com/slack/events` に設定
   - URL の末尾に `/` がないことを確認

2. **アプリの再デプロイ**:
   - 設定変更後にアプリを再デプロイ

### 4. データベース接続エラーの場合

**問題**: データベースに接続できない

**解決方法**:

1. **DATABASE_URL の確認**:
   - 環境変数が正しく設定されているか確認
   - データベースが起動しているか確認

2. **データベースの初期化**:
   ```bash
   flask init-db
   ```

### 5. 環境変数の設定確認

以下のコマンドで環境変数を確認できます:

```bash
# 環境変数の確認（本番環境）
env | grep SLACK
env | grep DATABASE
```

### 6. ログの確認

Render等のプラットフォームでログを確認:

```bash
# Renderの場合
# Dashboard → Service → Logs
```

### 7. よくある問題と解決策

| 問題 | 原因 | 解決方法 |
|------|------|----------|
| ボット無応答 | Event Subscriptions未設定 | Slack App設定で有効化 |
| 405エラー | Request URL誤り | `/slack/events`に修正 |
| OAuth失敗 | Redirect URL未設定 | `/callback`エンドポイント追加 |
| DB接続失敗 | DATABASE_URL誤り | 環境変数確認 |

## デバッグ方法

1. **ボットへメッセージ送信**: `ヘルプ` と送信してボットの動作確認
2. **ログ確認**: アプリケーションログでエラーメッセージを確認
3. **Slack App設定**: Event Subscriptions と OAuth設定を再確認
