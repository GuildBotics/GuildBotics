# Scheduler / workflow 凍結テスト対応表

凍結点: `origin/python-host` / `fea9e05aaf8b4bca96e19d615b6811ea0eacb7d0`。
対象は24ファイル・339テスト関数（パラメータ展開前）。各関数を個別行にし、
その下に移植条件と設計上の差を記す。同じRustテストへ集約した場合も行は省略しない。

## 境界と条件の差

- HostはRust、チャット判断と応答agentは同じコマンドVMのPythonで実行する（#81）。
  判定規則・確率の境界値・モデル設定・プロンプト境界の検査は共有Pythonに残す。
  replayも同じ設定・gatewayを使い、実スレッドのeffortやチャットを変更しない。
- コマンドの開始・lease・終端記録は共通Executionが所有する。Python contextの
  close回数や暗黙のcurrent traceのmockは移植しない。Traceを引数で渡し、
  取消後のjournal・snapshot・leaseと、VMの回収を確かめる。
- providerへ届かなかった操作だけを自律実行で見送る。NotFound・Unsupported・
  通常失敗は区別したまま失敗する。thread historyを取得できない場合はcacheで
  代行せず、入力をpendingに保つ。旧実装の未到達時1試行消費は0にする。
- 受信のevent loop参照とthread cacheは、service世代が所有するNotify・task・
  connectionへ置換する。backfill完了前はcatching upを維持し、保存待ちもheartbeatを更新する。
  同一接続はlistenerとwakeを共有し、メンバーごとのqueue/cadenceは独立させる。
- 停止待ちのtimeout後に受付を再開する旧挙動は廃止する。停止完了までdrain gateを閉じる。
  定期コマンドの間隔は開始時刻からではなく完了時刻から測る。
- task run、対話セッション、diagnostic containerは#924、VM生命周期は#87、
  provider内部の判定は#88にある実装を呼ぶ。CLI経由のservice黒箱は#92、
  設定画面のbacklog resetは#91、本番の写しは#108の範囲。
- keep awakeの外部wakepy呼出はOSのholdへ置換する。macOSはcaffeinate、
  Linuxはsystemd-inhibit、Windowsは専用threadのSetThreadExecutionState。
  テストはOSを変更せずfactoryを使い、保持数・解放・失敗時1警告を観測する。

## 失敗を見て変更したテスト部品・入力

- 予算検査のRunner doubleは、Rejectedで試行を戻した値を保存する。最初の実装は
  増加した値を保存してからメモリだけ戻していた。本番Runtimeはadmission後に増加するため、
  doubleも同じ順にした。拒否時0、実行時1/2/3というassertは維持する。
- roster fixtureは実PublicMemberの必須profile/type等を保持して差分だけ置換する。
  handoff候補のinactive・役割なし・accountなし・selfの条件は維持する。
- diagnostic indexのsession pointerは実イベントとは別に除外し、共有Activityは
  保存形式のkindで選ぶ。これによって期待する件数を緩めず、3イベントそれぞれの
  command/source/trace/属性を比較する。
- routineの3分という固定値を10分へ置換し、120秒かかる仕事の終了からの間隔を比較する。
  開始基準へ戻すと、10分時点で実行されて同じ検査が落ちる。
- RNGとcroniterのmockは、Rustのsample入力と発火時刻を固定する。待機・一度だけ・
  cycle更新・clamp・曜日・表示全文という元の観測を維持する。
- パッケージ到達性の検査は、ファイルパスで動的ロードされる同梱コマンドのimportも
  VM入口として走査する。grimpの通常module graphだけでは、workflow内のlazy importが
  到達不能と誤判定されるため。共有→guestの禁止と未使用packageのcanaryは維持する。

## VM側で継続する検査

共有判定のテストは `tests/guildbotics/intelligences/test_decision_rules.py`、
`test_decision_engines.py`、`test_decision_assessment.py`。
VMの鮮度確認・receipt/completion失敗後の再判定は
`tests/guildbotics/guest/test_chat_judgment.py`、wireの失敗分類は
`tests/guildbotics/guest/test_chat_failure.py`。
判断1回・応答の完了再試行・effort・明示空入力は
`tests/guildbotics/templates/commands/workflows/test_chat_conversation_workflow.py`。
実member側の同一reaction receiptは共通control検査で重複しないことも確認する。

## 個別対応

### tests/guildbotics/drivers/test_service_settings.py

- `tests/guildbotics/drivers/test_service_settings.py::test_the_defaults_run_everything_every_ten_minutes` -> `crates/gb-server/src/service_settings.rs::defaults_run_every_source_every_ten_minutes_without_creating_a_file`

  条件: 全条件。数値2種の上限下限、全7種の破損ファイルを含む。

- `tests/guildbotics/drivers/test_service_settings.py::test_saved_settings_are_read_back_from_the_device_file` -> `crates/gb-server/src/service_settings.rs::settings_roundtrip_at_the_device_path_and_old_fields_keep_defaults`

  条件: 全条件。数値2種の上限下限、全7種の破損ファイルを含む。

- `tests/guildbotics/drivers/test_service_settings.py::test_a_file_written_before_the_other_settings_keeps_its_keep_awake` -> `crates/gb-server/src/service_settings.rs::settings_roundtrip_at_the_device_path_and_old_fields_keep_defaults`

  条件: 全条件。数値2種の上限下限、全7種の破損ファイルを含む。

- `tests/guildbotics/drivers/test_service_settings.py::test_nothing_to_run_cannot_be_saved` -> `crates/gb-server/src/service_settings.rs::source_and_numeric_boundaries_are_validated_before_any_write`

  条件: 全条件。数値2種の上限下限、全7種の破損ファイルを含む。

- `tests/guildbotics/drivers/test_service_settings.py::test_numbers_stay_within_what_the_screen_offers` -> `crates/gb-server/src/service_settings.rs::source_and_numeric_boundaries_are_validated_before_any_write`

  条件: 全条件。数値2種の上限下限、全7種の破損ファイルを含む。

- `tests/guildbotics/drivers/test_service_settings.py::test_a_file_that_is_not_settings_is_refused_by_name` -> `crates/gb-server/src/service_settings.rs::invalid_files_name_the_path_and_are_preserved`

  条件: 全条件。数値2種の上限下限、全7種の破損ファイルを含む。

### tests/guildbotics/drivers/test_service_lock.py

- `tests/guildbotics/drivers/test_service_lock.py::test_service_lock_is_exclusive_and_records_owner` -> `crates/gb-server/src/service.rs::force_escalates_a_graceful_stop_and_releases_machine_ownership`

  条件: 所有者のpid・32桁UUID・絶対workspace・時刻、競合所有者一致、graceful→force、別instance再獲得、lockfile残存を確認。

- `tests/guildbotics/drivers/test_service_lock.py::test_service_lock_release_keeps_file_but_makes_it_available` -> `crates/gb-server/src/service.rs::force_escalates_a_graceful_stop_and_releases_machine_ownership`

  条件: 所有者のpid・32桁UUID・絶対workspace・時刻、競合所有者一致、graceful→force、別instance再獲得、lockfile残存を確認。

- `tests/guildbotics/drivers/test_service_lock.py::test_service_lock_can_be_reacquired_by_another_owner` -> `crates/gb-server/src/service.rs::force_escalates_a_graceful_stop_and_releases_machine_ownership`

  条件: 所有者のpid・32桁UUID・絶対workspace・時刻、競合所有者一致、graceful→force、別instance再獲得、lockfile残存を確認。

- `tests/guildbotics/drivers/test_service_lock.py::test_service_lock_retries_one_transient_conflict` -> `crates/gb-server/src/service.rs::one_transient_lock_conflict_retries_but_io_failures_do_not`

  条件: 実ファイルlockの初回競合後に解放し2回目で獲得。権限エラーは1回で伝播。

- `tests/guildbotics/drivers/test_service_lock.py::test_windows_lock_backend_uses_one_byte_range` -> `移さない：Pythonのmsvcrt呼出の差し替え検査。Rustは既存のstd::fsのファイルロックを使い独自のWindowsロック層を持たない。`

  条件: OS別バックエンドの内部呼出回数・バイト範囲は移行対象外。外部の排他・再獲得はserviceの実ファイル検査。

- `tests/guildbotics/drivers/test_service_lock.py::test_windows_lock_conflict_becomes_blocking_error` -> `移さない：Pythonのmsvcrt呼出の差し替え検査。Rustは既存のstd::fsのファイルロックを使い独自のWindowsロック層を持たない。`

  条件: OS別バックエンドの内部呼出回数・バイト範囲は移行対象外。外部の排他・再獲得はserviceの実ファイル検査。

- `tests/guildbotics/drivers/test_service_lock.py::test_the_service_lets_the_machine_sleep_unless_set_otherwise` -> `crates/gb-server/src/keep_awake.rs::only_changed_readable_settings_change_a_hold_and_repeated_settings_keep_one_hold`

  条件: 既定false保持0、保存true保持1、同一signatureの破損canaryを読まない、変更破損中は保持1と未読フラグ、正常falseで解放、削除時既定false。警告文のcaptureは別検査。

- `tests/guildbotics/drivers/test_service_lock.py::test_the_service_keeps_the_machine_awake_while_held_when_set` -> `crates/gb-server/src/keep_awake.rs::independent_holds_start_synchronously_and_release_from_another_thread`

  条件: 起動がhold取得後に返る、独立2個→別スレッドでdropし1個→dropで0個。Dropはwatcherをjoinするため終了後の監視task無し。OSへの実抑止は行わず凍結wakepy fake同様のfactoryで確認。

- `tests/guildbotics/drivers/test_service_lock.py::test_a_held_service_follows_the_setting_however_it_is_written` -> `crates/gb-server/src/keep_awake.rs::only_changed_readable_settings_change_a_hold_and_repeated_settings_keep_one_hold`

  条件: 既定false保持0、保存true保持1、同一signatureの破損canaryを読まない、変更破損中は保持1と未読フラグ、正常falseで解放、削除時既定false。警告文のcaptureは別検査。

- `tests/guildbotics/drivers/test_service_lock.py::test_a_setting_half_written_by_hand_keeps_the_hold_until_it_reads` -> `crates/gb-server/src/keep_awake.rs::only_changed_readable_settings_change_a_hold_and_repeated_settings_keep_one_hold`

  条件: 既定false保持0、保存true保持1、同一signatureの破損canaryを読まない、変更破損中は保持1と未読フラグ、正常falseで解放、削除時既定false。警告文のcaptureは別検査。

- `tests/guildbotics/drivers/test_service_lock.py::test_a_held_service_reads_the_setting_only_when_it_changes` -> `crates/gb-server/src/keep_awake.rs::only_changed_readable_settings_change_a_hold_and_repeated_settings_keep_one_hold`

  条件: 既定false保持0、保存true保持1、同一signatureの破損canaryを読まない、変更破損中は保持1と未読フラグ、正常falseで解放、削除時既定false。警告文のcaptureは別検査。

- `tests/guildbotics/drivers/test_service_lock.py::test_a_released_service_no_longer_follows_the_setting` -> `crates/gb-server/src/keep_awake.rs::independent_holds_start_synchronously_and_release_from_another_thread`

  条件: 起動がhold取得後に返る、独立2個→別スレッドでdropし1個→dropで0個。Dropはwatcherをjoinするため終了後の監視task無し。OSへの実抑止は行わず凍結wakepy fake同様のfactoryで確認。

- `tests/guildbotics/drivers/test_service_lock.py::test_an_unreadable_setting_fails_the_start_without_holding_the_lock` -> `crates/gb-server/src/member/native.rs::scheduler_http_owns_native_workers_refuses_unauthorized_start_and_releases_the_lock_on_stop`

  条件: 破損設定を残したHTTP開始が400。開始後にmachine lockを獲得でき、停止状態維持。

### tests/guildbotics/drivers/test_task_scheduler.py

