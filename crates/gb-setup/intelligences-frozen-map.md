# Intelligence API frozen test correspondence

Frozen: `origin/python-host` (`fea9e05aaf8b4bca96e19d615b6811ea0eacb7d0`).

Rust の既存保存形式に合わせ、brain は `kind/model/cli_agent` で保存し、API は凍結版の `brain_class/engine/target` を返す。保存JSONは既存writerがキーをsortするため、往復テストではbrainを名前で選ぶ（凍結版の先頭行への変更と同じdefault featureを変更する）。

## test_intelligences.py

- tests/guildbotics/app_api/test_intelligences.py::test_saving_a_protected_parent_refuses_the_whole_update -> crates/gb-server/src/config_api/tests/intelligences.rs::protected_parent_and_missing_local_grants_refuse_the_whole_save — 登録した別 workspace の親と不存在 local の両方で全体拒否。設定 bytes と未作成の document/local file を照合。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_template_fallback_when_team_config_absent -> crates/gb-setup/src/intelligences_tests.rs::inherited_slots_and_member_differences_are_reconciled — template の models/brains、member の継承と差分、削除の action と継承復帰。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_member_without_override_is_inherited -> crates/gb-setup/src/intelligences_tests.rs::inherited_slots_and_member_differences_are_reconciled — template の models/brains、member の継承と差分、削除の action と継承復帰。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_member_marks_team_owned_slots -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_team_has_no_inherited_slots -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_member_with_override_not_inherited -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_deduplicates_model_file_reads -> 移さない：Python load_yaml_file の呼出し回数という内部機構。 — 返すdefinitionの重複排除は malformed_definitions_and_unknown_catalog_mappings_are_reported で検証。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_handles_malformed_yaml -> crates/gb-setup/src/intelligences_tests.rs::malformed_definitions_and_unknown_catalog_mappings_are_reported — scalar model/sequence CLIを空設定として表示。unknown toolのcode、slot/path入り文、保存しないこと。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_rejects_a_mapping_outside_the_catalog -> crates/gb-setup/src/intelligences_tests.rs::malformed_definitions_and_unknown_catalog_mappings_are_reported — scalar model/sequence CLIを空設定として表示。unknown toolのcode、slot/path入り文、保存しないこと。
- tests/guildbotics/app_api/test_intelligences.py::test_update_config_rejects_a_mapping_outside_the_catalog -> crates/gb-setup/src/intelligences_tests.rs::malformed_definitions_and_unknown_catalog_mappings_are_reported — scalar model/sequence CLIを空設定として表示。unknown toolのcode、slot/path入り文、保存しないこと。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_cli_agent_malformed_definition_falls_back -> crates/gb-setup/src/intelligences_tests.rs::malformed_definitions_and_unknown_catalog_mappings_are_reported — scalar model/sequence CLIを空設定として表示。unknown toolのcode、slot/path入り文、保存しないこと。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_brain_mapping_engine_classification -> crates/gb-setup/src/intelligences_tests.rs::brain_rows_classify_engines_and_skip_non_mapping_entries — model/ai_cli/jevを各API engine/class/targetに変換し、非mappingの行は除く。保存形式はRustのkind形式。
- tests/guildbotics/app_api/test_intelligences.py::test_team_update_writes_all_files -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_team_environment_update_preserves_all_intelligence_settings -> crates/gb-server/src/config_api/tests/intelligences.rs::team_environment_save_preserves_omitted_settings_and_member_cannot_write_it — 省略された既存bytes保持・3環境ファイルの保存先・default declaration・grants/declarationのmember共通読取とteam限定保存・省略値保持。
- tests/guildbotics/app_api/test_intelligences.py::test_team_update_merges_existing_model_file -> crates/gb-setup/src/intelligences_tests.rs::model_parameters_and_native_empty_effort_survive_save — class/id更新、temperature/rate_limit保持、表示対象reasoning_effortの削除、CLIの空parameters/effortを明示保存。
- tests/guildbotics/app_api/test_intelligences.py::test_member_override_writes_only_changed_slots -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_member_override_writes_changed_brain_assignment_only -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_member_override_prunes_reverted_slot -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_member_override_preserves_unsurfaced_def_fields -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_inherit_team_defaults_deletes_member_intelligences -> crates/gb-setup/src/intelligences_tests.rs::inherited_slots_and_member_differences_are_reconciled — template の models/brains、member の継承と差分、削除の action と継承復帰。
- tests/guildbotics/app_api/test_intelligences.py::test_inherit_team_defaults_when_no_member_dir -> crates/gb-setup/src/intelligences_tests.rs::inherited_slots_and_member_differences_are_reconciled — template の models/brains、member の継承と差分、削除の action と継承復帰。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_surfaces_the_model_effort_overlay -> crates/gb-setup/src/intelligences_tests.rs::effort_round_trips_nested_types_and_rejects_invalid_input_before_writes — Anthropicのnested enum/integerとlow/highを型を保って往復。unknown/default level、設定typo、enum違反、integerのbool/stringを拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_surfaces_a_tools_effort_only_definition -> crates/gb-server/src/config_api/tests/intelligences.rs::native_definition_fields_and_empty_effort_are_not_refilled — CLI effortの表示・parameters/effortのみ保存・network非露出/除去・空effortのruntime解決・保存後descriptor保持とtypo拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_team_update_writes_the_model_effort_overlay_with_types_intact -> crates/gb-setup/src/intelligences_tests.rs::effort_round_trips_nested_types_and_rejects_invalid_input_before_writes — Anthropicのnested enum/integerとlow/highを型を保って往復。unknown/default level、設定typo、enum違反、integerのbool/stringを拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_team_update_writes_a_tools_effort_and_nothing_else -> crates/gb-server/src/config_api/tests/intelligences.rs::native_definition_fields_and_empty_effort_are_not_refilled — CLI effortの表示・parameters/effortのみ保存・network非露出/除去・空effortのruntime解決・保存後descriptor保持とtypo拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_emptying_a_native_effort_keeps_an_explicit_empty_override -> crates/gb-setup/src/intelligences_tests.rs::model_parameters_and_native_empty_effort_survive_save — class/id更新、temperature/rate_limit保持、表示対象reasoning_effortの削除、CLIの空parameters/effortを明示保存。
- tests/guildbotics/app_api/test_intelligences.py::test_an_emptied_native_effort_is_not_refilled_by_the_template -> crates/gb-server/src/config_api/tests/intelligences.rs::native_definition_fields_and_empty_effort_are_not_refilled — CLI effortの表示・parameters/effortのみ保存・network非露出/除去・空effortのruntime解決・保存後descriptor保持とtypo拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_member_override_keeps_only_a_differing_native_effort -> crates/gb-setup/src/intelligences_tests.rs::complete_team_save_and_member_pruning_preserve_hidden_definition_fields — 5保存ファイルと内容・scope別のlocked names・2件だけの差分保存・translateのみのbrain差分・revert時のprune・rate_limit保持・異なるCLI effortのみ保存。
- tests/guildbotics/app_api/test_intelligences.py::test_a_definition_with_an_unknown_effort_level_is_rejected -> crates/gb-setup/src/intelligences_tests.rs::effort_round_trips_nested_types_and_rejects_invalid_input_before_writes — Anthropicのnested enum/integerとlow/highを型を保って往復。unknown/default level、設定typo、enum違反、integerのbool/stringを拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_a_slot_without_a_template_still_reports_what_it_inherits -> crates/gb-setup/src/intelligences_tests.rs::native_catalog_and_shadowed_provider_defaults_keep_typed_fields — openai/geminiのshadowed defaultと別slot、継承low/highの具体値、全5 CLIのfield集合・effort_supported・Grok enum値。
- tests/guildbotics/app_api/test_intelligences.py::test_saving_a_setting_the_provider_does_not_accept_is_refused -> crates/gb-setup/src/intelligences_tests.rs::effort_round_trips_nested_types_and_rejects_invalid_input_before_writes — Anthropicのnested enum/integerとlow/highを型を保って往復。unknown/default level、設定typo、enum違反、integerのbool/stringを拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_a_definition_saved_before_effort_existed_still_offers_typed_editing -> crates/gb-setup/src/intelligences_tests.rs::native_catalog_and_shadowed_provider_defaults_keep_typed_fields — openai/geminiのshadowed defaultと別slot、継承low/highの具体値、全5 CLIのfield集合・effort_supported・Grok enum値。
- tests/guildbotics/app_api/test_intelligences.py::test_such_a_definition_still_rejects_a_setting_the_provider_refuses -> crates/gb-setup/src/intelligences_tests.rs::effort_round_trips_nested_types_and_rejects_invalid_input_before_writes — Anthropicのnested enum/integerとlow/highを型を保って往復。unknown/default level、設定typo、enum違反、integerのbool/stringを拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_a_second_slot_gets_descriptors_even_when_the_provider_default_is_shadowed -> crates/gb-setup/src/intelligences_tests.rs::native_catalog_and_shadowed_provider_defaults_keep_typed_fields — openai/geminiのshadowed defaultと別slot、継承low/highの具体値、全5 CLIのfield集合・effort_supported・Grok enum値。
- tests/guildbotics/app_api/test_intelligences.py::test_a_saved_native_tool_keeps_its_typed_editing_and_validation -> crates/gb-server/src/config_api/tests/intelligences.rs::native_definition_fields_and_empty_effort_are_not_refilled — CLI effortの表示・parameters/effortのみ保存・network非露出/除去・空effortのruntime解決・保存後descriptor保持とtypo拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_every_native_tool_declares_the_settings_its_adapter_applies -> crates/gb-setup/src/intelligences_tests.rs::native_catalog_and_shadowed_provider_defaults_keep_typed_fields — openai/geminiのshadowed defaultと別slot、継承low/highの具体値、全5 CLIのfield集合・effort_supported・Grok enum値。
- tests/guildbotics/app_api/test_intelligences.py::test_every_shipped_tool_declares_its_own_effort_capability -> crates/gb-setup/src/intelligences_tests.rs::native_catalog_and_shadowed_provider_defaults_keep_typed_fields — openai/geminiのshadowed defaultと別slot、継承low/highの具体値、全5 CLIのfield集合・effort_supported・Grok enum値。
- tests/guildbotics/app_api/test_intelligences.py::test_a_hand_tuned_setting_the_editor_never_shows_survives_a_save -> crates/gb-setup/src/intelligences_tests.rs::model_parameters_and_native_empty_effort_survive_save — class/id更新、temperature/rate_limit保持、表示対象reasoning_effortの削除、CLIの空parameters/effortを明示保存。
- tests/guildbotics/app_api/test_intelligences.py::test_tool_definition_network_is_not_part_of_the_api_or_a_saved_definition -> crates/gb-server/src/config_api/tests/intelligences.rs::native_definition_fields_and_empty_effort_are_not_refilled — CLI effortの表示・parameters/effortのみ保存・network非露出/除去・空effortのruntime解決・保存後descriptor保持とtypo拒否。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_returns_the_grants -> crates/gb-server/src/config_api/tests/intelligences.rs::scope_environment_reads_include_shared_device_files_and_declaration — 省略された既存bytes保持・3環境ファイルの保存先・default declaration・grants/declarationのmember共通読取とteam限定保存・省略値保持。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_returns_no_shared_grants_when_no_file_exists -> crates/gb-server/src/config_api/tests/intelligences.rs::team_environment_save_preserves_omitted_settings_and_member_cannot_write_it — 省略された既存bytes保持・3環境ファイルの保存先・default declaration・grants/declarationのmember共通読取とteam限定保存・省略値保持。
- tests/guildbotics/app_api/test_intelligences.py::test_read_config_returns_the_declaration_or_the_template -> crates/gb-server/src/config_api/tests/intelligences.rs::scope_environment_reads_include_shared_device_files_and_declaration — 省略された既存bytes保持・3環境ファイルの保存先・default declaration・grants/declarationのmember共通読取とteam限定保存・省略値保持。
- tests/guildbotics/app_api/test_intelligences.py::test_a_malformed_declaration_is_reported_where_it_is_edited -> crates/gb-server/src/config_api/tests/intelligences.rs::malformed_environment_files_are_reported_at_the_editor — 未知packagesをinvalid_agent_environment、絶対documentをinvalid_filesystem_grantsとして編集画面で返す。
- tests/guildbotics/app_api/test_intelligences.py::test_a_team_save_replaces_the_declaration_and_an_omitted_value_keeps_it -> crates/gb-server/src/config_api/tests/intelligences.rs::team_environment_save_preserves_omitted_settings_and_member_cannot_write_it — 省略された既存bytes保持・3環境ファイルの保存先・default declaration・grants/declarationのmember共通読取とteam限定保存・省略値保持。
- tests/guildbotics/app_api/test_intelligences.py::test_a_team_save_replaces_the_grants_and_an_omitted_value_keeps_them -> crates/gb-server/src/config_api/tests/intelligences.rs::team_environment_save_preserves_omitted_settings_and_member_cannot_write_it — 省略された既存bytes保持・3環境ファイルの保存先・default declaration・grants/declarationのmember共通読取とteam限定保存・省略値保持。
- tests/guildbotics/app_api/test_intelligences.py::test_a_malformed_grants_file_is_reported_where_it_is_edited -> crates/gb-server/src/config_api/tests/intelligences.rs::malformed_environment_files_are_reported_at_the_editor — 未知packagesをinvalid_agent_environment、絶対documentをinvalid_filesystem_grantsとして編集画面で返す。
- tests/guildbotics/app_api/test_intelligences.py::test_slots_do_not_expose_tool_network_settings -> crates/gb-server/src/config_api/tests/intelligences.rs::native_definition_fields_and_empty_effort_are_not_refilled — CLI effortの表示・parameters/effortのみ保存・network非露出/除去・空effortのruntime解決・保存後descriptor保持とtypo拒否。

