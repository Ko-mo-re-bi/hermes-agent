# Slack Phase1 観測Home — 独立指摘修正版

限定実装。実Slack・本番未適用、Phase1全体は未完了。
公開用の検証範囲と残件は `PHASE1_TEST_REPORT.md` を参照。
正規runner・既存承認回帰は NOT RUN / BLOCKED。ローカルの途中経過ログは本PRに含めない。

既存Socket receiver/clientのHomeイベントとページ操作だけを利用する。
Messages、承認handler、guard、認証・設定・サービス・実DBは変更しない。

```yaml
platforms:
  slack:
    extra:
      coordination_home:
        enabled: true
        boards:
          project-a: /absolute/path/to/existing/kanban.db
        grants:
          - team_id: T012345
            user_id: U012345
            board: project-a
            channels: [C012345]
        # 任意、schemaのみ確認。承認bindingは未接続。
        approval_db: /absolute/path/to/existing/business.sqlite
```

既定OFF。設定例のみで実設定は変更していない。grantはboard全体の閲覧許可。
team/user完全一致、登録board、当該teamの既存clientが必要。
ロック取得後とworker完了後のpublish直前に認可とclientを再確認する。
既に公開したHomeをgrant取消で消去する機能・運用は未確認。取消後の既存表示は残り得る。

20件/ページ。タイトル、本文、エラー詳細、成果物名、ローカルパス、board別名は送信しない。
IDはnativeの `t_` + 8桁hexだけ、状態は既知値だけ、担当は固定のhermes/muse/grokbotだけを
表示し、未知値は不明。固定担当名との一致は実エージェントの本人性や稼働の証明ではない。
Hermes/Muse/GrokBotの観測状態は未確認。fixture表示はライブ観測の証拠ではない。

判断待ちはreview taskの区分であり、承認件数・内容を示さない。進行・待機と、ページ内の
添付区分（成果物か未確認）を表示。attachment IDを内部取得するが、公開origin・認証・board範囲未確認のため
URLを生成しない。「添付N件（成果物か未確認、アクセスリンク未接続）」とする。全boardの最新成果物一覧は未実装。
時刻はtask_events.created_atの最大値を最終イベントとして表示し、なければ完了・開始・作成日時の順で
根拠を表示。JST表記。heartbeatはオンライン保証と区別する。
threadは許可channelのみ、team/channel/timestampを検証した固定client URLのbutton.urlを使う。
thread timestampの小数点を維持する。実Slackでのリンク動作は未確認。

読取は接続からcloseまでasyncio.to_thread内で実行。閲覧者ごとに直列化し、実行中1件と
待機1件まで、それ以上のイベントはまとめて破棄する。処理順にpublishし、古い結果の後勝ちを防ぐ。
429のRetry-Afterはteam単位で抑制し、自動retryしない。

SQLiteは既存絶対パスへmode=ro、query_only、短い読取transaction。native connect、migration、
checkpoint、immutableは使用しない。WALヘッダのDBでは既存-wal/-shmを事前確認し、
不足時は接続せず未接続。既存WALのread-markやロックの変化は許容する。
これはDB論理更新・main DB bytes変更とは異なる。全sidecarのbytes/mtime不変は保証しない。
事前確認と接続の間にsidecarが外部削除される競合は原子的に防げず、SQLiteが再作成し得る。
sidecar新規作成を厳密に禁止する運用では、OSのディレクトリ書込禁止等を別途確認する必要がある。
本修正では権限を変更しない。journal等による読取失敗も未接続とする。

rollbackはcoordination_home.enabledをfalseにして以後の公開を停止し、必要なら今回の
adapter追加箇所と新規ファイルを戻す。DB削除・復元・外部作業再送は不要。
設定OFFだけでは公開済みHomeを消せない。実際の設定変更・rollback試験はNOT RUN。

読取はadapter内の全viewer共通で最大4 worker。同viewerは1 active＋最大1 pending。
キャンセル時も実thread終了までviewer/global枠を保持し、取消要求からはpublishしない。
URL button `hermes_observe_thread` はACKのみで、読取・更新・URL呼出をしない。