- `tests/guildbotics/drivers/test_task_scheduler.py::test_task_scheduler_runs_routine_at_configured_minute_interval` -> `crates/gb-server/src/scheduler.rs::scheduled_then_routines_round_robin_then_chat_and_no_schedule_runs_twice`

  条件: 設定された間隔でroutineをround robin実行、同時刻の定期commandは重複なし。メンバー直列化・本番接続はNative/HTTP検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_task_scheduler_measures_routine_interval_after_routine_finishes` -> `crates/gb-server/src/scheduler.rs::routine_interval_is_measured_after_the_command_finishes_and_chat_runs_between_patrol_checks`

  条件: paused clockでcommand120秒、開始から10分時点は未実行、完了から10分後に次routine。空heartbeat→person/last/nextを確認。凍結の3分を10分に置換しても開始基準へ戻す変異は同じく落ちる。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_routine_work_is_tracked_under_its_trace_id` -> `crates/gb-server/src/member/native.rs::native_commands_keep_trace_and_journal_in_sync_when_only_autonomous_service_outages_defer`

  条件: 4sourceを実Runtimeで通し、Driver実行中にactive workのID/person/sourceと同じrun IDを確認。終了後にjournalとcommand境界を検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_routine_ticket_patrol_selects_outside_any_trace_or_tracked_work` -> `crates/gb-server/src/member/native.rs::workflow_runtime_assembles_local_ports_and_idle_ticket_command_leaves_no_run`

  条件: 実組立Runtimeでidle選択時にtrace・run無し。手動HTTPの空output/trace IDとVM無しはscheduler HTTPでも検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_a_routine_is_deferred_while_the_environment_is_unavailable` -> `crates/gb-server/src/native_scheduler.rs::environment_deferral_warns_once_per_changed_reason_then_announces_recovery`

  条件: building/filesystemの両理由を2回ずつ観測し警告は理由ごと1回、回復1回。en/jaの案内キーと理由を確認。仕事が消費されないことはScheduler unavailable検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_a_scheduled_command_waits_for_the_environment_and_runs_once_it_is_ready` -> `crates/gb-server/src/scheduler.rs::unavailable_environment_consumes_neither_routine_nor_schedule_and_deferral_keeps_the_slot`

  条件: 環境不可ではcommandもeventsも0回、回復後に定期commandを実行。一時見送りの定期発火を消費せず次tickで再試行。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_pending_chat_is_deferred_while_the_environment_is_unavailable` -> `crates/gb-server/src/scheduler.rs::unavailable_environment_consumes_neither_routine_nor_schedule_and_deferral_keeps_the_slot`

  条件: 環境不可ではcommandもeventsも0回、回復後に定期commandを実行。一時見送りの定期発火を消費せず次tickで再試行。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_ticket_patrol_idle_leaves_no_trace_and_no_run_record` -> `crates/gb-server/src/member/native.rs::workflow_runtime_assembles_local_ports_and_idle_ticket_command_leaves_no_run`

  条件: 実組立Runtimeでidle選択時にtrace・run無し。手動HTTPの空output/trace IDとVM無しはscheduler HTTPでも検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_ticket_patrol_dispatches_as_tracked_work_under_a_titled_trace` -> `crates/gb-server/src/member/native.rs::native_ticket_patrol_moves_the_local_ticket_and_accepts_real_member_comment_and_completion`

  条件: 実行中active snapshot1件のID/person/source、開始runと完了runが同じ、target title/urlをjournalに保持、slot区別と完了後snapshot空。kindと会話種別の衝突はslot kindへ分離。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_failed_ticket_patrol_is_recorded_as_failed_and_counted` -> `crates/gb-server/src/member/native.rs::native_patrol_keeps_distinct_candidates_after_failure_and_lease_conflict_then_records_cancellation`

  条件: 2候補・同一slotでも異なるticket URL、失敗後も次候補実行、lease競合は記録なしで後から実行可、scheduled/routineの取消はcancelled記録・エラー加算なし。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_ticket_patrol_keeps_next_candidate_due_and_uses_distinct_identity` -> `crates/gb-server/src/member/native.rs::native_patrol_keeps_distinct_candidates_after_failure_and_lease_conflict_then_records_cancellation`

  条件: 2候補・同一slotでも異なるticket URL、失敗後も次候補実行、lease競合は記録なしで後から実行可、scheduled/routineの取消はcancelled記録・エラー加算なし。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_ticket_patrol_selection_failure_is_recorded_under_a_trace` -> `crates/gb-server/src/member/native.rs::native_failed_selection_closes_traces_without_runs_and_records_the_worker_error_limit`

  条件: candidates/refreshそれぞれ3回失敗、VM/runを作らず各失敗traceを閉じ、3回でworker failedを記録。世代ID・person・source・連続回数・上限も確認。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_routine_run_updates_member_routine_heartbeat` -> `crates/gb-server/src/scheduler.rs::routine_interval_is_measured_after_the_command_finishes_and_chat_runs_between_patrol_checks`

  条件: paused clockでcommand120秒、開始から10分時点は未実行、完了から10分後に次routine。空heartbeat→person/last/nextを確認。凍結の3分を10分に置換しても開始基準へ戻す変異は同じく落ちる。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_scheduled_work_is_tracked_under_its_trace_id` -> `crates/gb-server/src/member/native.rs::native_commands_keep_trace_and_journal_in_sync_when_only_autonomous_service_outages_defer`

  条件: 4sourceを実Runtimeで通し、Driver実行中にactive workのID/person/sourceと同じrun IDを確認。終了後にjournalとcommand境界を検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_failed_scheduler_command_is_recorded_as_failed` -> `crates/gb-server/src/member/native.rs::native_commands_keep_trace_and_journal_in_sync_when_only_autonomous_service_outages_defer`

  条件: manual/scheduled/routine/event_queue×4ServiceError。Unavailableだけ自律で見送り、型名とjournal/trace終端一致。counterの維持/成功リセット/上限はService outcomes検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_unavailable_scheduler_command_is_deferred_without_counting_an_error` -> `crates/gb-server/src/member/native.rs::native_commands_keep_trace_and_journal_in_sync_when_only_autonomous_service_outages_defer`

  条件: manual/scheduled/routine/event_queue×4ServiceError。Unavailableだけ自律で見送り、型名とjournal/trace終端一致。counterの維持/成功リセット/上限はService outcomes検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_deferred_scheduled_occurrence_stays_due_until_service_recovers` -> `crates/gb-server/src/scheduler.rs::unavailable_environment_consumes_neither_routine_nor_schedule_and_deferral_keeps_the_slot`

  条件: 環境不可と操作未到達でslotを消費しない。回復後の同じ定期command実行と重複なし、チャットを塞がない。journalのdeferred→succeededは共通実行検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_ticket_outage_keeps_the_worker_and_chat_alive_and_recovers` -> `crates/gb-server/src/member/native.rs::native_patrol_selection_and_refresh_outages_keep_chat_alive_and_recover_without_failed_runs`

  条件: candidates/refreshの両位相を4回Unavailableにしてchatを各回処理。chat実返信/完了は1回、解除後にticket実処理1回、failed runなし。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_force_stopped_scheduler_command_is_recorded_as_cancelled` -> `crates/gb-server/src/member/native.rs::native_patrol_keeps_distinct_candidates_after_failure_and_lease_conflict_then_records_cancellation`

  条件: 2候補・同一slotでも異なるticket URL、失敗後も次候補実行、lease競合は記録なしで後から実行可、scheduled/routineの取消はcancelled記録・エラー加算なし。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_process_pending_chat_delegates_to_dispatcher` -> `crates/gb-server/src/scheduler.rs::scheduled_then_routines_round_robin_then_chat_and_no_schedule_runs_twice`

  条件: 定期→routine round robin→chatの順序、定期を同一slotで重複実行しない。Native local journeyで実Dispatcherとの接続も検査。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_run_cancellable_allows_long_task_to_finish_on_graceful_stop` -> `crates/gb-server/src/service.rs::graceful_stop_finishes_current_work_and_prevents_the_next_cycle`

  条件: inflight中にgraceful stop、stopperは未完了でcancelせず、release後に正常終了、次cycle無し、worker0。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_run_cancellable_cancels_long_task_on_force_stop` -> `crates/gb-server/src/service.rs::force_escalates_a_graceful_stop_and_releases_machine_ownership`

  条件: graceful stop中にforceへ昇格し、作業release未発火のまま停止、全stopper同じ完了、lock再取得。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_update_consecutive_errors_ignores_failures_during_shutdown` -> `crates/gb-server/src/service.rs::graceful_stop_finishes_current_work_and_prevents_the_next_cycle`

  条件: inflight中にgraceful stop、stopperは未完了でcancelせず、release後に正常終了、次cycle無し、worker0。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_run_work_rejection_during_drain_mirrors_stop` -> `crates/gb-server/src/execution.rs::coordinator_seals_registration_until_graceful_drain_finishes_and_force_escalates_it`

  条件: drain中は新規登録不可、作業完了後のwaitだけが次generationへ移す。forceは同じgenerationを取り消す。

- `tests/guildbotics/drivers/test_task_scheduler.py::test_run_work_lease_conflict_skips_without_stopping_scheduler` -> `crates/gb-server/src/member/native.rs::native_patrol_keeps_distinct_candidates_after_failure_and_lease_conflict_then_records_cancellation`

  条件: 2候補・同一slotでも異なるticket URL、失敗後も次候補実行、lease競合は記録なしで後から実行可、scheduled/routineの取消はcancelled記録・エラー加算なし。

### tests/guildbotics/drivers/test_ticket_selector.py

- `tests/guildbotics/drivers/test_ticket_selector.py::test_run_moves_a_ready_ticket_and_hands_the_turn_its_run` -> `crates/gb-server/src/ticket_selector/tests.rs::ready_ticket_moves_once_and_turn_receives_trace_identity_and_completion_budget`

  条件: readyをin progressへ1回だけ移動、run/trace同一、workと予算、コメントなし。target属性と既にin progressのmove canaryは専用検査。Python context closeはVM終了へ置換。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_mention_work_keeps_the_assignee_and_lane_and_has_its_own_session` -> `crates/gb-server/src/ticket_selector/tests.rs::mention_preserves_lane_and_assignee_and_uses_a_distinct_conversation_kind`

  条件: new/ready/in progress/doneの4lane、他担当を維持、独立会話kind、回答後は再選択されない。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_run_hands_the_turn_the_default_budget` -> `crates/gb-server/src/ticket_selector/tests.rs::default_and_clamped_budgets_reach_the_turn_without_moving_in_progress_work`

  条件: 空/0/不正/負数/空白付き数の5条件。実turn payloadの値とtrace IDを比較。進行中taskではmoveを故意に失敗させるportを置き、動かさず成功することを検査。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_run_outside_a_trace_is_refused_before_the_ticket_moves` -> `移さない：RustのSelector.runはTraceの参照を必須とし、trace無しの呼出自体を型で禁止。`

  条件: 空trace IDのDispatcher入力も拒否。グローバルcurrent traceへの依存を廃止。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_failed_run_posts_a_safe_status_comment_and_raises` -> `crates/gb-server/src/ticket_selector/tests.rs::safe_failure_and_timeout_are_voiced_and_only_autonomous_failures_announce`

  条件: en/ja × unknown/timeout × voice有/空/失敗 × manual/routine。exact fallback・安全な失敗status・1コメントと通知・返信後の再開を確認。Nativeの実同梱helper呼出も検査。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_failed_pull_request_run_says_so_on_the_pull_request` -> `crates/gb-server/src/ticket_selector/tests.rs::failed_pull_request_runs_report_to_code_host_and_leave_the_ticket_untouched`

  条件: PRだけにfailed statusと通知、元ticketは無書込。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_failed_move_is_reported_like_a_failed_run` -> `crates/gb-server/src/ticket_selector/tests.rs::move_errors_preserve_the_ready_lane_and_only_unavailable_autonomous_work_defers`

  条件: manual/scheduled/routine×通常失敗/NotFound/Unsupported/Unavailableの12条件。レーンready維持、turn未実行、autonomous Unavailableだけコメント無し。他は失敗コメント、autonomousなら停止通知。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_an_unavailable_move_is_deferred_only_for_autonomous_work` -> `crates/gb-server/src/ticket_selector/tests.rs::move_errors_preserve_the_ready_lane_and_only_unavailable_autonomous_work_defers`

  条件: manual/scheduled/routine×通常失敗/NotFound/Unsupported/Unavailableの12条件。レーンready維持、turn未実行、autonomous Unavailableだけコメント無し。他は失敗コメント、autonomousなら停止通知。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_rate_limited_run_is_settled_with_a_status_comment_and_an_event` -> `crates/gb-server/src/ticket_selector/tests.rs::rate_limit_settles_without_worker_failure_or_work_stop_and_preserves_reset`

  条件: rate制限はworker失敗にせずstatusコメント・reset保持・Activity、停止通知なし。属性はNativeイベント検査も参照。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_rate_limit_is_recorded_even_if_the_comment_cannot_be_posted` -> `crates/gb-server/src/ticket_selector/tests.rs::failed_comment_delivery_keeps_rate_limit_evidence_and_an_unaddressed_failure_still_notifies`

  条件: コメント成功/失敗×通常/レート制限4条件。制限時は公開失敗でもactivityを残し成功返却、停止通知なし。URL無しの通常失敗は元itemへ通知する。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_run_next_runs_the_first_ticket_in_patrol_order` -> `crates/gb-server/src/ticket_selector/tests.rs::next_takes_only_the_first_refreshed_item_and_idle_selection_creates_no_run_or_trace`

  条件: 最初だけdispatch・移動、次候補ready維持。候補なしはVM/run/traceなし。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_run_next_without_work_runs_nothing` -> `crates/gb-server/src/ticket_selector/tests.rs::next_takes_only_the_first_refreshed_item_and_idle_selection_creates_no_run_or_trace`

  条件: 最初だけdispatch・移動、次候補ready維持。候補なしはVM/run/traceなし。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_candidates_are_the_pull_requests_and_then_the_board_and_refresh_by_kind` -> `crates/gb-server/src/ticket_selector/tests.rs::candidates_put_pull_requests_first_and_refresh_each_from_its_own_service`

  条件: PR→boardの候補順・provider別refresh、ticket/PR URLの割当て。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_pr_whose_checks_settled_resumes_the_awaited_work` -> `crates/gb-server/src/ticket_selector/tests.rs::resumed_checks_restore_work_summary_and_settle_only_when_not_awaited_again`

  条件: 凍結と同じ7ケース：checks/feedback、resume有無、done/asking/awaiting checks、同PR/別PR/両方の再待機。旧runなしsummary空・chat会話identity保持、evidenceとsettledコメント。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_ticket_trace_attributes_for_ticket_and_pull_request` -> `crates/gb-server/src/ticket_selector/tests.rs::task_trace_attributes_distinguish_ticket_and_pull_request_without_changing_the_work_kind`

  条件: ticket/PR両方のprovider・label・kind・title・url全5項目を辞書全体で比較。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_run_resumed_for_checks_ends_their_wait` -> `crates/gb-server/src/ticket_selector/tests.rs::resumed_checks_restore_work_summary_and_settle_only_when_not_awaited_again`

  条件: 凍結と同じ7ケース：checks/feedback、resume有無、done/asking/awaiting checks、同PR/別PR/両方の再待機。旧runなしsummary空・chat会話identity保持、evidenceとsettledコメント。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_run_that_waits_for_a_person_is_announced_once` -> `crates/gb-server/src/member/native.rs::native_completed_ticket_waits_notify_once_and_later_execution_failure_reports_failed`

  条件: routine/manual×done/asking/blocked×完了後失敗有無の12条件。実member完了、routine人待ちだけ1通知、後続失敗はfailed通知に置換、manualは通知0、失敗コメントが先に追加済。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_failed_run_is_announced_with_its_failure_comment` -> `crates/gb-server/src/member/native.rs::native_completed_ticket_waits_notify_once_and_later_execution_failure_reports_failed`

  条件: manual/routine × done/asking/blocked × 完了前失敗/完了後失敗/成功。done通知なし、待ち先なし通知なし、failedは対応したコメントURLを使用。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_timeout_posts_one_explanation_and_one_failed_notice` -> `crates/gb-server/src/ticket_selector/tests.rs::safe_failure_and_timeout_are_voiced_and_only_autonomous_failures_announce`

  条件: en/ja × unknown/timeout × voice有/空/失敗 × manual/routine。exact fallback・安全な失敗status・1コメントと通知・返信後の再開を確認。Nativeの実同梱helper呼出も検査。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_failure_comments_share_member_voice_and_fallback` -> `crates/gb-server/src/ticket_selector/tests.rs::safe_failure_and_timeout_are_voiced_and_only_autonomous_failures_announce`

  条件: en/ja × unknown/timeout × voice有/空/失敗 × manual/routine。exact fallback・安全な失敗status・1コメントと通知・返信後の再開を確認。Nativeの実同梱helper呼出も検査。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_failure_comment_without_an_address_is_announced_with_its_item` -> `crates/gb-server/src/ticket_selector/tests.rs::failed_comment_delivery_keeps_rate_limit_evidence_and_an_unaddressed_failure_still_notifies`

  条件: コメント成功/失敗×通常/レート制限4条件。制限時は公開失敗でもactivityを残し成功返却、停止通知なし。URL無しの通常失敗は元itemへ通知する。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_stop_that_waits_for_no_person_is_not_announced` -> `crates/gb-server/src/member/native.rs::native_completed_ticket_waits_notify_once_and_later_execution_failure_reports_failed`

  条件: manual/routine × done/asking/blocked × 完了前失敗/完了後失敗/成功。done通知なし、待ち先なし通知なし、failedは対応したコメントURLを使用。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_manual_run_is_not_announced` -> `crates/gb-server/src/member/native.rs::native_completed_ticket_waits_notify_once_and_later_execution_failure_reports_failed`

  条件: routine/manual×done/asking/blocked×完了後失敗有無の12条件。実member完了、routine人待ちだけ1通知、後続失敗はfailed通知に置換、manualは通知0、失敗コメントが先に追加済。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_notice_that_cannot_be_posted_changes_nothing_of_the_run` -> `crates/gb-server/src/ticket_selector/tests.rs::failed_comment_delivery_keeps_rate_limit_evidence_and_an_unaddressed_failure_still_notifies`

  条件: 失敗説明/レート制限のコメント送信失敗でも例外の分類・Activity・run結果を変更しない。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_each_stop_is_announced_by_its_own_notice` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: 同じitem/comment URLで異なるrunのasking/blockedを同じchannelへ各1回。URLを重複キーにして次のrunを抑止しない。

- `tests/guildbotics/drivers/test_ticket_selector.py::test_a_run_that_fails_after_its_completion_is_announced_as_failed` -> `crates/gb-server/src/member/native.rs::native_completed_ticket_waits_notify_once_and_later_execution_failure_reports_failed`

  条件: routine/manual×done/asking/blocked×完了後失敗有無の12条件。実member完了、routine人待ちだけ1通知、後続失敗はfailed通知に置換、manualは通知0、失敗コメントが先に追加済。

### tests/guildbotics/drivers/test_review_limit.py

- `tests/guildbotics/drivers/test_review_limit.py::test_provider_only_returns_the_review_limit_decision` -> `#88：providerのreview-limit判定は読取専用の既存port契約。`

  条件: 既存Branvil code parityのreview limitケースで3head、全PR/コメント不変、明示依頼優先を確認済み。このPRは返された判定のsettlementを実装。