## test_config_revisions_api.py

- tests/guildbotics/app_api/test_config_revisions_api.py::test_intelligence_api_rejects_invalid_member_before_any_read_write_or_delete -> crates/gb-server/src/config_api/tests/intelligences.rs::invalid_member_ids_are_rejected_before_read_save_or_delete — 凍結版10 ID値をGET/PUTで422、sentinel含む全config bytes保持。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_member_stored_under_no_valid_id_is_listed_and_can_be_removed -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_an_identity_naming_another_member_is_repaired_by_saving_it -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_invalid_member_data_remains_listed_and_repairable -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_directories_without_member_config_are_not_runtime_members -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_colliding_members_are_repairable_without_changing_shared_secrets -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_candidate_namespace_conflict_offers_a_different_id -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_repair_read_requires_an_actual_stored_member -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_project_read_reports_the_revision_of_every_file_it_used -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_project_save_at_the_reported_revisions_applies -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_project_save_from_a_stale_screen_keeps_the_newer_content -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_refusal_reports_the_revisions_to_reload_with -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_stale_file_the_user_never_edited_still_refuses_the_save -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_first_time_setup_saves_without_revisions -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_member_read_reports_its_revision_and_a_stale_save_is_refused -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_an_intelligence_read_covers_the_whole_directory_it_reconciles -> crates/gb-server/src/config_api/tests/intelligences.rs::intelligence_http_reads_saves_and_resets_member_differences — HTTPでteam保存→member差分1ファイル→継承reset。tree revision集合・保存後revisionと次の保存・event非発行。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_an_intelligence_save_is_refused_when_the_directory_moved -> crates/gb-server/src/config_api/tests/intelligences.rs::intelligence_http_stale_content_additions_and_inherited_tree_refuse_save — 既存content変更・新規追加・継承memberのディレクトリ出現の3条件で409/config_changed、到着bytesを保持。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_an_intelligence_save_is_refused_when_a_file_was_added_to_the_directory -> crates/gb-server/src/config_api/tests/intelligences.rs::intelligence_http_stale_content_additions_and_inherited_tree_refuse_save — 既存content変更・新規追加・継承memberのディレクトリ出現の3条件で409/config_changed、到着bytesを保持。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_member_inheriting_the_team_defaults_is_still_guarded -> crates/gb-server/src/config_api/tests/intelligences.rs::intelligence_http_stale_content_additions_and_inherited_tree_refuse_save — 既存content変更・新規追加・継承memberのディレクトリ出現の3条件で409/config_changed、到着bytesを保持。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_an_intelligence_save_reports_where_its_directory_now_stands -> crates/gb-server/src/config_api/tests/intelligences.rs::intelligence_http_reads_saves_and_resets_member_differences — HTTPでteam保存→member差分1ファイル→継承reset。tree revision集合・保存後revisionと次の保存・event非発行。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_save_reports_where_the_files_it_wrote_now_stand -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_saving_twice_from_the_same_screen_is_not_a_conflict -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_member_save_reports_both_files_it_wrote -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_member_keeps_its_id -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_file_created_after_the_read_is_not_overwritten_unseen -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_revision_naming_something_outside_config_is_a_bad_request -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_member_read_never_hands_out_a_revision_newer_than_its_content -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_member_save_answers_with_the_revisions_it_wrote -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_config_revisions_api.py::test_a_member_deletion_answers_with_what_it_left -> 移さない：本件の6 endpoint以外。

