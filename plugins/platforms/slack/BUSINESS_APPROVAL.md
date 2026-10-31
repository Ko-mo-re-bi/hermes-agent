# Slack業務承認（HD-20261028-066）

業務判断をSQLiteに保存する。実行権限は付与しない。
登録経路は丞相が管理するローカル発行原本とCLI。Slackイベントから登録しない。

## 発行原本と信頼境界

発行担当が、対象profileのOSユーザー所有の絶対パスディレクトリを用意する。
issuer-rootとDB親ディレクトリは既存の0700、原本とDBは0600が必須。
所有者は実行UIDで確認する。profile名は原本との完全一致で確認する。
同じOSユーザーを共有するprofile間をOS権限だけで隔離するものではない。
シンボリックリンク経由のパスは拒否する。
原本を生成・配置できるローカル発行担当が信頼の起点であり、電子署名方式ではない。
依頼本文をこのディレクトリへ自動コピーしてはならない。

原本ファイル名は `<hd>.md`。本文は次の全11項目を各1行、重複・追加行なしで記載する。
数値はUnix秒。以下はダミー値であり秘密鍵は使用しない。

```text
hd: HD-20990101-001
team: T_DUMMY
profile: example
channel: C0C84GJEC5R
message_ts: 100.1
thread_ts: 99.1
issued: 4070908800
expires: 4070995200
requester: dummy-requester
approver: U0C79V9C6N6
automatic_execution: none
```

`parse_issuer_document()`が専用書式を解析し、verifierが登録引数、
expires=issued+86400、固定承認者、automatic_execution=noneをすべて照合する。
原本欠落、読取不能、権限不備、重複項目、不一致は拒否。
自己申告trustedフラグは存在しない。Slack投稿者や本文は真正性の証拠にしない。

## 表示項目（任意）

原本には、承認者が判断材料として読む表示項目を任意で追加できる。
`title`（作業名）、`outcome`（承認後の結果）、`scope`（影響範囲）の3項目で、
各1行・最大200文字・改行なし。追加した項目は登録引数（`--title`等）と
完全一致しなければ登録できない。意味の解釈には使わず、DMとApp Homeの
先頭にそのまま表示するだけである。
項目がないカードは「（発行原本に記載なし）」と表示され、推測で補わない。
詳細は原本に紐づく正規の依頼スレッドへのリンクから確認する。
DMとApp Homeのボタン文言は「承認する」「差し戻す」で統一する。
発行担当がチャンネルへ掲載するカードのボタンも同じ文言にする。

## 登録と照会

発行担当が正規経路でカードを掲載した後、返却されたmessage_tsを原本に確定する。
次の実フラグで登録する。例の発行時刻はダミーなので実行時に有効な値へ置き換える。

```bash
python -m plugins.platforms.slack.business_approval register \
  --db /absolute/profile/business.sqlite --profile example \
  --issuer-root /absolute/profile/issuer \
  --hd HD-20990101-001 --team T_DUMMY --channel C0C84GJEC5R \
  --message-ts 100.1 --thread-ts 99.1 --issued 4070908800 --requester dummy-requester
python -m plugins.platforms.slack.business_approval pending --db /absolute/profile/business.sqlite --profile example
python -m plugins.platforms.slack.business_approval results --db /absolute/profile/business.sqlite --profile example
```

DB初期化はregister CLIのみ。原本照合後に0600で作成する。
Ledger.registerとSlack ingressはmode=rwで既存DBだけを開く。
同一profileのHD、およびteam/channel/message_tsは重複登録不可。
旧試作DBは流用せず新規専用DBを使用する。

Slack PlatformConfig.extraには次を指定する。

```yaml
business_approval_db: /absolute/profile/business.sqlite
business_approval_outbox: /absolute/profile/outbox
business_approval_profile: example
```

DB未設定なら無効。設定時は登録済みDBが必要。
承認者はU0C79V9C6N6、対象channelはC0C84GJEC5R。
ボタンおよび✅／❌は同一トランザクションで最初の決定に収束する。
発行前、発行から86400秒以降、actor/team/channel/profile/message不一致は拒否。
thread_tsは登録原本と照合し、操作時の対象はmessage_tsで固定する。
結果は再起動後もresultsで取得可能。pendingは期限内未決定のみ。
表示更新失敗でも決定は保存される。

## 期限切れの永続化

期限（expires）を過ぎた未決定カードは `decision='expired'`, `actor='system'`,
`decided=現在時刻` としてDBに残る。書込みは `decision IS NULL` の行だけを更新する1本のUPDATEで、
ボタン押下・リアクションとの競合でも最初の1件だけが残る。
実行契機はアダプター接続時と、その後60秒ごとの見回りタスク（切断時に停止）。
期限後に押された場合もその場で同じUPDATEを行い、結果はexpiredとなる（承認にはならない）。
expiredは`results`に現れ、`pending`からは外れる。

## 結果ファイル（outbox）

決定を先にDBへ確定し、その後 `<request_id>_Slack業務承認結果.md` を作る。
同一ディレクトリの一時ファイルに書いてfsyncし、`os.link` で本来の名前に置く（既存名は上書きしない）。
置けたら `outbox_written_at` に時刻を記録する。置けなければ空のままにし、60秒ごとの見回りで置き直す。
置き直しで同名ファイルがある場合は内容のSHA-256を比べ、同じなら成功、違えば上書きせずエラー。
3回連続で失敗したらエラーを1回だけ記録する（同じ失敗の繰り返し記録はしない）。
結果ファイルの内容に `outbox_written_at` は含めない（旧版の結果ファイルと一致させるため）。

`outbox_written_at` 列は既存DBに `ALTER TABLE ADD COLUMN` で追加される（register CLIの初期化時、
およびLedger接続時に未追加なら実行。既存行は変更されずNULLのまま）。旧版で結果ファイルを
作成済みの決定済み行は、見回りで内容が一致すれば書込み済みとして記録される。

## 検証状況・未解決点

指定コマンド:

```bash
HERMES_HOME=/home/hermes-svc/.hermes scripts/run_tests.sh tests/gateway/test_slack_business_approval.py tests/gateway/test_slack_approval_buttons.py tests/gateway/test_slack_plugin_action_handlers.py
```

このセッションでは環境再構築がuvキャッシュの読み取り専用制約で停止。
既存PM管理PythonをHERMES_PYTHONで指定してもrunnerの/var/tmpが読み取り専用で停止。
既存の/tmpラッパー（runner保存先だけを/tmpへ変更）での結果は控えに記録する。
正式コマンドでのPASSは未達成であり、書き込み可能な環境で再実行が必要。

実Slackでの対象メッセージ存在・ボタン・リアクション検証は未実施。
ローカル原本の発行・配置は運用担当の責任。work-relay配送・受領確認・定期再送は未接続。
Slack/Discord投稿、再起動、PR、push、コミットはこの作業では行わない。