- `tests/guildbotics/drivers/test_review_limit.py::test_listing_hands_over_in_its_own_trace_and_runs_the_next_ticket` -> `crates/gb-server/src/ticket_selector/tests.rs::listing_limited_pulls_uses_distinct_traces_and_refresh_limit_still_runs_the_next_ticket`

  条件: manual/scheduled/routine×listing時/refresh時の上限到達。2PRを別traceでDraft化・通知、次のticketを1回だけ実行。外側trace IDと両handover IDは異なりagent run記録無し。

- `tests/guildbotics/drivers/test_review_limit.py::test_conversion_failure_is_reported_but_unavailability_is_deferred` -> `crates/gb-server/src/ticket_selector/tests.rs::review_limit_conversion_failures_notify_but_unavailability_defers_and_retries_without_an_agent`

  条件: Failed/NotFound/Unsupportedはfailedコメント・停止通知・failed終端、Unavailableはコメント/通知なし・deferred終端。次回復旧とVM未起動も確認。

- `tests/guildbotics/drivers/test_review_limit.py::test_refresh_hands_over_and_duplicate_decisions_do_not_notify_twice` -> `crates/gb-server/src/ticket_selector/tests.rs::concurrent_reviewers_and_a_prior_human_draft_never_announce_a_second_conversion`

  条件: 2reviewerの同時handover、humanが先にDraft化の両条件。勝者だけ1コメント・1通知・1activity、先にDraft済なら0件。

- `tests/guildbotics/drivers/test_review_limit.py::test_a_failed_pr_notice_still_records_and_notifies` -> `crates/gb-server/src/ticket_selector/tests.rs::review_limit_failed_pr_notice_still_records_and_notifies_with_the_pr_address`

  条件: PRコメント失敗でもActivityのcomment_errorとreview_limit停止通知・PR URLをassert。

- `tests/guildbotics/drivers/test_review_limit.py::test_cancellation_closes_its_trace_and_restores_the_outer_trace` -> `crates/gb-server/src/ticket_selector/tests.rs::cancelled_review_limit_conversion_and_comment_close_the_trace_and_record_any_completed_draft`

  条件: convert/comment両awaitの途中でfutureをDrop。開始・取消失敗境界を同じtraceに残し、変換済のcomment取消だけdraft factを残す。外側traceは引数値でありグローバルcorrelationを持たない。

- `tests/guildbotics/drivers/test_review_limit.py::test_run_next_continues_when_the_first_candidate_reaches_the_limit_at_refresh` -> `crates/gb-server/src/ticket_selector/tests.rs::listing_limited_pulls_uses_distinct_traces_and_refresh_limit_still_runs_the_next_ticket`

  条件: manual/scheduled/routine×listing時/refresh時の上限到達。2PRを別traceでDraft化・通知、次のticketを1回だけ実行。外側trace IDと両handover IDは異なりagent run記録無し。

- `tests/guildbotics/drivers/test_review_limit.py::test_a_human_who_drafted_after_selection_is_not_announced_as_the_host` -> `crates/gb-server/src/ticket_selector/tests.rs::concurrent_reviewers_and_a_prior_human_draft_never_announce_a_second_conversion`

  条件: 2reviewerの同時handover、humanが先にDraft化の両条件。勝者だけ1コメント・1通知・1activity、先にDraft済なら0件。

- `tests/guildbotics/drivers/test_review_limit.py::test_listing_settles_every_limited_pr_in_a_separate_trace` -> `crates/gb-server/src/ticket_selector/tests.rs::listing_limited_pulls_uses_distinct_traces_and_refresh_limit_still_runs_the_next_ticket`

  条件: manual/scheduled/routine×listing時/refresh時の上限到達。2PRを別traceでDraft化・通知、次のticketを1回だけ実行。外側trace IDと両handover IDは異なりagent run記録無し。

- `tests/guildbotics/drivers/test_review_limit.py::test_two_reviewers_with_preselected_decisions_announce_only_the_winner` -> `crates/gb-server/src/ticket_selector/tests.rs::concurrent_reviewers_and_a_prior_human_draft_never_announce_a_second_conversion`

  条件: 2reviewerの同時handover、humanが先にDraft化の両条件。勝者だけ1コメント・1通知・1activity、先にDraft済なら0件。

- `tests/guildbotics/drivers/test_review_limit.py::test_a_deferred_conversion_is_tried_again_on_the_next_listing` -> `crates/gb-server/src/ticket_selector/tests.rs::review_limit_conversion_failures_notify_but_unavailability_defers_and_retries_without_an_agent`

  条件: Unavailableでは副作用なし・見送り。解除後のhandoverでDraft化し1通知、一般失敗/NotFound/Unsupportedは失敗通知として区別。