## test_decision_api.py

- tests/guildbotics/app_api/test_decision_api.py::test_registration_and_deletion -> crates/gb-server/src/config_api/tests/intelligences.rs::decision_credentials_stay_in_workspace_store_and_never_echo — 未登録→保存unverified→登録→削除→未登録。trim、workspace限定、値の非露出、配列422、空/locked拒否。
- tests/guildbotics/app_api/test_decision_api.py::test_malformed_credential_never_echoes_its_input -> crates/gb-server/src/config_api/tests/intelligences.rs::decision_credentials_stay_in_workspace_store_and_never_echo — 未登録→保存unverified→登録→削除→未登録。trim、workspace限定、値の非露出、配列422、空/locked拒否。
- tests/guildbotics/app_api/test_decision_api.py::test_wrong_jev_model_is_rejected -> crates/gb-setup/src/intelligences_tests.rs::invalid_brains_and_unknown_tools_never_write_assignments — Jev誤モデル5値、text feature3名、CLI5 toolのchat_decision割当を拒否しbrain fileを作らない。
- tests/guildbotics/app_api/test_decision_api.py::test_jev_cannot_be_assigned_to_text_commands -> crates/gb-setup/src/intelligences_tests.rs::invalid_brains_and_unknown_tools_never_write_assignments — Jev誤モデル5値、text feature3名、CLI5 toolのchat_decision割当を拒否しbrain fileを作らない。
- tests/guildbotics/app_api/test_decision_api.py::test_member_rejects_cli_override_and_keeps_inherited_assignment -> crates/gb-server/src/config_api/tests/intelligences.rs::saved_brains_resolve_member_models_and_reject_cli_decision_override — team assignmentからmember modelを実行時に解決。Jev継承、member CLI拒否後の継承維持、旧decision.ymlを作らない。
- tests/guildbotics/app_api/test_decision_api.py::test_cli_assignment_cannot_be_saved_for_chat_decision -> crates/gb-setup/src/intelligences_tests.rs::invalid_brains_and_unknown_tools_never_write_assignments — Jev誤モデル5値、text feature3名、CLI5 toolのchat_decision割当を拒否しbrain fileを作らない。
- tests/guildbotics/app_api/test_decision_api.py::test_member_model_resolves_with_inherited_assignment -> crates/gb-server/src/config_api/tests/intelligences.rs::saved_brains_resolve_member_models_and_reject_cli_decision_override — team assignmentからmember modelを実行時に解決。Jev継承、member CLI拒否後の継承維持、旧decision.ymlを作らない。

