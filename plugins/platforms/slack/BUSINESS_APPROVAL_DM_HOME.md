# Slack承認（DM＋App Home）運用と本番切替

- rules_version: v9
- date: 2026-10-31
- GitHub baseline: `business_approval.py` blob `6a5ed76e2cb56f96cb7a6fa892d05a5d74a688cd`

## 機能

既存の `business.sqlite` の `cards` を唯一の承認決定正本とする。
ローカル発行原本で登録した案件のみ、既存Slackチャンネル上のカードを正本として保持し、同じ依頼を承認者へDM通知する。
DMカードの「承認」「差戻し」は、Slack署名検証済みのBoltイベント・本人ID・team・DMのメッセージ時刻・既存DBの一意なrequest_idを照合してから、既存`Ledger.decide`へ委譲する。
App Homeには同一DBから取得した未処理一覧を20件ずつ表示し、期限が近い順にページ送りできる。処理後は一覧から消え、DMと元カードを更新する。
DM送信の同時二重起動を防ぐリース、失敗時60秒以降の再試行、同一依頼のclient_msg_id再利用、結果のDM更新が途中で失敗したときの再起動後再照合を備える。

**業務処理やコマンドの自動実行は不可**。判断（approve/deny）が返るだけ。既存のexec承認権限は変更しない。
DM通知により承認者に権限を追加付与することはない。

## GitHubでの反映・検証

`git apply --check Slack_Approval_DM_Home_20261031.patch`
`git apply Slack_Approval_DM_Home_20261031.patch`

推奨： `scripts/run_tests.sh tests/gateway/test_slack_business_approval_inbox.py tests/gateway/test_slack_business_approval.py tests/gateway/test_slack_business_approval_receiver.py`

この成果物のローカル検証は、`main`の原本blobに対してパッチを適用して実施する。
ローカルのpytest成功だけで正式CI/Slack E2Eの通過とは扱わない。PRで全回帰と静的検査を確認すること。

## 有効化（本番の操作は未実施）

1. Phase 0の通知ループなどを停止・再発防止済みと確認し、GitHubで正式な回帰検査合格を確認。
2. SlackアプリのBotに `chat:write` / `im:write` とApp Home Tabを付与し、Homeの`app_home_opened`イベントを確認。必要なときのみアプリ再インストール。
3. 保存済み本番候補DB `/home/hermes-svc/.hermes/profiles/koumei/business_approval/db/business.sqlite` の権限0600、親0700、schema・backup・0行の正本を再検証する。実案件でなければ承認カードを登録しない。
4. `koumei`の実行設定`platforms.slack.extra`の既存3キーに加えて、次の2キーを明示的に有効にする：

```yaml
business_approval_db: /home/hermes-svc/.hermes/profiles/koumei/business_approval/db/business.sqlite
business_approval_outbox: /home/hermes-svc/.hermes/profiles/koumei/business_approval/outbox
business_approval_profile: koumei
business_approval_dm_enabled: true
business_approval_home_enabled: true
```

5. configバックアップ・差分確認の後、gatewayをメンテナンス窓で安全に再起動して稼働SHA/設定を読戻す。
6. **正式な実案件**1件で、発行原本→元チャンネルカード→DM受信→App Home表示→承認者がDMボタンを押す→SQLite記録→レシート→元カード/DM更新→Home消込みまで実機で検収する。受領側のclaim/ACKは別の明示操作と区別する。

## 境界・残リスク

- クラッシュで「Slackに送信成功、SQLiteの送信済み記録前」に停止した場合、`client_msg_id`を固定して再試行するが、SlackのAPI側で完全exactly-onceは保証されない。通知が二重表示されても、決定は既存SQLiteの原子的ガードにより1件に収束する。
- DMを閉じて通知をブロックする等、Slack側の状態によりDMが届かないことがある。エラーログを監視し、未送信に戻して後続tickで再試行する。
- 元の承認チャンネルは既存`CHANNEL`定数で固定される。任意のユーザーやチャンネルから発行権限を取得することはない。
- Slack App Homeはアプリ単位で一画面。PR #2の旧Home画面を別途本番導入する場合は統合設計が必要。双方がviews.publishを上書きしないようにする。
- Slack DMの既読・業務処理済みは、承認操作と別の事実。レシートのACKは必ず証拠参照付きで扱う。
- セキュリティ境界はSlack署名検証と本人Botイベント、SQLiteの所有権・発行原本検証が前提。秘密値をログ出力しない。

## ロールバック

1. `business_approval_dm_enabled: false` / `business_approval_home_enabled: false`に戻す。
2. gatewayをメンテナンス窓で再起動してイベントを止める（既存承認DB・レシートは保全）。
3. App Homeの既掲載ビューは構成停止だけでは消えない場合があるため、必要に応じてSlack上で権限を確認して表示を差し替える。
4. 送信済みDM、DBの通知テーブルを勝手に削除・再作成しない。元の承認決定は維持する。