### tests/guildbotics/drivers/test_workflow_invocation_architecture.py

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_workflow_invocation_dataclass` -> `移さない：Python dataclassのconstructor/field保持の仕組みは共有wireのschema生成型へ置換済。`

  条件: payload/command/member/source/work/runの実際の受渡しはDispatcherとNative journeyが検査。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_workflow_dispatcher_dispatch` -> `crates/gb-server/src/dispatcher.rs::dispatcher_resolves_member_override_once_and_carries_the_callers_work_and_run`

  条件: member上書き解決、引用符付き引数、work種別/identity、同じtrace/run、broker chat subject、交換directoryを確認。Python Context.clone/acloseは削除しコマンドVMのsettlementが引き継ぐ。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_task_scheduler_uses_ticket_selector` -> `crates/gb-server/src/scheduler.rs::scheduled_then_routines_round_robin_then_chat_and_no_schedule_runs_twice`

  条件: 定期→routine round robin→chatの順序、定期を同一slotで重複実行しない。Native local journeyで実Dispatcherとの接続も検査。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_task_scheduler_ticket_selector_returns_none` -> `crates/gb-server/src/member/native.rs::workflow_runtime_assembles_local_ports_and_idle_ticket_command_leaves_no_run`

  条件: 実組立Runtimeでidle選択時にtrace・run無し。手動HTTPの空output/trace IDとVM無しはscheduler HTTPでも検査。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_task_scheduler_ticket_selector_raises_error` -> `crates/gb-server/src/member/native.rs::native_failed_selection_closes_traces_without_runs_and_records_the_worker_error_limit`

  条件: candidates/refreshそれぞれ3回失敗、VM/runを作らず各失敗traceを閉じ、3回でworker failedを記録。世代ID・person・source・連続回数・上限も確認。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_run_command_malformed` -> `crates/gb-server/src/member/native.rs::malformed_scheduled_and_routine_commands_fail_under_a_closed_trace_and_run`

  条件: scheduled/routine×閉じない引用符/空文字/未登録command。VM0回、6件のfailed run、開始+失敗の閉じたtrace2件。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_task_scheduler_malformed_scheduled_command` -> `crates/gb-server/src/member/native.rs::malformed_scheduled_and_routine_commands_fail_under_a_closed_trace_and_run`

  条件: scheduled/routine×閉じない引用符/空文字/未登録command。VM0回、6件のfailed run、開始+失敗の閉じたtrace2件。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_task_scheduler_malformed_routine_command` -> `crates/gb-server/src/member/native.rs::malformed_scheduled_and_routine_commands_fail_under_a_closed_trace_and_run`

  条件: scheduled/routine×閉じない引用符/空文字/未登録command。VM0回、6件のfailed run、開始+失敗の閉じたtrace2件。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_task_scheduler_routine_with_args_runs_as_a_command` -> `crates/gb-server/src/member/native.rs::native_ticket_patrol_moves_the_local_ticket_and_accepts_real_member_comment_and_completion`

  条件: bare routineだけがEngine.patrol。引数付き経路ではhostで選択したtaskを保ち、二語の引数を分割せず実commandへ渡す。VMへ送る前にhost制御項目はpayloadから除去。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_dispatcher_reuses_active_trace` -> `crates/gb-server/src/dispatcher.rs::dispatcher_resolves_member_override_once_and_carries_the_callers_work_and_run`

  条件: member上書き解決、引用符付き引数、work種別/identity、同じtrace/run、broker chat subject、交換directoryを確認。Python Context.clone/acloseは削除しコマンドVMのsettlementが引き継ぐ。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_dispatcher_does_not_open_its_own_trace` -> `crates/gb-server/src/dispatcher.rs::dispatcher_resolves_member_override_once_and_carries_the_callers_work_and_run`

  条件: member上書き解決、引用符付き引数、work種別/identity、同じtrace/run、broker chat subject、交換directoryを確認。Python Context.clone/acloseは削除しコマンドVMのsettlementが引き継ぐ。

- `tests/guildbotics/drivers/test_workflow_invocation_architecture.py::test_run_command_empty` -> `crates/gb-server/src/member/native.rs::malformed_scheduled_and_routine_commands_fail_under_a_closed_trace_and_run`

  条件: scheduled/routine×閉じない引用符/空文字/未登録command。VM0回、6件のfailed run、開始+失敗の閉じたtrace2件。

### tests/guildbotics/drivers/test_pending_chat_dispatcher.py

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_runs_workflow_and_clears_pending` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_runs_same_chat_event_for_each_member` -> `crates/gb-server/src/member/native.rs::each_member_can_dispatch_the_same_event_and_thread_identity_survives_new_input`

  条件: 同じproviderイベントをaiko/yuki別queueで消化。aikoの次メッセージは同一会話identity・cursor前進。本人provider組立は別のNative組立検査、effort保持はrequired IOとVMの検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_only_claims_the_event_the_workflow_records_the_run` -> `移さない：Pythonの開始責務のmock検査。RustはExecutionが開始・終端記録を所有し、dispatch前のbatch証跡を保証する設計。`

  条件: Nativeのlocal journeyで開始時点と終端を観測する。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_finishes_the_run_the_workflow_started` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_tracks_work_under_its_trace_id` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatch_records_the_trace_boundary_around_the_turn` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_event_that_is_not_work_leaves_no_trace` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_participation_gates_leave_no_run_and_provider_outage_keeps_budget`

  条件: strict/social/muted、他人宛、自己投稿、未参加はrunなし・ack。provider未到達は試行を消費しない。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_selection_failure_is_traced_and_retried` -> `crates/gb-server/src/member/native.rs::local_chat_preparation_failure_spends_budget_but_history_outage_retries_without_a_run`

  条件: identity失敗/未到達とhistory失敗を分離。genericは予算消費・最終通知、history取得不能は期限なくpending。strict/social/mutedを同じ鮮度gateで扱い、cacheで代行しない。旧未到達1試行消費から0に変更（届いていない操作を失敗扱いしない）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_unavailable_chat_service_is_traced_and_retried` -> `crates/gb-server/src/member/native.rs::local_chat_preparation_failure_spends_budget_but_history_outage_retries_without_a_run`

  条件: identity失敗/未到達とhistory失敗を分離。genericは予算消費・最終通知、history取得不能は期限なくpending。strict/social/mutedを同じ鮮度gateで扱い、cacheで代行しない。旧未到達1試行消費から0に変更（届いていない操作を失敗扱いしない）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_selection_failure_on_the_final_attempt_abandons_the_event` -> `crates/gb-server/src/member/native.rs::local_chat_preparation_failure_spends_budget_but_history_outage_retries_without_a_run`

  条件: identity失敗/未到達とhistory失敗を分離。genericは予算消費・最終通知、history取得不能は期限なくpending。strict/social/mutedを同じ鮮度gateで扱い、cacheで代行しない。旧未到達1試行消費から0に変更（届いていない操作を失敗扱いしない）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_selected_event_runs_under_a_trace_naming_its_thread` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatch_records_a_failed_boundary_and_still_retries` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 共通Executionのlease拒否はrunなし、強制取消はjournalのcancelled終端とlease再取得・snapshot空。traceのcancelled境界はNativeと観測基盤の検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_cancelled_dispatch_records_a_failed_boundary` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 共通Executionのlease拒否はrunなし、強制取消はjournalのcancelled終端とlease再取得・snapshot空。traceのcancelled境界はNativeと観測基盤の検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_rejected_dispatch_records_no_boundary` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 共通Executionのlease拒否はrunなし、強制取消はjournalのcancelled終端とlease再取得・snapshot空。traceのcancelled境界はNativeと観測基盤の検査を併用。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_uses_env_for_initial_retry_budget` -> `crates/gb-server/src/member/native.rs::initial_chat_budget_reads_the_process_environment_without_mutating_the_test_runtime`

  条件: 子プロセスの環境変数10を実Dispatcherで読み、attempt1/max10の保存値を確認。親プロセスの環境は変更しない。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_skips_already_processed` -> `crates/gb-server/src/member/native.rs::equal_time_threads_sort_by_id_and_already_processed_input_never_calls_the_provider`

  条件: 同時刻ID順。処理済queueを復元しprovider identityに失敗canaryを置いても呼ばれず、run追加なしでqueue除去。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_leaves_event_queued_on_error` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_failure_log_shares_workflow_trace` -> `crates/gb-server/src/member/native.rs::native_chat_retry_events_keep_command_trace_rate_metadata_and_capped_abandonment`

  条件: 実Runtimeのログと診断/共有Activityを検査。person/command/source/trace/世代/chat属性、retryとabandon、予算超過入力99を上限3へ丸める。レート属性・reset・対象イベントも確認。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_escalates_final_attempt_failure_to_error` -> `crates/gb-server/src/member/native.rs::native_chat_retry_events_keep_command_trace_rate_metadata_and_capped_abandonment`

  条件: 実Runtimeのログと診断/共有Activityを検査。person/command/source/trace/世代/chat属性、retryとabandon、予算超過入力99を上限3へ丸める。レート属性・reset・対象イベントも確認。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_follower_in_same_thread_never_overtakes_backing_off_head` -> `crates/gb-server/src/member/native.rs::a_failed_head_wakes_once_only_and_other_threads_and_terminal_followers_keep_their_order`

  条件: failed/unknown/rateカテゴリ。failedのみ新着cursorで一度早起き、同じfollowerでは再試行せず、別threadは進行、最終headだけ除去→次巡回でfollower。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_follower_arrival_wakes_backing_off_head_once` -> `crates/gb-server/src/member/native.rs::a_failed_head_wakes_once_only_and_other_threads_and_terminal_followers_keep_their_order`

  条件: failed/unknown/rateカテゴリ。failedのみ新着cursorで一度早起き、同じfollowerでは再試行せず、別threadは進行、最終headだけ除去→次巡回でfollower。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_messages_of_one_time_run_in_the_order_of_their_ids` -> `crates/gb-server/src/member/native.rs::equal_time_threads_sort_by_id_and_already_processed_input_never_calls_the_provider`

  条件: 同時刻ID順。処理済queueを復元しprovider identityに失敗canaryを置いても呼ばれず、run追加なしでqueue除去。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_follower_does_not_wake_head_with_unknown_error_category` -> `crates/gb-server/src/member/native.rs::a_failed_head_wakes_once_only_and_other_threads_and_terminal_followers_keep_their_order`

  条件: failed/unknown/rateカテゴリ。failedのみ新着cursorで一度早起き、同じfollowerでは再試行せず、別threadは進行、最終headだけ除去→次巡回でfollower。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_head_failure_blocks_thread_but_not_other_threads` -> `crates/gb-server/src/member/native.rs::a_failed_head_wakes_once_only_and_other_threads_and_terminal_followers_keep_their_order`

  条件: failed/unknown/rateカテゴリ。failedのみ新着cursorで一度早起き、同じfollowerでは再試行せず、別threadは進行、最終headだけ除去→次巡回でfollower。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_thread_follower_runs_after_head_terminalizes` -> `crates/gb-server/src/member/native.rs::a_failed_head_wakes_once_only_and_other_threads_and_terminal_followers_keep_their_order`

  条件: failed/unknown/rateカテゴリ。failedのみ新着cursorで一度早起き、同じfollowerでは再試行せず、別threadは進行、最終headだけ除去→次巡回でfollower。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_reloads_thread_after_each_workflow_snapshot` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_freezes_latest_batch_and_recovers_completion_without_reinvocation`

  条件: 編集済provider snapshotと新着を一括化、1回dispatchでqueued followerを消化、完了run再配置時はVM再実行なし。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_uses_provider_exact_rate_limit_reset` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_plain_failure_wakes_for_new_follower_but_rate_limit_does_not`

  条件: provider resetを保存して未来時刻を維持、新着でもrate制限は起こさない。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_rejection_does_not_consume_retry_attempt` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_skips_future_retry` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatch_failure_records_retry_scheduled_event` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_thread_context_unavailable_keeps_event_pending_forever` -> `crates/gb-server/src/member/native.rs::local_chat_preparation_failure_spends_budget_but_history_outage_retries_without_a_run`

  条件: identity失敗/未到達とhistory失敗を分離。genericは予算消費・最終通知、history取得不能は期限なくpending。strict/social/mutedを同じ鮮度gateで扱い、cacheで代行しない。旧未到達1試行消費から0に変更（届いていない操作を失敗扱いしない）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_dispatcher_side_abandon_records_abandoned_event` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_failed_event_runs_again_once_its_retry_time_arrives` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_event_completed_elsewhere_is_processed_without_running_again` -> `crates/gb-server/src/member/native.rs::completed_chat_recovery_consumes_only_its_batch_and_updates_with_a_later_action`

  条件: batchあり/なし × 更新後action7種またはなし。完了証跡後の更新だけを取り込み、新着M4を残す。blocked/noopは更新の処理根拠にしない。

- `tests/guildbotics/drivers/test_pending_chat_dispatcher.py::test_repeated_failures_use_the_whole_attempt_budget_then_abandon` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

### tests/guildbotics/drivers/test_event_listener_runner.py

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_runner_notifies_when_worker_thread_stops` -> `移さない：Pythonのthread-local event loopとthread→loop参照の管理を廃止。`

  条件: Rustはservice世代が所有するNotify/JoinHandleを停止時に回収。別OS threadからのNotifyと単一stopはRustの受信検査で確認。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_a_message_goes_round_to_the_members_reply_on_the_local_chat` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local provider受信→pending→本番dispatch→実member reply→完了・queue空。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_members_of_one_connection_share_one_listener` -> `crates/gb-server/src/member/native.rs::native_members_reuse_one_listener_and_wakeup_for_the_same_connection`

  条件: aiko/yuki/aikoの再組立でlistener生成1、listenerとwakeのArc共有。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_no_chat_service_means_nothing_to_listen_to` -> `crates/gb-server/src/member/native.rs::scheduler_http_owns_native_workers_refuses_unauthorized_start_and_releases_the_lock_on_stop`

  条件: subscriptionあり・chat_service無しでもservice開始可、listener_count0、停止可。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_a_member_who_cannot_connect_is_skipped` -> `crates/gb-server/src/member/native.rs::scheduler_http_owns_native_workers_refuses_unauthorized_start_and_releases_the_lock_on_stop`

  条件: 認証失敗メンバーだけ受信除外、他メンバーとservice継続、HTTPのauth_failed/connection summaryを確認。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_received_events_are_queued_with_their_channels_participation` -> `crates/gb-server/src/member/native.rs::local_chat_receiver_backfills_threads_suppresses_status_and_retains_live_input_during_write_contention`

  条件: channel participation保持、workflow statusはackのみ、live/backfill両方を保存。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_a_workflow_status_message_is_marked_processed_not_queued` -> `crates/gb-server/src/member/native.rs::local_chat_receiver_backfills_threads_suppresses_status_and_retains_live_input_during_write_contention`

  条件: channel participation保持、workflow statusはackのみ、live/backfill両方を保存。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_an_event_is_queued_for_each_member_who_has_not_handled_it` -> `crates/gb-server/src/chat_receiver.rs::one_connection_wakes_from_another_thread_during_backfill_and_fans_out_only_unprocessed_input`

  条件: 履歴取得をブロック、別threadから到着通知→同一listener1回drain、未処理2人に保存、処理済3人目は除外、stop1回。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_backfill_reads_the_channel_and_known_threads_once_per_interval` -> `crates/gb-server/src/chat_receiver.rs::failed_backfill_obeys_each_members_cadence_and_never_claims_ready_until_recovery`

  条件: 2メンバー別間隔、channel/既知threadの巡回、失敗でも次回時刻を進める。回復まではcatching upを維持し完了後だけready。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_backfill_receive_cutoff_floors_the_window_and_filters_events` -> `crates/gb-server/src/chat_receiver.rs::backfill_preserves_cutoff_equal_times_thread_overlap_and_missing_thread_disable`

  条件: 受信cutoffの下限・フィルタ、同時刻ID順、最新既知からoverlap、NotFound threadは次回対象外。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_messages_of_one_time_are_both_backfilled_in_the_order_of_their_ids` -> `crates/gb-server/src/chat_receiver.rs::backfill_preserves_cutoff_equal_times_thread_overlap_and_missing_thread_disable`

  条件: 受信cutoffの下限・フィルタ、同時刻ID順、最新既知からoverlap、NotFound threadは次回対象外。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_thread_backfill_reads_from_the_newest_known_message_less_overlap` -> `crates/gb-server/src/chat_receiver.rs::backfill_preserves_cutoff_equal_times_thread_overlap_and_missing_thread_disable`

  条件: 受信cutoffの下限・フィルタ、同時刻ID順、最新既知からoverlap、NotFound threadは次回対象外。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_a_thread_that_is_gone_is_not_backfilled_again` -> `crates/gb-server/src/chat_receiver.rs::backfill_preserves_cutoff_equal_times_thread_overlap_and_missing_thread_disable`

  条件: 受信cutoffの下限・フィルタ、同時刻ID順、最新既知からoverlap、NotFound threadは次回対象外。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_backfill_is_due_once_per_member_channel_and_interval` -> `crates/gb-server/src/chat_receiver.rs::failed_backfill_obeys_each_members_cadence_and_never_claims_ready_until_recovery`

  条件: 2メンバー別間隔、channel/既知threadの巡回、失敗でも次回時刻を進める。回復まではcatching upを維持し完了後だけready。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_backfill_failure_updates_attempt_cadence` -> `crates/gb-server/src/chat_receiver.rs::failed_backfill_obeys_each_members_cadence_and_never_claims_ready_until_recovery`

  条件: 2メンバー別間隔、channel/既知threadの巡回、失敗でも次回時刻を進める。回復まではcatching upを維持し完了後だけready。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_a_channel_name_is_resolved_once_while_the_subscription_stands` -> `crates/gb-server/src/chat_receiver.rs::one_connection_wakes_from_another_thread_during_backfill_and_fans_out_only_unprocessed_input`

  条件: 構成時のname解決は1回、共有listener start/stop各1、in-flight backfillを取消回収。接続cacheはservice世代の所有物。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_subscription_signature_follows_participation` -> `移さない：文字列signature cacheを廃止し型付きsubscriptionの等値比較へ置換。`

  条件: channel/name/participationの値をReceiverに固定し、service再起動で全subscriptionを再構成する。動的設定のUIは#91。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_aclose_sources_stops_listeners_and_clears_caches` -> `crates/gb-server/src/chat_receiver.rs::one_connection_wakes_from_another_thread_during_backfill_and_fans_out_only_unprocessed_input`

  条件: 構成時のname解決は1回、共有listener start/stop各1、in-flight backfillを取消回収。接続cacheはservice世代の所有物。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_get_status_summary_surfaces_auth_failed_connections` -> `crates/gb-server/src/member/native.rs::scheduler_http_owns_native_workers_refuses_unauthorized_start_and_releases_the_lock_on_stop`

  条件: 認証失敗メンバーだけ受信除外、他メンバーとservice継続、HTTPのauth_failed/connection summaryを確認。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_stop_cancels_in_flight_cycle` -> `crates/gb-server/src/chat_receiver.rs::one_connection_wakes_from_another_thread_during_backfill_and_fans_out_only_unprocessed_input`

  条件: 構成時のname解決は1回、共有listener start/stop各1、in-flight backfillを取消回収。接続cacheはservice世代の所有物。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_socket_notification_persists_while_backfill_is_waiting` -> `crates/gb-server/src/chat_receiver.rs::one_connection_wakes_from_another_thread_during_backfill_and_fans_out_only_unprocessed_input`

  条件: 履歴取得をブロック、別threadから到着通知→同一listener1回drain、未処理2人に保存、処理済3人目は除外、stop1回。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_receive_save_failure_retains_event_and_marks_unavailable` -> `crates/gb-server/src/chat_receiver.rs::failed_receive_save_retains_input_and_heartbeat_recovers_only_after_durable_save`

  条件: 保存先をdirectoryにして失敗、未保存入力を保持・unavailable。復旧後1回だけ保存してready。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_receiver_can_restart_on_another_event_loop` -> `移さない：Pythonのthread-local event loopとthread→loop参照の管理を廃止。`

  条件: Rustはservice世代が所有するNotify/JoinHandleを停止時に回収。別OS threadからのNotifyと単一stopはRustの受信検査で確認。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_write_lock_wait_keeps_heartbeat_alive_and_prevents_publication` -> `crates/gb-server/src/chat_receiver.rs::both_live_and_backfill_shared_write_waits_keep_a_fresh_catching_up_heartbeat`

  条件: live/backfill × shared write lock待機。heartbeat更新継続・catching up、保存完了までpublish gateを閉じる。