## test_agent_environment_status.py

- tests/guildbotics/app_api/test_agent_environment_status.py::test_status_reports_the_device_in_the_words_a_turn_is_refused_with -> crates/gb-environment/src/device.rs::dns_problem_is_rendered_even_when_runtime_health_takes_precedence — 既存：deviceの拒否。アラートとの統合は #147。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_a_declared_image_this_device_lacks_is_the_devices_problem -> crates/gb-server/src/environment.rs::declaration_and_picker_share_reference_digest_and_architecture_identity — 既存：device image/picker。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_the_picker_is_offered_the_images_the_cli_lists -> crates/gb-server/src/environment.rs::declaration_and_picker_share_reference_digest_and_architecture_identity — 既存：device image/picker。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_problems_name_the_device_once_and_only_the_tools_in_use -> #147 に回す — アラートとの連携条件。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_a_ready_device_with_logins_has_no_problems -> #147 に回す — アラートとの連携条件。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_filesystem_refusal_is_shared_by_status_and_alerts -> #147 に回す — アラートとの連携条件。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_status_resolves_the_grants_once_and_names_what_each_slot_cannot_get -> crates/gb-server/src/environment.rs::member_slots_only_repeat_local_grant_problems_and_keep_tool_guidance_on_device — 既存：slotごとのgrant問題。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_an_unresolvable_local_path_is_reported_on_every_slot -> crates/gb-server/src/environment.rs::member_slots_only_repeat_local_grant_problems_and_keep_tool_guidance_on_device — 既存：slotごとのgrant問題。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_a_typed_grant_is_judged_before_it_is_saved -> crates/gb-server/src/config_api/tests/intelligences.rs::grant_preview_validates_without_creating_directories_and_composes_existing — 凍結11条件のvalid/present/locale文と非作成を照合、home deny拒否、3 scopeすべて既存shared/denyと合成。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_a_deny_that_would_close_the_home_is_refused -> crates/gb-server/src/config_api/tests/intelligences.rs::grant_preview_validates_without_creating_directories_and_composes_existing — 凍結11条件のvalid/present/locale文と非作成を照合、home deny拒否、3 scopeすべて既存shared/denyと合成。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_preview_includes_existing_shared_grants_and_local_denies -> crates/gb-server/src/config_api/tests/intelligences.rs::grant_preview_validates_without_creating_directories_and_composes_existing — 凍結11条件のvalid/present/locale文と非作成を照合、home deny拒否、3 scopeすべて既存shared/denyと合成。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_the_sandbox_endpoints_answer_from_this_device -> crates/gb-server/src/config_api/tests/intelligences.rs::grant_preview_validates_without_creating_directories_and_composes_existing — grant-evaluation行。status/images行は既存環境API、buildは本件対象外。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_login_instructions_use_the_managed_cli -> crates/gb-server/src/environment.rs::tool_guidance_uses_labels_language_and_each_credential_state — 既存：login案内と状態。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_device_authentication_failure_drives_card_and_alert_then_recovers -> #147 に回す — アラートとの連携条件。
- tests/guildbotics/app_api/test_agent_environment_status.py::test_card_and_guidance_share_the_login_command -> crates/gb-server/src/environment.rs::tool_guidance_uses_labels_language_and_each_credential_state — 既存：login案内と状態。