- `tests/guildbotics/drivers/test_event_listener_runner.py::test_wake_receiver_uses_one_loop_reference_during_shutdown` -> `移さない：Pythonのthread-local event loopとthread→loop参照の管理を廃止。`

  条件: Rustはservice世代が所有するNotify/JoinHandleを停止時に回収。別OS threadからのNotifyと単一stopはRustの受信検査で確認。

### tests/guildbotics/drivers/test_execution.py

- `tests/guildbotics/drivers/test_execution.py::test_track_work_snapshot_and_completion` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: id/person/source snapshot、同person排他とreadonly並列、別person許可、取消journal後のlease解放とsnapshot空。各sourceはNative境界検査でも確認。

- `tests/guildbotics/drivers/test_execution.py::test_person_lease_rejects_same_person_and_allows_different_person` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 同一member競合と記録無し、別member同identity許可、非exclusive並行とlease非保持、drain拒否、取消を永続化後にlease解放、成功済identity重複拒否。

- `tests/guildbotics/drivers/test_execution.py::test_begin_drain_rejects_new_work_until_drained` -> `crates/gb-server/src/execution.rs::coordinator_seals_registration_until_graceful_drain_finishes_and_force_escalates_it`

  条件: graceful drain中はspawn拒否、既存workをreleaseするまではwait未完了、後に新世代へ。

- `tests/guildbotics/drivers/test_execution.py::test_drain_gate_stays_closed_until_wait_for_drain` -> `crates/gb-server/src/execution.rs::coordinator_seals_registration_until_graceful_drain_finishes_and_force_escalates_it`

  条件: 登録とdrainのsealを同じMutexにし、waitが終わるまでは同じ世代。

- `tests/guildbotics/drivers/test_execution.py::test_forced_drain_cancels_active_work` -> `crates/gb-server/src/execution.rs::coordinator_seals_registration_until_graceful_drain_finishes_and_force_escalates_it`

  条件: gracefulとforceはArc同一世代、forceでcancel tokenが既存workへ届く。

- `tests/guildbotics/drivers/test_execution.py::test_wait_for_drain_timeout_closes_drain_window` -> `移さない：停止待ちにtimeoutでgateを開く旧APIは廃止。停止完了だけが次のgenerationを開始する。`

  条件: 未完了の停止中に新規workが入る状態を廃止。graceful/forceとgate保持はCoordinator検査。

- `tests/guildbotics/drivers/test_execution.py::test_non_exclusive_work_runs_alongside_the_same_person_lease` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 同一member競合と記録無し、別member同identity許可、非exclusive並行とlease非保持、drain拒否、取消を永続化後にlease解放、成功済identity重複拒否。

- `tests/guildbotics/drivers/test_execution.py::test_non_exclusive_work_does_not_hold_the_person_lease` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 同一member競合と記録無し、別member同identity許可、非exclusive並行とlease非保持、drain拒否、取消を永続化後にlease解放、成功済identity重複拒否。

- `tests/guildbotics/drivers/test_execution.py::test_non_exclusive_work_is_still_rejected_while_draining` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 同一member競合と記録無し、別member同identity許可、非exclusive並行とlease非保持、drain拒否、取消を永続化後にlease解放、成功済identity重複拒否。

- `tests/guildbotics/drivers/test_execution.py::test_task_run_begin_deduplicates_terminal_work_and_finishes` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 同一member競合と記録無し、別member同identity許可、非exclusive並行とlease非保持、drain拒否、取消を永続化後にlease解放、成功済identity重複拒否。

- `tests/guildbotics/drivers/test_execution.py::test_task_run_begin_allows_same_identity_for_different_members` -> `crates/gb-server/src/execution.rs::every_source_shares_one_member_lease_and_cancel_is_journaled_before_release`

  条件: 同一member競合と記録無し、別member同identity許可、非exclusive並行とlease非保持、drain拒否、取消を永続化後にlease解放、成功済identity重複拒否。

- `tests/guildbotics/drivers/test_execution.py::test_task_run_begin_retries_every_terminal_state_that_left_work_undone` -> `crates/gb-server/src/execution.rs::every_terminal_state_preserves_evidence_and_only_unfinished_work_retries`

  条件: 全終端6状態を網羅。失敗・取消・中断・見送りは同一runで証拠を保持して再開、成功・結果不明・asking済は重複拒否。

- `tests/guildbotics/drivers/test_execution.py::test_unavailable_work_closes_as_deferred_without_a_failure_record` -> `crates/gb-server/src/member/native.rs::native_commands_keep_trace_and_journal_in_sync_when_only_autonomous_service_outages_defer`

  条件: manual/scheduled/routine/event_queue×Failure/NotFound/Unsupported/Unavailableの16条件。autonomous Unavailableだけ見送り、他は失敗。journalとtrace境界2件の終端と型名一致。

- `tests/guildbotics/drivers/test_execution.py::test_task_run_begin_keeps_a_recorded_asking_completion_terminal` -> `crates/gb-server/src/execution.rs::every_terminal_state_preserves_evidence_and_only_unfinished_work_retries`

  条件: 全終端6状態を網羅。失敗・取消・中断・見送りは同一runで証拠を保持して再開、成功・結果不明・asking済は重複拒否。

- `tests/guildbotics/drivers/test_execution.py::test_claim_only_begin_records_nothing_and_leaves_nothing_to_finish` -> `crates/gb-server/src/execution.rs::idle_selection_leaves_no_record_and_deferred_input_can_retry`

  条件: 開始記録を作らないclaimを終了してもrecord無し。占有中の拒否は共通member lease検査。

- `tests/guildbotics/drivers/test_execution.py::test_claim_only_begin_still_rejects_a_held_identity` -> `crates/gb-server/src/execution.rs::idle_selection_leaves_no_record_and_deferred_input_can_retry`

  条件: 開始記録を作らないclaimを終了してもrecord無し。占有中の拒否は共通member lease検査。

- `tests/guildbotics/drivers/test_execution.py::test_begin_and_finish_mirror_the_runs_trace_into_its_record` -> `#924：RunStoreのfirst-seen属性保持と更新は既存Rustの永続化契約。`

  条件: このPRではticket/chatの属性を開始時に明示供給し、Native journeyで終了後のtitle/urlとservice IDを検査。mutableなthread-local traceの追跡は廃止。

- `tests/guildbotics/drivers/test_execution.py::test_begin_outside_the_runs_trace_stamps_only_the_fallback_source` -> `移さない：グローバルのcurrent traceから暗黙に値を拾う仕組みを廃止。`

  条件: Rustは明示のRunRecordとTraceを渡す。別traceが混入する状態を作れず、sourceは呼出元から渡される。

### tests/guildbotics/capabilities/test_chat_selection.py

- `tests/guildbotics/capabilities/test_chat_selection.py::test_workflow_delegates_to_handle_chat_event_and_updates_reply_state` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_redispatch_of_completed_run_skips_agent_and_reuses_evidence` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_freezes_latest_batch_and_recovers_completion_without_reinvocation`

  条件: 編集済provider snapshotと新着を一括化、1回dispatchでqueued followerを消化、完了run再配置時はVM再実行なし。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_two_messages_in_one_thread_share_conversation_and_advance_cursor` -> `crates/gb-server/src/member/native.rs::each_member_can_dispatch_the_same_event_and_thread_identity_survives_new_input`

  条件: 同じproviderイベントをaiko/yuki別queueで消化。aikoの次メッセージは同一会話identity・cursor前進。本人provider組立は別のNative組立検査、effort保持はrequired IOとVMの検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_live_thread_snapshot_paginates_and_keeps_latest_bound` -> `crates/gb-server/src/member/native.rs::local_chat_snapshot_paginates_bounds_history_and_keeps_every_unread_message_at_the_same_time`

  条件: local実provider 150件を100+50ページで取得。head=150では過去100件M050..M149、head=1では未読150件を全保持。同時刻のID順もassert。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_reaction_only_completion_processes_without_bot_message` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_noop_completion_processes_without_visible_action` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_taking_a_batch_records_the_runs_start_under_its_trace` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: local受信→本番Runtime dispatch→実member reply/noop/reaction→complete。run開始をDriver内で確認、trace=run、世代snapshot、chat属性、batch証跡より後のaction、visible actionのみ参加者追加、queue空。VMのみdoubleで同梱workflowのPython検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_declined_batch_leaves_no_run_record` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_participation_gates_leave_no_run_and_provider_outage_keeps_budget`

  条件: strict/social/muted、他人宛、自己投稿、未参加はrunなし・ack。provider未到達は試行を消費しない。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_unmentioned_new_thread_is_processed_without_agent` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_participation_gates_leave_no_run_and_provider_outage_keeps_budget`

  条件: strict/social/muted、他人宛、自己投稿、未参加はrunなし・ack。provider未到達は試行を消費しない。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_social_unmentioned_new_thread_delegates` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_participation_gates_leave_no_run_and_provider_outage_keeps_budget`

  条件: strict/social/muted、他人宛、自己投稿、未参加はrunなし・ack。provider未到達は試行を消費しない。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_unmentioned_followup_after_prior_mention_delegates` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_muted_unmentioned_followup_after_prior_mention_skips` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_empty_cache_followup_participates_via_provider_snapshot` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_snapshot_is_fetched_once_and_persisted_to_cache` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_provider_unavailable_without_cache_keeps_event_unprocessed` -> `crates/gb-server/src/member/native.rs::local_chat_preparation_failure_spends_budget_but_history_outage_retries_without_a_run`

  条件: identity失敗/未到達とhistory失敗を分離。genericは予算消費・最終通知、history取得不能は期限なくpending。strict/social/mutedを同じ鮮度gateで扱い、cacheで代行しない。旧未到達1試行消費から0に変更（届いていない操作を失敗扱いしない）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_provider_unavailable_with_cached_mention_waits_for_fresh_input` -> `crates/gb-server/src/member/native.rs::local_chat_preparation_failure_spends_budget_but_history_outage_retries_without_a_run`

  条件: identity失敗/未到達とhistory失敗を分離。genericは予算消費・最終通知、history取得不能は期限なくpending。strict/social/mutedを同じ鮮度gateで扱い、cacheで代行しない。旧未到達1試行消費から0に変更（届いていない操作を失敗扱いしない）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_followup_mentioning_other_member_skips_even_after_prior_mention` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_author_labels_include_mentionable_team_members_not_in_thread` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_handoff_candidates_include_only_mentionable_other_members_with_roles` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_chat_user_to_person_labels_uses_each_members_chat_user` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_an_agent_without_a_configured_chat_user_is_its_credentials_user` -> `crates/gb-server/src/member/native.rs::workflow_runtime_assembles_local_ports_and_idle_ticket_command_leaves_no_run`

  条件: active aiko、inactive kenjiともlocal本人credential identityへ。human ownerは推測しない。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_record_handoffs_saves_mentions_to_known_members` -> `crates/gb-server/src/member/native.rs::recovered_reply_records_only_known_team_handoffs_and_preserves_topic_focus_and_effort`

  条件: 完了返信からknown memberのmentionだけhandoff保存、roles/本文/返信ID/topic/focusを確認。未知のmentionは無視、effort保持、reply cacheと参加者追加。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_record_handoffs_ignores_non_team_participant_labels` -> `crates/gb-server/src/member/native.rs::recovered_reply_records_only_known_team_handoffs_and_preserves_topic_focus_and_effort`

  条件: 完了返信からknown memberのmentionだけhandoff保存、roles/本文/返信ID/topic/focusを確認。未知のmentionは無視、effort保持、reply cacheと参加者追加。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_prompt_payload_includes_existing_handoffs` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_incomplete_turns_retry_then_escalate` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_agent_run_failure_escalates_and_stops` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_non_final_agent_run_failure_bubbles_for_pending_backoff` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_rate_limit_posts_notice_and_leaves_event_pending` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_duplicate_system_notice_is_not_posted_again` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_final_notice_post_failure_marks_processed_without_notice_state` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_non_final_prerun_identity_failure_bubbles` -> `crates/gb-server/src/member/native.rs::local_chat_preparation_failure_spends_budget_but_history_outage_retries_without_a_run`

  条件: identity失敗/未到達とhistory失敗を分離。genericは予算消費・最終通知、history取得不能は期限なくpending。strict/social/mutedを同じ鮮度gateで扱い、cacheで代行しない。旧未到達1試行消費から0に変更（届いていない操作を失敗扱いしない）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_completion_on_retry_stops_early` -> `移さない：#81で判定とresponse agentは同じVMの共有Pythonに保持。`

  条件: 共有workflow検査で理由4種・既定/高effort、最初/2回目完了、判断1回・明示空入力・model/brain/前effort漏入なしを確認。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_obvious_self_message_is_marked_processed_without_agent` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_participation_gates_leave_no_run_and_provider_outage_keeps_budget`

  条件: strict/social/muted、他人宛、自己投稿、未参加はrunなし・ack。provider未到達は試行を消費しない。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_final_attempt_abandon_records_dispatch_abandoned_event` -> `crates/gb-server/src/member/native.rs::chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts`

  条件: 通常失敗/レート制限 × 通知成功/失敗、3試行全消費、60/120秒backoff、未来では呼出なし、run同一、lease拒否は0消費、notice最大1、最終ackとabandon。旧内部agent retryはVM側共有turnが所有（別検査）。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_recovered_completion_records_workflow_completed_event` -> `crates/gb-server/src/member/native.rs::completed_chat_recovery_consumes_only_its_batch_and_updates_with_a_later_action`

  条件: batchあり/なし × 更新後action7種またはなし。完了証跡後の更新だけを取り込み、新着M4を残す。blocked/noopは更新の処理根拠にしない。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_judgment_without_effort_preserves_response_settings` -> `移さない：#81で判定とresponse agentは同じVMの共有Pythonに保持。`

  条件: 共有workflow検査で理由4種・既定/高effort、最初/2回目完了、判断1回・明示空入力・model/brain/前effort漏入なしを確認。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_response_effort_is_promoted_and_persisted` -> `crates/gb-server/src/required_io.rs::durable_judgment_promotes_only_its_bound_threads_effort_and_never_downgrades_it`

  条件: 7組のstored/candidate effort、保存前のadoption拒否、bound threadだけ更新、high降格なし、topic保持、別person拒否。VM側は判断effortを同じresponse retryへ渡し、replayは実スレッドを変えない。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_no_invoked_command_inherits_another_command_output_as_input` -> `移さない：#81で判定とresponse agentは同じVMの共有Pythonに保持。`

  条件: 共有workflow検査で理由4種・既定/高effort、最初/2回目完了、判断1回・明示空入力・model/brain/前effort漏入なしを確認。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_judgment_runs_once_per_event_not_per_retry` -> `移さない：#81で判定とresponse agentは同じVMの共有Pythonに保持。`

  条件: 共有workflow検査で理由4種・既定/高effort、最初/2回目完了、判断1回・明示空入力・model/brain/前effort漏入なしを確認。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_batch_reads_requests_and_corrections_but_leaves_inflight_arrivals` -> `crates/gb-server/src/member/native.rs::failed_native_chat_batches_refresh_edited_and_new_input_and_preserve_prior_action_evidence`

  条件: strict/muted × 既存actionあり/なし、failed終端・resultなし、同一runで再起動、編集と新着でbatch2→3、過去action証跡保持、最終queue空。完了済回復で新着を残す条件は専用回復検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_failed_batch_is_refreshed_on_retry_without_losing_action_evidence` -> `crates/gb-server/src/member/native.rs::failed_native_chat_batches_refresh_edited_and_new_input_and_preserve_prior_action_evidence`

  条件: strict/muted × 既存actionあり/なし、failed終端・resultなし、同一runで再起動、編集と新着でbatch2→3、過去action証跡保持、最終queue空。完了済回復で新着を残す条件は専用回復検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_completed_batch_recovery_does_not_consume_new_messages` -> `crates/gb-server/src/member/native.rs::completed_chat_recovery_consumes_only_its_batch_and_updates_with_a_later_action`

  条件: batchあり/なし × 更新後action7種またはなし。完了証跡後の更新だけを取り込み、新着M4を残す。blocked/noopは更新の処理根拠にしない。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_batch_keeps_unread_messages_beyond_historical_context_bound` -> `crates/gb-server/src/member/native.rs::local_chat_snapshot_paginates_bounds_history_and_keeps_every_unread_message_at_the_same_time`

  条件: head=1で同時刻のM001..M150を全保持、history空、source_snapshot150、dispatch1回。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_dispatcher_consumes_one_batch_and_skips_its_queued_followers` -> `crates/gb-server/src/member/native.rs::local_chat_dispatch_freezes_latest_batch_and_recovers_completion_without_reinvocation`

  条件: 編集済provider snapshotと新着を一括化、1回dispatchでqueued followerを消化、完了run再配置時はVM再実行なし。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_dispatcher_retries_a_crashed_run_with_the_agent` -> `crates/gb-server/src/member/native.rs::failed_native_chat_batches_refresh_edited_and_new_input_and_preserve_prior_action_evidence`

  条件: strict/muted × 既存actionあり/なし、failed終端・resultなし、同一runで再起動、編集と新着でbatch2→3、過去action証跡保持、最終queue空。完了済回復で新着を残す条件は専用回復検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_batch_includes_members_own_reply_without_using_it_as_reaction_target` -> `crates/gb-server/src/member/native.rs::fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target`

  条件: strict/social/muted × 他人宛/通常。空cacheから履歴を1回取得、過去mention・本人返信を文脈へ、reactionは最後の他者メッセージ、effortとhandoff保持。inactiveでも役割+accountありを候補に、self/役割なし/accountなしを除外。

- `tests/guildbotics/capabilities/test_chat_selection.py::test_updates_read_during_turn_are_consumed_only_on_completion` -> `crates/gb-server/src/member/native.rs::completed_chat_recovery_consumes_only_its_batch_and_updates_with_a_later_action`

  条件: batchあり/なし × 更新後action7種またはなし。完了証跡後の更新だけを取り込み、新着M4を残す。blocked/noopは更新の処理根拠にしない。

### tests/guildbotics/capabilities/test_chat_selection_fast_path.py

- `tests/guildbotics/capabilities/test_chat_selection_fast_path.py::test_fast_path_records_evidence_before_completing` -> `crates/gb-server/src/member/native.rs::native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread`

  条件: reply/noop/reaction全経路のbatch→action→completion・参加者/queueを実member経由で検査。判断→recheck→action順はVM側検査を併用。

- `tests/guildbotics/capabilities/test_chat_selection_fast_path.py::test_unavailable_receiver_never_confirms_a_fast_path` -> `移さない：#81により判定fast pathはVMの共有Pythonに置く。`

  条件: VM側でcatching up/未到達、新着/編集/繰返しページ、reaction/receipt/completion失敗後の再判定を確認。Hostのreaction証跡冪等性と最終abandonはRust側も検査。

- `tests/guildbotics/capabilities/test_chat_selection_fast_path.py::test_new_input_during_evaluation_is_reconsidered` -> `移さない：#81により判定fast pathはVMの共有Pythonに置く。`

  条件: VM側でcatching up/未到達、新着/編集/繰返しページ、reaction/receipt/completion失敗後の再判定を確認。Hostのreaction証跡冪等性と最終abandonはRust側も検査。

- `tests/guildbotics/capabilities/test_chat_selection_fast_path.py::test_reaction_failure_never_completes` -> `移さない：#81により判定fast pathはVMの共有Pythonに置く。`

  条件: VM側でcatching up/未到達、新着/編集/繰返しページ、reaction/receipt/completion失敗後の再判定を確認。Hostのreaction証跡冪等性と最終abandonはRust側も検査。

- `tests/guildbotics/capabilities/test_chat_selection_fast_path.py::test_reaction_recovers_without_duplicate_visible_action` -> `移さない：#81により判定fast pathはVMの共有Pythonに置く。`

  条件: VM側でcatching up/未到達、新着/編集/繰返しページ、reaction/receipt/completion失敗後の再判定を確認。Hostのreaction証跡冪等性と最終abandonはRust側も検査。

- `tests/guildbotics/capabilities/test_chat_selection_fast_path.py::test_failed_judgment_agent_fallback_escalates_on_final_attempt` -> `移さない：#81により判定fast pathはVMの共有Pythonに置く。`

  条件: VM側でcatching up/未到達、新着/編集/繰返しページ、reaction/receipt/completion失敗後の再判定を確認。Hostのreaction証跡冪等性と最終abandonはRust側も検査。

### tests/guildbotics/capabilities/test_decisions.py

- `tests/guildbotics/capabilities/test_decisions.py::test_adoption` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_adoption

- `tests/guildbotics/capabilities/test_decisions.py::test_definite_obligations_precede_uncertainty` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_definite_obligations_precede_uncertainty

- `tests/guildbotics/capabilities/test_decisions.py::test_invalid_evaluation_precedes_a_definite_request` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_invalid_evaluation_precedes_a_definite_request

- `tests/guildbotics/capabilities/test_decisions.py::test_unrelated_unknowns_do_not_prevent_safe_reaction` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_unrelated_unknowns_do_not_prevent_safe_reaction

- `tests/guildbotics/capabilities/test_decisions.py::test_response_effort_depends_on_required_work_not_route_reason` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_response_effort_depends_on_required_work_not_route_reason

- `tests/guildbotics/capabilities/test_decisions.py::test_three_valued_algebra` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_three_valued_algebra

- `tests/guildbotics/capabilities/test_decisions.py::test_jev_noul_thresholds` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_jev_noul_thresholds

- `tests/guildbotics/capabilities/test_decisions.py::test_choice_adopts_top_option_without_a_confidence_threshold` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_choice_adopts_top_option_without_a_confidence_threshold

- `tests/guildbotics/capabilities/test_decisions.py::test_top_reaction_only_applies_after_noul_obligations_are_excluded` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_top_reaction_only_applies_after_noul_obligations_are_excluded

- `tests/guildbotics/capabilities/test_decisions.py::test_choice_rejects_an_option_below_the_maximum_probability` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_choice_rejects_an_option_below_the_maximum_probability

- `tests/guildbotics/capabilities/test_decisions.py::test_invalid_probability_is_not_adopted` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_invalid_probability_is_not_adopted

- `tests/guildbotics/capabilities/test_decisions.py::test_self_reported_confidence_does_not_change_assertion` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_self_reported_confidence_does_not_change_assertion

- `tests/guildbotics/capabilities/test_decisions.py::test_failed_judgment_delegates_without_execution_settings` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_failed_judgment_delegates_without_execution_settings

- `tests/guildbotics/capabilities/test_decisions.py::test_snapshot_is_full_and_replayable_even_without_transcripts` -> `crates/gb-server/src/required_io.rs::replay_input_is_full_redacted_and_exclusive_and_masks_are_loaded_once`

  条件: 全文・全questionの保存、hash、mask1回読込、排他的record、機密canary除去、既存recordを上書きしない。共有Python assessment側のsnapshot equalityも検査。

- `tests/guildbotics/capabilities/test_decisions.py::test_record_failure_cannot_skip_agent` -> `移さない：#81の共有Python assessmentの必須記録失敗検査へ移す。`

  条件: started/resolved/result全3段階で失敗→agent。started/resolved失敗では推論呼出0回、result失敗では既に1回。

- `tests/guildbotics/capabilities/test_decisions.py::test_a_selection_carries_only_a_semantic_reaction` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_a_selection_carries_only_a_semantic_reaction

- `tests/guildbotics/capabilities/test_decisions.py::test_generic_choice_is_not_restricted_to_chat_reactions` -> `移さない：#81の決定に従い共有Pythonへ復元。`

  条件: 関数AST（全assertとparametrize引数を含む）が凍結版と完全一致。#81に従い共有Pythonで実行。 現在の検査: tests/guildbotics/intelligences/test_decision_rules.py::test_generic_choice_is_not_restricted_to_chat_reactions

- `tests/guildbotics/capabilities/test_decisions.py::test_recorded_response_effort_is_separate_from_judgment_settings` -> `移さない：#81の共有Python。新設の判定engineテストに同名関数を移した。`

  条件: 全元条件。Jevは推論portだけを代替、LLM/CLIはhost解決済definitionを渡してVMと同じBrainを構築。資格情報除去・1呼出・事前記録・戻り値metadata・effortの分離・記録失敗の両条件。