## test_api.py

- tests/guildbotics/app_api/test_api.py::test_registered_workspaces_are_listed_and_removal_preserves_files -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_selected_workspace_registration_cannot_be_removed -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_registration_removal_needs_no_access_to_obsolete_path -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_api_starts_unselected_when_input_store_cannot_be_initialized -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_status_poll_retries_only_failed_input_store -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_shutdown_stops_the_services_before_the_server_exits -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_teardown_budget_covers_every_wait_at_its_limit -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_a_secret_store_that_cannot_be_used_is_a_503_with_its_reason -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_health_reports_the_service_its_workspace_and_its_machine -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_workspace_change_updates_runtime_workspace -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_runtime_config_status_reports_workspace_active_location -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_command_options_describe_workspace_commands -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_command_options_exclude_template_commands -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_command_options_do_not_seed_empty_workspace -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_command_options_propagate_nested_requirements -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_command_options_resolve_brain_mapping_requirements -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_command_options_extract_markdown_arguments -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_run_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_input_file_upload_uses_app_session_temporary_directory -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_input_file_upload_runs_sync_io_outside_event_loop -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_input_file_upload_takes_any_file_a_browser_drops -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_authoring_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_authoring_create_allows_ai_to_choose_identity -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_authoring_edit_requires_existing_identity -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_authoring_apply_uses_reviewed_changes -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_options_endpoint_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_routine_command_options_endpoint_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_event_stream_replays_trace_id -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_error_uses_stable_error_shape -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_scheduler_start_event_queue_source_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_transcript_settings_endpoints_use_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_validation_error_uses_stable_error_shape -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_cli_agent_usage_endpoint_reads_one_tool -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_scenario_diagnostics_endpoint_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_scenario_diagnostics_endpoint_accepts_person_id -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_init_endpoint_writes_project_without_github -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_project_endpoints_read_and_update_non_destructively -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_project_update_without_services_removes_them -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_project_update_rejects_invalid_services -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_roles_endpoint_returns_template_roles -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_intelligence_config_endpoints_read_update_and_member_inherit -> crates/gb-server/src/config_api/tests/intelligences.rs::intelligence_http_reads_saves_and_resets_member_differences — HTTPでteam保存→member差分1ファイル→継承reset。tree revision集合・保存後revisionと次の保存・event非発行。
- tests/guildbotics/app_api/test_api.py::test_member_config_endpoints_read_update_delete -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_member_create_uses_existing_runtime_config_dir -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_default_person_endpoint_writes_runtime_config_dir -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_member_config_accepts_member_without_github_link -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_reports_missing_config -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_reload_workspace_env_before_context -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_updates_transcript_settings -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_scheduler_start_stop_lifecycle -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_marks_scheduler_failed_on_stop_timeout -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_event_listener_start_stop_lifecycle -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_marks_event_listener_failed_on_start_error -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_app_runtime_marks_event_listener_failed_on_stop_timeout -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_every_route_but_the_unauthenticated_ones_refuses_a_request_without_a_credential -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_every_api_route_lives_under_the_api_prefix -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_unexpected_error_maps_to_internal_error -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_unexpected_error_response_carries_the_security_headers -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_a_preflight_from_another_origin_is_refused_without_cors -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_workspace_change_rejects_missing_directory -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_workspace_change_rejects_non_directory -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_roles_rejects_invalid_language -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_init_maps_setup_service_error -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_project_get_reports_project_not_found -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_project_update_maps_setup_service_error -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_member_config_get_reports_missing_member_before_project_init -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_member_update_rejects_person_id_mismatch -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_providers_lists_the_providers_the_setup_screens_offer -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_provider_pages_are_served_without_a_session -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_a_setup_error_from_an_operation_is_rendered_in_the_screen_language -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_an_operation_request_is_validated_by_its_model -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_resolve_user_refuses_a_github_apps_identity_that_is_not_an_app_url -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_resolve_user_answers_with_the_github_user -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_status_options_read_the_named_board_with_a_member_who_can_reach_it -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_a_board_that_cannot_be_named_is_unavailable -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_agent_field_reports_the_registered_and_missing_members -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_agent_field_ensure_adds_the_missing_members -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_config_intelligences_reports_project_not_found -> crates/gb-server/src/config_api/tests/intelligences.rs::provider_catalog_is_available_without_project_and_settings_require_project — project未初期化で設定はproject_not_found、providerはOpenAIを含め取得できる。
- tests/guildbotics/app_api/test_api.py::test_model_providers_available_before_project_init -> crates/gb-server/src/config_api/tests/intelligences.rs::provider_catalog_is_available_without_project_and_settings_require_project — project未初期化で設定はproject_not_found、providerはOpenAIを含め取得できる。
- tests/guildbotics/app_api/test_api.py::test_config_intelligences_update_maps_setup_service_error -> crates/gb-server/src/config_api/tests/intelligences.rs::setup_errors_keep_their_code_and_sentence — 無効chat_decisionのHTTP status/code/reasonを確認。
- tests/guildbotics/app_api/test_api.py::test_scheduler_start_scheduled_and_routine_sources_endpoint -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_scheduler_start_both_endpoint -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_scheduler_stop_endpoint_leaves_the_server_running -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_scheduler_stop_endpoint_accepts_force -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_a_run_a_force_stop_cancels_answers_with_its_reason -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_a_drain_before_the_exit_can_still_be_cancelled -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_chat_receive_state_reset_endpoint_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_chat_receive_state_reset_ignores_past_for_active_members -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_chat_receive_state_reset_covers_name_only_subscription -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_chat_receive_state_reset_rejected_while_running -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_transcript_settings_reject_invalid_retention -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_runtime_debug_status_endpoint -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_runtime_debug_update_endpoint -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_service_settings_are_saved_for_every_later_start -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_an_unreadable_service_settings_file_is_reported_not_replaced -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_the_service_keep_awake_setting_applies_to_a_running_service_at_once -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_options_reports_person_not_found -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_command_run_conflict_returns_409 -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_verify_endpoint_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_diagnostics_troubleshoot_uses_runtime -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_diagnostics_troubleshoot_defaults_the_focus_to_absent -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_api.py::test_diagnostics_troubleshoot_rejects_invalid_requests -> 移さない：本件の6 endpoint以外。

## test_models.py

- tests/guildbotics/app_api/test_models.py::test_command_authoring_edit_requires_file_revision_identity -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_command_authoring_create_rejects_existing_draft_fields -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_task_schedule_accepts_five_field_cron -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_task_schedule_rejects_non_five_field_cron -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_task_schedule_excludes_blank_schedules -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_task_schedule_strips_surrounding_whitespace -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_task_schedule_requires_command -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_command_run_request_serializes_path_to_json_string -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_command_run_request_serializes_none_cwd -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_command_option_serializes_path_to_json_string -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_command_run_request_requires_non_empty_command -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_project_config_update_request_provider_api_keys_default_empty -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_project_config_update_request_keeps_provider_api_keys -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_project_config_update_request_serializes_path_to_json_string -> 移さない：本件の6 endpoint以外。
- tests/guildbotics/app_api/test_models.py::test_runtime_status_reports_active_work -> 移さない：本件の6 endpoint以外。