- `tests/guildbotics/capabilities/test_decisions.py::test_public_provider_thinking_settings_exclude_credentials` -> `移さない：#81の共有Python。新設の判定engineテストに同名関数を移した。`

  条件: 全元条件。Jevは推論portだけを代替、LLM/CLIはhost解決済definitionを渡してVMと同じBrainを構築。資格情報除去・1呼出・事前記録・戻り値metadata・effortの分離・記録失敗の両条件。

- `tests/guildbotics/capabilities/test_decisions.py::test_jev_uses_latest_and_records_the_returned_version` -> `移さない：#81の共有Python。新設の判定engineテストに同名関数を移した。`

  条件: 全元条件。Jevは推論portだけを代替、LLM/CLIはhost解決済definitionを渡してVMと同じBrainを構築。資格情報除去・1呼出・事前記録・戻り値metadata・effortの分離・記録失敗の両条件。

- `tests/guildbotics/capabilities/test_decisions.py::test_jev_rejects_other_models` -> `移さない：#81の共有Python。新設の判定engineテストに同名関数を移した。`

  条件: 全元条件。Jevは推論portだけを代替、LLM/CLIはhost解決済definitionを渡してVMと同じBrainを構築。資格情報除去・1呼出・事前記録・戻り値metadata・effortの分離・記録失敗の両条件。

- `tests/guildbotics/capabilities/test_decisions.py::test_jev_one_request_and_failure_is_sanitized` -> `移さない：#81の共有Python。新設の判定engineテストに同名関数を移した。`

  条件: 全元条件。Jevは推論portだけを代替、LLM/CLIはhost解決済definitionを渡してVMと同じBrainを構築。資格情報除去・1呼出・事前記録・戻り値metadata・effortの分離・記録失敗の両条件。

- `tests/guildbotics/capabilities/test_decisions.py::test_common_factory_run_and_execution_metadata` -> `移さない：#81の共有Python。新設の判定engineテストに同名関数を移した。`

  条件: 全元条件。Jevは推論portだけを代替、LLM/CLIはhost解決済definitionを渡してVMと同じBrainを構築。資格情報除去・1呼出・事前記録・戻り値metadata・effortの分離・記録失敗の両条件。

- `tests/guildbotics/capabilities/test_decisions.py::test_resolved_configuration_is_durable_before_failed_call` -> `移さない：#81の共有Python。新設の判定engineテストに同名関数を移した。`

  条件: 全元条件。Jevは推論portだけを代替、LLM/CLIはhost解決済definitionを渡してVMと同じBrainを構築。資格情報除去・1呼出・事前記録・戻り値metadata・effortの分離・記録失敗の両条件。

- `tests/guildbotics/capabilities/test_decisions.py::test_failed_configuration_record_does_not_call_model` -> `移さない：#81の共有Python assessmentの必須記録失敗検査へ移す。`

  条件: started/resolved/result全3段階で失敗→agent。started/resolved失敗では推論呼出0回、result失敗では既に1回。

- `tests/guildbotics/capabilities/test_decisions.py::test_jev_credentials_are_fresh_and_never_fall_back_to_environment` -> `#87：hostのDirectInferenceと環境変数fallbackを廃止済。VM判定は認証済gatewayだけを利用。`

  条件: このチケットで資格情報解決を再導入しない。#87のhost gateway/credential scope契約が引き継ぐ。

### tests/guildbotics/capabilities/test_workflow_completion_events.py

- `tests/guildbotics/capabilities/test_workflow_completion_events.py::test_abandoned_caps_attempt_count_at_max_attempts` -> `crates/gb-server/src/member/native.rs::native_chat_retry_events_keep_command_trace_rate_metadata_and_capped_abandonment`

  条件: 実Runtimeのログと診断/共有Activityを検査。person/command/source/trace/世代/chat属性、retryとabandon、予算超過入力99を上限3へ丸める。レート属性・reset・対象イベントも確認。

- `tests/guildbotics/capabilities/test_workflow_completion_events.py::test_abandoned_keeps_attempt_count_within_budget` -> `crates/gb-server/src/member/native.rs::native_chat_retry_events_keep_command_trace_rate_metadata_and_capped_abandonment`

  条件: 実Runtimeのログと診断/共有Activityを検査。person/command/source/trace/世代/chat属性、retryとabandon、予算超過入力99を上限3へ丸める。レート属性・reset・対象イベントも確認。

### tests/guildbotics/capabilities/test_workflow_rate_limits.py

- `tests/guildbotics/capabilities/test_workflow_rate_limits.py::test_workflow_rate_limit_from_exception_extracts_from_cli_agent_error` -> `crates/gb-server/src/ticket_selector/tests.rs::rate_limit_envelopes_accept_direct_and_exhausted_cli_errors_but_not_other_categories`

  条件: 直CLI/CompletionRetryExhausted × rate/timeout/unknownのwire分類。plain例外にはCLI分類を付けないVM検査も併用。

- `tests/guildbotics/capabilities/test_workflow_rate_limits.py::test_workflow_rate_limit_from_exception_extracts_from_exhausted_turn_last_error` -> `crates/gb-server/src/ticket_selector/tests.rs::rate_limit_envelopes_accept_direct_and_exhausted_cli_errors_but_not_other_categories`

  条件: 直CLI/CompletionRetryExhausted × rate/timeout/unknownのwire分類。plain例外にはCLI分類を付けないVM検査も併用。

- `tests/guildbotics/capabilities/test_workflow_rate_limits.py::test_workflow_rate_limit_from_exception_returns_none_for_different_category` -> `crates/gb-server/src/ticket_selector/tests.rs::rate_limit_envelopes_accept_direct_and_exhausted_cli_errors_but_not_other_categories`

  条件: 直CLI/CompletionRetryExhausted × rate/timeout/unknownのwire分類。plain例外にはCLI分類を付けないVM検査も併用。

- `tests/guildbotics/capabilities/test_workflow_rate_limits.py::test_workflow_rate_limit_from_exception_returns_none_for_plain_exception` -> `crates/gb-server/src/ticket_selector/tests.rs::rate_limit_envelopes_accept_direct_and_exhausted_cli_errors_but_not_other_categories`

  条件: 直CLI/CompletionRetryExhausted × rate/timeout/unknownのwire分類。plain例外にはCLI分類を付けないVM検査も併用。

- `tests/guildbotics/capabilities/test_workflow_rate_limits.py::test_notice_explains_the_workflow_retry` -> `crates/gb-server/src/ticket_selector/tests.rs::every_rate_limit_reset_hint_and_retry_guidance_renders_in_both_languages`

  条件: en/ja × ticket/chat × 有効offset/Z・hint・空・不正・naive。Tokyo表示でexact文、reset優先・retry案内・unknownを確認。

- `tests/guildbotics/capabilities/test_workflow_rate_limits.py::test_notice_selects_reset_information` -> `crates/gb-server/src/ticket_selector/tests.rs::every_rate_limit_reset_hint_and_retry_guidance_renders_in_both_languages`

  条件: en/ja × ticket/chat × 有効offset/Z・hint・空・不正・naive。Tokyo表示でexact文、reset優先・retry案内・unknownを確認。

- `tests/guildbotics/capabilities/test_workflow_rate_limits.py::test_record_workflow_rate_limited` -> `crates/gb-server/src/member/native.rs::native_chat_retry_events_keep_command_trace_rate_metadata_and_capped_abandonment`

  条件: 実Runtimeのログと診断/共有Activityを検査。person/command/source/trace/世代/chat属性、retryとabandon、予算超過入力99を上限3へ丸める。レート属性・reset・対象イベントも確認。

### tests/scripts/test_evaluate_chat_decision.py

- `tests/scripts/test_evaluate_chat_decision.py::test_a_case_set_is_judged_by_the_members_configured_brain` -> `移さない：#81の評価道具は共有Pythonとして復帰。`

  条件: 実行時設定をVMへ渡すreplayをPythonで検査。実モデルの実機呼出は行わない。

### tests/guildbotics/templates/commands/workflows/test_workflow_contract.py

- `tests/guildbotics/templates/commands/workflows/test_workflow_contract.py::test_contract_resolves_with_required_tokens_in_both_languages` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

- `tests/guildbotics/templates/commands/workflows/test_workflow_contract.py::test_contract_token_sets_match_across_languages` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

- `tests/guildbotics/templates/commands/workflows/test_workflow_contract.py::test_contract_excludes_completion_command_forms` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

- `tests/guildbotics/templates/commands/workflows/test_workflow_contract.py::test_contract_renders_all_placeholders` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

### tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py

- `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py::test_workflow_runs_one_turn_with_the_host_selected_ticket` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

- `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py::test_a_resumed_run_continues_the_awaited_work_and_its_session` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

- `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py::test_a_new_run_continues_its_works_session_without_a_checks_wait` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

- `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py::test_mention_work_passes_the_response_only_work_type` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

- `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py::test_workflow_passes_pull_request_work_type` -> `移さない：VM内の共有Python workflowを継続使用し、同名の既存Pythonテストを維持。`

  条件: 凍結版と同名テストのassert/パラメータ条件を維持。host選択はRust Native journeyで別途検査。

- `tests/guildbotics/templates/commands/workflows/test_ticket_driven_workflow.py::test_a_ticket_is_selected_moved_worked_and_completed_on_the_local_board` -> `crates/gb-server/src/member/native.rs::native_ticket_patrol_moves_the_local_ticket_and_accepts_real_member_comment_and_completion`

  条件: mention有無×linked PR有無4条件。選択理由、lane/assignee維持または移動、実member comment/complete、既存PR再利用、同じPRだけ1件と完了後候補無し。

### tests/guildbotics/entities/test_scheduled_command.py

- `tests/guildbotics/entities/test_scheduled_command.py::test_should_run_simple_random` -> `crates/gb-server/src/schedule.rs::random_occurrences_wait_until_the_sample_run_once_and_render_the_exact_state`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_should_run_range` -> `crates/gb-server/src/schedule.rs::random_occurrences_wait_until_the_sample_run_once_and_render_the_exact_state`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_should_run_boundary` -> `crates/gb-server/src/schedule.rs::occurrence_runs_once_defers_without_consuming_and_resets_at_boundary`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_should_run_month_day_clamp` -> `crates/gb-server/src/schedule.rs::random_default_and_explicit_ranges_preserve_clamp_and_weekday_order`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_should_run_weekday` -> `crates/gb-server/src/schedule.rs::five_fields_and_random_bounds_are_required_and_sunday_uses_zero`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_invalid_schedule` -> `crates/gb-server/src/schedule.rs::five_fields_and_random_bounds_are_required_and_sunday_uses_zero`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_day_of_month_clamp_direct` -> `crates/gb-server/src/schedule.rs::random_default_and_explicit_ranges_preserve_clamp_and_weekday_order`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_sample_random_weekday_range` -> `crates/gb-server/src/schedule.rs::random_default_and_explicit_ranges_preserve_clamp_and_weekday_order`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_should_run_reset_cycle` -> `crates/gb-server/src/schedule.rs::occurrence_runs_once_defers_without_consuming_and_resets_at_boundary`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_sample_random_default_range` -> `crates/gb-server/src/schedule.rs::random_default_and_explicit_ranges_preserve_clamp_and_weekday_order`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

- `tests/guildbotics/entities/test_scheduled_command.py::test_str_representation` -> `crates/gb-server/src/schedule.rs::random_occurrences_wait_until_the_sample_run_once_and_render_the_exact_state`

  条件: 凍結と同じ日付・乱数15/3・March31→February28・February31 clamp・曜日2..4で過去木曜へ。乱数発火の待機/一度だけ/次cycle、4/6field拒否、Sunday0、文字列表現全文。外部cron/RNGのmockを固定したsample/境界へ置換。

### tests/guildbotics/drivers/test_utils.py

- `tests/guildbotics/drivers/test_utils.py::test_run_command_success_logs_and_returns_true` -> `crates/gb-server/src/member/native.rs::native_commands_keep_trace_and_journal_in_sync_when_only_autonomous_service_outages_defer`

  条件: manual/routine/scheduled、Failed/NotFound/Unsupported/Unavailableを実Driver経由で検査。成功/失敗/見送りはtraceとjournal一致、callerへ例外伝搬。

- `tests/guildbotics/drivers/test_utils.py::test_run_command_exception_logs_and_reraises` -> `crates/gb-server/src/member/native.rs::native_commands_keep_trace_and_journal_in_sync_when_only_autonomous_service_outages_defer`

  条件: manual/routine/scheduled、Failed/NotFound/Unsupported/Unavailableを実Driver経由で検査。成功/失敗/見送りはtraceとjournal一致、callerへ例外伝搬。

- `tests/guildbotics/drivers/test_utils.py::test_command_failure_preserves_structured_authentication_cause` -> `crates/gb-server/src/dispatcher.rs::wrapped_cli_authentication_and_explicit_cancellation_keep_their_alert_codes`

  条件: authentication/network/rate limit全3category、wrapped CommandErrorの認証だけ警報code維持、明示取消codeも検査。

- `tests/guildbotics/drivers/test_utils.py::test_an_exchange_directory_it_may_not_create_is_told_as_the_environment_tells_it` -> `#87：交換directoryの検証とOSのDocuments拒否の案内は既存host path/隔離環境境界。`

  条件: Dispatcherは同じhost path検証を呼び、そのエラーをEnvironment型で返す。独自のdirectory fallbackを作らない。

### tests/guildbotics/capabilities/test_work_stops.py

- `tests/guildbotics/capabilities/test_work_stops.py::test_comment_url_of_names_the_members_latest_comment` -> `crates/gb-server/src/work_stops.rs::latest_comment_on_the_subject_precedes_comments_elsewhere`

  条件: 同じ対象の最新を優先、対象無しは他所の最新、全5種のコメント、空証拠・URL無し・create/reaction除外。

- `tests/guildbotics/capabilities/test_work_stops.py::test_the_notice_is_posted_once_only_in_selected_channels` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: en/ja×asking/blocked/failed/review limit×コメントURL有無。名前とIDの重複は1回、2対象channel、無効/非通知/旧設定channelのcanaryは空。本文全文・投稿者・status全体を比較。失敗/成功/未解決が独立に記録される。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_stop_without_a_comment_address_links_only_its_item` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: en/ja×asking/blocked/failed/review limit×コメントURL有無。名前とIDの重複は1回、2対象channel、無効/非通知/旧設定channelのcanaryは空。本文全文・投稿者・status全体を比較。失敗/成功/未解決が独立に記録される。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_channel_named_only_by_its_name_is_resolved` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: en/ja×asking/blocked/failed/review limit×コメントURL有無。名前とIDの重複は1回、2対象channel、無効/非通知/旧設定channelのcanaryは空。本文全文・投稿者・status全体を比較。失敗/成功/未解決が独立に記録される。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_channel_that_cannot_be_resolved_is_recorded_undelivered` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: en/ja×asking/blocked/failed/review limit×コメントURL有無。名前とIDの重複は1回、2対象channel、無効/非通知/旧設定channelのcanaryは空。本文全文・投稿者・status全体を比較。失敗/成功/未解決が独立に記録される。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_member_with_nowhere_to_say_it_posts_nothing` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: en/ja×asking/blocked/failed/review limit×コメントURL有無。名前とIDの重複は1回、2対象channel、無効/非通知/旧設定channelのcanaryは空。本文全文・投稿者・status全体を比較。失敗/成功/未解決が独立に記録される。

- `tests/guildbotics/capabilities/test_work_stops.py::test_existing_channels_without_a_notification_setting_post_nothing` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: en/ja×asking/blocked/failed/review limit×コメントURL有無。名前とIDの重複は1回、2対象channel、無効/非通知/旧設定channelのcanaryは空。本文全文・投稿者・status全体を比較。失敗/成功/未解決が独立に記録される。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_selected_name_and_id_resolving_to_the_same_channel_post_once` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: en/ja×asking/blocked/failed/review limit×コメントURL有無。名前とIDの重複は1回、2対象channel、無効/非通知/旧設定channelのcanaryは空。本文全文・投稿者・status全体を比較。失敗/成功/未解決が独立に記録される。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_failed_post_is_recorded_and_not_raised` -> `crates/gb-server/src/work_stops.rs::notification_matrix_preserves_language_status_scope_and_safe_delivery_failures`

  条件: en/ja×asking/blocked/failed/review limit×コメントURL有無。名前とIDの重複は1回、2対象channel、無効/非通知/旧設定channelのcanaryは空。本文全文・投稿者・status全体を比較。失敗/成功/未解決が独立に記録される。

- `tests/guildbotics/capabilities/test_work_stops.py::test_an_unexpected_failure_records_only_its_kind` -> `移さない：providerから任意のPython例外と機密messageを投げる仕組みは廃止。RustのChatServiceは閉じたServiceErrorだけを返す。`

  条件: 通知のerrorは安全なlocale keyのみ。通常失敗後も他channelへ届くことを通知matrixで検査。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_record_that_cannot_be_written_does_not_raise` -> `#924：診断の永続化失敗を呼出側へ返さず警告にする既存Diagnosticsの責務。`

  条件: 通知側のobserverはResultを返さない。通知後に独自の永続化層や例外型を再導入しない。

- `tests/guildbotics/capabilities/test_work_stops.py::test_every_completion_and_status_is_classified` -> `移さない：PythonのLiteralから全候補を取り出す仕組みはRustのenumへ置換。`

  条件: completion全4種はRustの網羅match、通知4理由は通知matrix、制限・checksはselectorの非通知検査へ分解。新enum値は網羅matchを更新しない限りコンパイルが通らない。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_completion_is_a_stop_only_when_it_waits_for_a_person` -> `crates/gb-server/src/work_stops.rs::only_completed_waits_exclusively_for_active_agents_suppress_a_stop`

  条件: 全4completionをexhaustive matchで分類、対象はactive agent/human/inactive/unknown/空/mixed。asking/blockedだけ人待ちの通知、完了無し・未終了は通知無し。

- `tests/guildbotics/capabilities/test_work_stops.py::test_a_run_that_recorded_no_completion_is_no_stop` -> `crates/gb-server/src/work_stops.rs::only_completed_waits_exclusively_for_active_agents_suppress_a_stop`

  条件: 全4completionをexhaustive matchで分類、対象はactive agent/human/inactive/unknown/空/mixed。asking/blockedだけ人待ちの通知、完了無し・未終了は通知無し。

- `tests/guildbotics/capabilities/test_work_stops.py::test_only_the_ticket_selector_announces_a_stop` -> `移さない：PythonのASTから関数名の呼出元を探す検査。RustではSelectorからRunnerの通知portを呼び、Runtimeが配信する。`

  条件: グローバル関数の探索は設計から削除。外からの通知回数はselectorとlocal chatの検査で固定。

### tests/guildbotics/utils/test_keep_awake.py

- `tests/guildbotics/utils/test_keep_awake.py::test_a_hold_is_held_from_start_until_stop` -> `crates/gb-server/src/keep_awake.rs::independent_holds_start_synchronously_and_release_from_another_thread`

  条件: 起動がhold取得後に返る、独立2個→別スレッドでdropし1個→dropで0個。Dropはwatcherをjoinするため終了後の監視task無し。OSへの実抑止は行わず凍結wakepy fake同様のfactoryで確認。

- `tests/guildbotics/utils/test_keep_awake.py::test_starting_and_stopping_twice_is_one_hold` -> `移さない：start/stopを繰返す可変KeepAwake APIを廃止し、所有権を持つFollowの構築とDropだけにした。`

  条件: 同一holdを2回終了する状態はRust所有権で構築できない。サービスの重複start/stopはHTTPテストで検査。

- `tests/guildbotics/utils/test_keep_awake.py::test_another_thread_can_stop_a_hold` -> `crates/gb-server/src/keep_awake.rs::independent_holds_start_synchronously_and_release_from_another_thread`

  条件: 起動がhold取得後に返る、独立2個→別スレッドでdropし1個→dropで0個。Dropはwatcherをjoinするため終了後の監視task無し。OSへの実抑止は行わず凍結wakepy fake同様のfactoryで確認。

- `tests/guildbotics/utils/test_keep_awake.py::test_holds_are_independent` -> `crates/gb-server/src/keep_awake.rs::independent_holds_start_synchronously_and_release_from_another_thread`

  条件: 起動がhold取得後に返る、独立2個→別スレッドでdropし1個→dropで0個。Dropはwatcherをjoinするため終了後の監視task無し。OSへの実抑止は行わず凍結wakepy fake同様のfactoryで確認。

- `tests/guildbotics/utils/test_keep_awake.py::test_the_block_is_held_and_released_however_it_ends` -> `crates/gb-server/src/keep_awake.rs::unsupported_keep_awake_warns_once_and_unwinding_always_releases_a_hold`

  条件: 失敗factoryでも処理継続・hold0・2度の起動で警告1回。panic unwindで取得済holdをDropしjoin後hold0。

- `tests/guildbotics/utils/test_keep_awake.py::test_a_machine_that_cannot_be_held_awake_runs_on_and_warns_once` -> `crates/gb-server/src/keep_awake.rs::unsupported_keep_awake_warns_once_and_unwinding_always_releases_a_hold`

  条件: 失敗factoryでも処理継続・hold0・2度の起動で警告1回。panic unwindで取得済holdをDropしjoin後hold0。

### tests/guildbotics/integrations/test_chat_state_store.py

- `tests/guildbotics/integrations/test_chat_state_store.py::test_channel_cursor_roundtrip` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_mark_processed_event_and_dedupes` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_processed_event_limit_keeps_most_recent_unique` -> `crates/gb-server/src/chat_state.rs::acknowledgement_larger_than_bounded_history_removes_whole_batch_and_keeps_future_input`

  条件: 履歴上限2に対し3件ackしてE2/E3を保持、queue全除去、あとから来たE4は保持。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_thread_state_roundtrip` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_load_missing_state_returns_defaults` -> `移さない：Pythonの状態dataclassの初期値を直接検査するテスト。Rustの内部読取は空JSON、各利用先が既定値を設定する。`

  条件: 空thread/cursor/cacheは状態往復テストで検査。payloadで返す既定topic/focus/handoffはchat prompt検査へ分離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_thread_system_notices_roundtrip` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_state_is_isolated_per_person_and_channel` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_thread_messages_roundtrip_replace_and_trim` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_list_thread_states_returns_known_threads_for_channel` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_append_thread_message_splits_cache_from_shared_state` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_pending_events_roundtrip_dedupe_and_remove` -> `crates/gb-server/src/chat_state.rs::redelivery_retains_attempts_and_unrelated_queue_entries_and_cursor_updates_do_not_lose_acknowledgements`

  条件: redeliveryで本文・参加設定だけ更新、3試行とrun・次回時刻維持、別入力を残して対象ack、watermark更新後も処理済ID維持。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_pending_event_status_and_retry_state_roundtrip` -> `crates/gb-server/src/chat_state.rs::pending_status_retry_metadata_and_channel_enumeration_preserve_member_scope`

  条件: status無し/レート制限status有りの両方、全retry項目のJSON完全往復。2member/2channel/別serviceを隔離して列挙。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_pending_event_reader_does_not_drop_missing_identity_fields` -> `crates/gb-server/src/chat_state.rs::malformed_pending_status_and_each_missing_identity_are_errors_and_preserve_the_file`

  条件: ID3種・時刻の各欠落と不正statusの全条件で読込/新規保存とも拒否し元fileのbytesを保持。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_a_pending_event_without_a_status_reads_back_without_one` -> `crates/gb-server/src/chat_state.rs::pending_status_retry_metadata_and_channel_enumeration_preserve_member_scope`

  条件: status無し/レート制限status有りの両方、全retry項目のJSON完全往復。2member/2channel/別serviceを隔離して列挙。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_a_pending_event_with_an_invalid_status_is_a_record_error` -> `crates/gb-server/src/chat_state.rs::malformed_pending_status_and_each_missing_identity_are_errors_and_preserve_the_file`

  条件: ID3種・時刻の各欠落と不正statusの全条件で読込/新規保存とも拒否し元fileのbytesを保持。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_list_pending_channels` -> `crates/gb-server/src/chat_state.rs::pending_status_retry_metadata_and_channel_enumeration_preserve_member_scope`

  条件: status無し/レート制限status有りの両方、全retry項目のJSON完全往復。2member/2channel/別serviceを隔離して列挙。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_list_known_channels_unions_cursor_pending_and_threads` -> `#91：chat設定APIの受信backlogリセットと既知channel列挙。`

  条件: このrunのreceiverは既存cutoffを読む。画面からの設定変更/リセットの書込は#91のAPI範囲。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_receive_cutoff_roundtrip` -> `crates/gb-server/src/chat_state.rs::thread_state_cache_and_channel_cursors_roundtrip_without_crossing_scopes`

  条件: 全thread項目・handoff・noticeを往復、cursor watermarkと処理済IDを共存、service/person/channelの3軸で隔離。cacheの編集置換と2件trim、botフラグ、本文canaryが共有stateに無いこと、既知thread列挙、cutoffのmember隔離。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_clear_channel_receive_backlog_drops_pending_and_threads` -> `#91：chat設定APIの受信backlogリセットと既知channel列挙。`

  条件: このrunのreceiverは既存cutoffを読む。画面からの設定変更/リセットの書込は#91のAPI範囲。

- `tests/guildbotics/integrations/test_chat_state_store.py::test_clear_channel_receive_backlog_empties_pending_when_unlink_fails` -> `#91：chat設定APIの受信backlogリセットと既知channel列挙。`

  条件: このrunのreceiverは既存cutoffを読む。画面からの設定変更/リセットの書込は#91のAPI範囲。

### tests/guildbotics/cli/test_service_api.py

- `tests/guildbotics/cli/test_service_api.py::test_service_api_preserves_error_reason` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_api.py::test_malformed_api_error_preserves_the_http_failure` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_api.py::test_non_json_error_preserves_the_http_failure` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_api.py::test_commands_show_the_api_reason_without_retry` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

### tests/guildbotics/cli/test_service_process.py

- `tests/guildbotics/cli/test_service_process.py::test_cli_stop_ends_start_successfully_and_closes_the_server` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_process.py::test_a_forced_stop_ends_the_server_even_while_work_holds_it` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_process.py::test_services_that_stop_or_fail_leave_the_server_serving` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_process.py::test_the_server_starts_alone_unless_the_setting_says_otherwise` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_process.py::test_a_first_server_starts_before_any_workspace_is_selected` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_process.py::test_a_second_start_is_refused_with_its_reason` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_process.py::test_forced_parent_exit_is_followed_by_the_backend_watchdog` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_process.py::test_two_ctrl_c_drain_then_cancel_the_work` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。

- `tests/guildbotics/cli/test_service_process.py::test_sigterm_to_start_alone_cancels_the_work_at_once` -> `#92：CLIのservice client/別process起動・signal・watchdogの黒箱検査。`

  条件: チケット本文の分担どおりCLI完成後に実行。Rust HTTP開始停止、重複起動、認証拒否、設定破損、graceful/force、親serverの独立はこのPRのservice/serverテストで確認。
