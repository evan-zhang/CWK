"""PR-001 Wave-0: central append-only script-evolution guard.

RT-016 froze 26 RT-011~015 files to a genesis SHA-256 table living in
``tests/test_rt016_schemas.py``.  Plan section 4.1 of
``PR/PR-001-multitenant-knowledge-spaces/plans/剩余工作执行计划-2026-08-20.md``
allows exactly 9 of those files to evolve, through exactly 10 predeclared
stages owned by RT-012 / RT-013 / RT-017 / RT-019 / RT-021 / RT-022 / RT-026.  Every other
genesis entry stays byte-identical forever.

An owner RT evolves its script by *appending* a receipt under
``RT/RT-0NN/receipts/script-evolution/`` plus a migration note under
``RT/RT-0NN/reports/migrations/``.  This module replays that receipt chain
and recomputes, for every pinned path, the SHA the worktree must currently
have:

* no receipt for a path  ->  the path must still equal its genesis SHA;
* receipts present       ->  the path must equal the chain tip ``to_sha256``.

Central artefacts (policy + two schemas) are pinned by raw SHA-256 below,
and this module's own SHA-256 is pinned by both
``tests/test_pr001_script_evolution_guard.py`` and
``tests/test_rt016_schemas.py``.  Downstream RTs must never edit the policy,
the schemas, this helper, the guard tests or the genesis table.

SECURITY POSTURE — READ BEFORE RELYING ON THIS
==============================================
This is an **in-repository, self-referential guard**, not cryptographic
tamper-proofing.  It has no external trust anchor and no signature: a single
commit that rewrites the genesis table, the policy, this helper and both test
pins together defeats it.  What the design buys is *tamper evidence* — such a
commit must touch every one of those files at once, and (because each receipt
repeats ``policy_sha256`` and links the previous receipt's raw bytes) must
also rewrite every receipt on every path.  The real control is **independent
diff review of the six central paths**.  Do not describe this guard as
"unforgeable", "cryptographically enforced" or "tamper-proof".

Design notes
------------
* ``ast.dump`` is deliberately NOT used for the tenant-CLI fingerprint: its
  output is unstable across CPython releases (3.12 added ``type_params``;
  3.13 elides default-valued fields), so pins would silently rotate.  A
  hand-rolled serializer driven by an explicit frozen node/field table is
  used instead — an unknown node type or an unexpected field set raises
  loudly rather than changing the fingerprint quietly.
* Comments are invisible to the AST, and the lines RT-019 / RT-026 touch are
  comments, so a second fingerprint covers every comment *outside* the slot
  assignment's line span.  Comments inside the span are intentionally free
  (plan section 4.2 permits appending "the slot and its comments").
* Every check takes an explicit ``root`` so the whole attack surface is
  testable against a synthetic tree; no function derives ``root`` from
  ``__file__``, imports the RT-016 test module, or calls ``os.chdir``.

Python 3.11+, pure stdlib, no pytest.
"""

from __future__ import annotations

import ast
import hashlib
import io
import os
import re
import stat
import sys
import tokenize
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts"))

import cwk_pr001_contracts as C  # noqa: E402


# ---------------------------------------------------------------------------
# Central pins.  Bootstrap order is contracts -> this helper -> the two test
# files; nothing pins the test files (two files cannot pin each other), which
# is why they are the human-review trust root.
# ---------------------------------------------------------------------------

CONTRACT_DIR = "PR/PR-001-multitenant-knowledge-spaces/contracts/script-evolution"
POLICY_REL = CONTRACT_DIR + "/policy_v1.json"
POLICY_SCHEMA_REL = CONTRACT_DIR + "/policy_v1.schema.json"
RECEIPT_SCHEMA_REL = CONTRACT_DIR + "/receipt_v1.schema.json"

PINNED_POLICY_SHA256 = "2089490e45bdd84ba3bac75fe40092f81f40765638b988e17facdc4040d14a6d"
PINNED_POLICY_SCHEMA_SHA256 = "a3c5a7f48c1edcf2ac2c47beeeb5aa8e361443eda561a24a5e0e8a0a5b7a0b86"
PINNED_RECEIPT_SCHEMA_SHA256 = "fc5759981bc5f2d555dfc348ff00528dc4842a2f2aab0c549188691bd7f69410"

# Wave-0 baseline fingerprints of scripts/cwk_tenant_cli.py.  The AST
# fingerprint normalises away ONLY the FROZEN_PROVIDER_SLOTS tuple value; the
# comment fingerprint covers every comment outside that assignment's span.
PINNED_TENANT_CLI_AST_FINGERPRINT = (
    "a27ea2c297aceb1b8dc9710f0fb31c79558504f21bf935630db03800b08671de"
)
PINNED_TENANT_CLI_COMMENT_FINGERPRINT = (
    "f04669549d5985e6ef206e833110ea411286884ea23fa8c52a281af83337fcfd"
)

# The two files that hold the CommandProviderV1 ABI but are NOT in the RT-016
# genesis table.  Pinned here, not merely counted in the policy: a schema that
# only says "exactly 2 entries" is satisfied by two copies of one path, or by
# swapping one of them for a file nobody cares about.
REQUIRED_COMPANION_PATHS: tuple[str, ...] = (
    "scripts/cwk_tenant_cli_api.py",
    "scripts/cwk_tenant_cmd_core.py",
)

GENESIS_ENTRY_COUNT = 26
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_JSON_DEPTH = 12
MIN_MIGRATION_NOTE_BYTES = 200

# Frozen negative-coverage surface.  tests/test_pr001_script_evolution_guard.py
# asserts its own collected tests equal this set, so attack coverage cannot be
# quietly deleted or renamed by a later RT.
REQUIRED_ATTACK_TEST_NAMES: frozenset[str] = frozenset(
    (
        "AcceptanceTestRefTests.test_acceptance_ref_naming_another_rt_rejected",
        "AcceptanceTestRefTests.test_acceptance_ref_out_of_grammar_rejected",
        "AcceptanceTestRefTests.test_acceptance_ref_with_traversal_rejected",
        "AcceptanceTestRefTests.test_acceptance_ref_with_unknown_class_rejected",
        "AcceptanceTestRefTests.test_acceptance_ref_with_unknown_method_rejected",
        "AcceptanceTestRefTests.test_acceptance_test_file_that_does_not_parse_rejected",
        "AcceptanceTestRefTests.test_duplicate_acceptance_refs_rejected",
        "AcceptanceTestRefTests.test_empty_acceptance_refs_rejected",
        "AcceptanceTestRefTests.test_missing_acceptance_test_file_rejected",
        "AcceptanceTestRigorTests.test_a_base_class_aliased_skip_decorator_is_rejected",
        "AcceptanceTestRigorTests.test_a_base_class_skip_decorator_really_skips_and_is_rejected",
        "AcceptanceTestRigorTests.test_a_base_class_skip_flag_is_rejected",
        "AcceptanceTestRigorTests.test_a_class_name_bound_as_something_other_than_a_class_is_rejected",
        "AcceptanceTestRigorTests.test_a_decorator_of_unknown_provenance_is_refused",
        "AcceptanceTestRigorTests.test_a_method_bound_as_something_other_than_a_def_is_rejected",
        "AcceptanceTestRigorTests.test_a_well_formed_acceptance_test_is_accepted",
        "AcceptanceTestRigorTests.test_aliased_skip_decorator_on_the_class_is_rejected",
        "AcceptanceTestRigorTests.test_aliased_skip_decorator_on_the_method_is_rejected",
        "AcceptanceTestRigorTests.test_aliased_testcase_bases_are_refused_conservatively",
        "AcceptanceTestRigorTests.test_async_method_rejected",
        "AcceptanceTestRigorTests.test_bare_skip_names_are_rejected",
        "AcceptanceTestRigorTests.test_class_body_skip_flag_is_rejected_and_really_skips",
        "AcceptanceTestRigorTests.test_class_defined_inside_a_truthy_branch_is_also_rejected",
        "AcceptanceTestRigorTests.test_class_defined_inside_a_try_block_is_rejected",
        "AcceptanceTestRigorTests.test_class_deleted_after_definition_is_rejected",
        "AcceptanceTestRigorTests.test_class_deriving_from_a_plain_object_base_rejected",
        "AcceptanceTestRigorTests.test_class_deriving_through_an_intermediate_base_rejected",
        "AcceptanceTestRigorTests.test_class_hidden_behind_a_false_branch_is_rejected",
        "AcceptanceTestRigorTests.test_class_rebound_by_an_assignment_is_rejected",
        "AcceptanceTestRigorTests.test_class_that_unittest_would_never_collect_rejected",
        "AcceptanceTestRigorTests.test_conditionally_rebound_base_is_rejected",
        "AcceptanceTestRigorTests.test_delete_hidden_in_a_branch_or_destructured_is_rejected",
        "AcceptanceTestRigorTests.test_destructured_skip_flag_is_rejected_and_really_skips",
        "AcceptanceTestRigorTests.test_docstring_plus_a_real_assertion_accepted",
        "AcceptanceTestRigorTests.test_duplicate_class_is_rejected",
        "AcceptanceTestRigorTests.test_duplicate_method_is_rejected_and_the_no_op_really_wins",
        "AcceptanceTestRigorTests.test_expecting_failure_flag_is_rejected",
        "AcceptanceTestRigorTests.test_from_unittest_import_testcase_accepted",
        "AcceptanceTestRigorTests.test_imported_testcase_rebound_later_is_rejected",
        "AcceptanceTestRigorTests.test_imported_testcase_shadowed_by_a_local_class_is_rejected",
        "AcceptanceTestRigorTests.test_intermediate_base_shadowed_by_an_assignment_is_rejected",
        "AcceptanceTestRigorTests.test_keyword_base_arguments_are_rejected",
        "AcceptanceTestRigorTests.test_locally_faked_testcase_base_is_rejected_and_collects_nothing",
        "AcceptanceTestRigorTests.test_method_calling_skiptest_rejected",
        "AcceptanceTestRigorTests.test_method_deleted_from_the_class_body_is_rejected",
        "AcceptanceTestRigorTests.test_method_deleted_through_an_attribute_is_rejected",
        "AcceptanceTestRigorTests.test_method_rebound_by_an_assignment_is_rejected",
        "AcceptanceTestRigorTests.test_method_rebound_inside_a_class_body_branch_is_rejected",
        "AcceptanceTestRigorTests.test_method_skip_flag_assigned_from_module_scope_is_rejected",
        "AcceptanceTestRigorTests.test_method_that_makes_no_assertion_rejected",
        "AcceptanceTestRigorTests.test_method_with_default_arguments_accepted",
        "AcceptanceTestRigorTests.test_method_with_extra_required_parameters_rejected",
        "AcceptanceTestRigorTests.test_method_with_required_keyword_only_parameter_rejected",
        "AcceptanceTestRigorTests.test_method_without_self_rejected",
        "AcceptanceTestRigorTests.test_multiple_inheritance_is_rejected",
        "AcceptanceTestRigorTests.test_no_op_method_body_rejected",
        "AcceptanceTestRigorTests.test_self_fail_counts_as_an_assertion",
        "AcceptanceTestRigorTests.test_skip_flag_bound_by_a_for_target_or_walrus_is_rejected",
        "AcceptanceTestRigorTests.test_skip_flag_bound_through_every_destructuring_shape_is_rejected",
        "AcceptanceTestRigorTests.test_skip_helpers_imported_under_an_alias_are_rejected",
        "AcceptanceTestRigorTests.test_skipped_class_rejected",
        "AcceptanceTestRigorTests.test_skipped_method_rejected",
        "AcceptanceTestRigorTests.test_the_guard_never_executes_the_acceptance_test",
        "AcceptanceTestRigorTests.test_the_static_check_cannot_detect_a_tautological_assertion",
        "AcceptanceTestRigorTests.test_the_well_formed_control_really_is_collected",
        "AcceptanceTestRigorTests.test_unittest_imported_inside_a_try_block_is_rejected",
        "AcceptanceTestRigorTests.test_unittest_module_name_rebound_is_rejected",
        "AstSkippedFieldTests.test_default_only_check_passes_on_the_real_baseline",
        "AstSkippedFieldTests.test_default_only_check_rejects_a_non_default_type_comment",
        "AstSkippedFieldTests.test_default_only_check_rejects_a_non_default_type_params",
        "AstSkippedFieldTests.test_every_skipped_field_has_a_declared_default",
        "AstSkippedFieldTests.test_generic_class_drift_is_caught_end_to_end",
        "AstSkippedFieldTests.test_generic_class_rejected",
        "AstSkippedFieldTests.test_generic_error_class_cannot_hide_behind_the_fingerprint",
        "AstSkippedFieldTests.test_generic_function_drift_is_caught_end_to_end",
        "AstSkippedFieldTests.test_generic_function_rejected",
        "AstSkippedFieldTests.test_pinned_fingerprints_are_stable_across_python_versions",
        "AstSkippedFieldTests.test_type_comment_is_invisible_because_we_never_request_it",
        "ChainReplayTests.test_actual_bytes_beyond_the_tip_rejected",
        "ChainReplayTests.test_companion_immutable_drift_rejected",
        "ChainReplayTests.test_from_equals_to_rejected",
        "ChainReplayTests.test_full_chain_of_all_ten_stages_passes",
        "ChainReplayTests.test_gap_in_chain_rejected",
        "ChainReplayTests.test_genesis_link_copied_from_another_path_rejected",
        "ChainReplayTests.test_immutable_path_drift_rejected",
        "ChainReplayTests.test_no_receipt_and_no_drift_passes",
        "ChainReplayTests.test_no_receipt_drift_rejected",
        "ChainReplayTests.test_receipt_bound_to_a_different_policy_rejected",
        "ChainReplayTests.test_receipt_bytes_edited_after_the_fact_breaks_the_next_link",
        "ChainReplayTests.test_receipt_missing_required_field_rejected",
        "ChainReplayTests.test_receipt_present_but_file_reverted_rejected",
        "ChainReplayTests.test_receipt_symlink_rejected",
        "ChainReplayTests.test_receipt_with_bom_rejected",
        "ChainReplayTests.test_receipt_with_duplicate_json_key_rejected",
        "ChainReplayTests.test_receipt_with_extra_field_rejected",
        "ChainReplayTests.test_receipt_with_uppercase_sha_rejected",
        "ChainReplayTests.test_receipt_with_wrong_policy_id_rejected",
        "ChainReplayTests.test_receipt_written_at_another_stages_declared_path_rejected",
        "ChainReplayTests.test_second_receipt_with_broken_previous_bytes_link_rejected",
        "ChainReplayTests.test_single_receipt_chain_passes",
        "ChainReplayTests.test_undeclared_receipt_file_rejected",
        "ChainReplayTests.test_wrong_from_sha_rejected",
        "ChainReplayTests.test_wrong_ordinal_rejected",
        "ChainReplayTests.test_wrong_owner_rt_rejected",
        "ChainReplayTests.test_wrong_previous_link_rejected",
        "ChainReplayTests.test_wrong_stage_index_rejected",
        "ChainReplayTests.test_wrong_target_path_rejected",
        "ChainReplayTests.test_wrong_to_sha_rejected",
        "CommentPositionTests.test_appending_a_slot_keeps_the_comment_fingerprint",
        "CommentPositionTests.test_column_shift_alone_is_caught",
        "CommentPositionTests.test_comment_deletion_is_caught",
        "CommentPositionTests.test_comment_move_is_caught_end_to_end_behind_a_receipt",
        "CommentPositionTests.test_comment_reordering_between_two_sites_is_caught",
        "CommentPositionTests.test_comments_inside_the_span_stay_unpinned",
        "CommentPositionTests.test_identical_comment_text_moved_across_the_span_is_caught",
        "CommentPositionTests.test_inline_to_own_line_move_is_caught",
        "CommentPositionTests.test_noqa_moved_to_the_next_statement_is_caught",
        "CommentPositionTests.test_source_that_does_not_tokenise_is_rejected",
        "CommentPositionTests.test_type_ignore_moved_to_the_next_statement_is_caught",
        "CompanionImmutableTests.test_companion_drift_is_caught_even_with_a_full_legal_chain",
        "CompanionImmutableTests.test_companion_that_is_also_evolvable_rejected",
        "CompanionImmutableTests.test_companions_are_absent_from_the_genesis_table",
        "CompanionImmutableTests.test_dropped_companion_rejected",
        "CompanionImmutableTests.test_duplicate_companion_entry_rejected",
        "CompanionImmutableTests.test_each_companion_is_checked_for_drift_independently",
        "CompanionImmutableTests.test_extra_third_companion_rejected",
        "CompanionImmutableTests.test_real_policy_pins_each_companion_exactly_once",
        "CompanionImmutableTests.test_required_companion_paths_are_exactly_the_two_abi_files",
        "CompanionImmutableTests.test_substituted_companion_rejected",
        "LiveRepoInvariantTests.test_invariants_hold_after_a_single_receipt",
        "LiveRepoInvariantTests.test_invariants_hold_after_every_declared_stage_lands",
        "LiveRepoInvariantTests.test_invariants_hold_after_receipts_on_several_independent_paths",
        "LiveRepoInvariantTests.test_invariants_hold_after_the_ordered_tenant_cli_pair",
        "LiveRepoInvariantTests.test_invariants_hold_at_wave_0_with_no_receipts",
        "LiveRepoInvariantTests.test_invariants_reject_a_gapped_future_state",
        "MigrationNoteTests.test_migration_note_never_mentioning_the_owner_rejected",
        "MigrationNoteTests.test_migration_note_never_mentioning_the_target_rejected",
        "MigrationNoteTests.test_migration_note_path_not_the_declared_one_rejected",
        "MigrationNoteTests.test_migration_note_sha_mismatch_rejected",
        "MigrationNoteTests.test_migration_note_short_but_hash_matched_rejected",
        "MigrationNoteTests.test_migration_note_too_short_rejected",
        "MigrationNoteTests.test_missing_migration_note_rejected",
        "PolicyPinTests.test_fixture_policy_loads",
        "PolicyPinTests.test_genesis_entry_count_mismatch_rejected",
        "PolicyPinTests.test_genesis_manifest_mismatch_rejected",
        "PolicyPinTests.test_genesis_missing_an_evolvable_path_rejected",
        "PolicyPinTests.test_policy_duplicate_provider_slot_rejected",
        "PolicyPinTests.test_policy_edited_after_pinning_rejected",
        "PolicyPinTests.test_policy_extra_field_rejected",
        "PolicyPinTests.test_policy_non_tenant_cli_stage_adding_a_slot_rejected",
        "PolicyPinTests.test_policy_receipt_path_not_owned_by_stage_owner_rejected",
        "PolicyPinTests.test_policy_schema_pin_mismatch_rejected",
        "PolicyPinTests.test_policy_sha_pin_mismatch_rejected",
        "PolicyPinTests.test_policy_stage_index_out_of_order_rejected",
        "PolicyPinTests.test_policy_stage_owner_not_an_owner_of_the_path_rejected",
        "PolicyPinTests.test_policy_stage_readding_a_baseline_slot_rejected",
        "PolicyPinTests.test_policy_with_tenth_evolvable_path_rejected",
        "PolicyPinTests.test_policy_with_nine_stages_rejected",
        "PolicyPinTests.test_receipt_schema_pin_mismatch_rejected",
        "RT026OrderTests.test_independent_paths_may_land_out_of_stage_order",
        "RT026OrderTests.test_ordered_tenant_cli_stages_pass",
        "RT026OrderTests.test_requires_stage_index_branch_runs_inside_verify",
        "RT026OrderTests.test_requires_stage_index_null_falls_through_to_the_gap_rule",
        "RT026OrderTests.test_rt026_cannot_skip_rt019_tenant_cli_stage",
        "ReaderRaceTests.test_assert_stat_unchanged_accepts_a_stable_file",
        "ReaderRaceTests.test_assert_stat_unchanged_detects_every_invariant_field",
        "ReaderRaceTests.test_case_alias_defence_survives_the_dir_fd_rewrite",
        "ReaderRaceTests.test_dir_fd_support_is_present_on_this_platform",
        "ReaderRaceTests.test_file_rewritten_during_the_read_rejected",
        "ReaderRaceTests.test_hard_link_created_after_the_open_rejected",
        "ReaderRaceTests.test_leaf_file_swapped_between_stat_and_open_rejected",
        "ReaderRaceTests.test_leaf_is_opened_relative_to_the_parent_fd_not_by_path",
        "ReaderRaceTests.test_parent_directory_swapped_between_stat_and_open_rejected",
        "ReaderRaceTests.test_reader_refuses_to_run_without_dir_fd_support",
        "ReaderRaceTests.test_stable_file_reads_cleanly_under_the_same_hooks",
        "ReaderRaceTests.test_unicode_alias_defence_survives_the_dir_fd_rewrite",
        "RealRepoTests.test_attack_surface_matches_the_frozen_required_set",
        "RealRepoTests.test_central_contract_shas_match_their_pins",
        "RealRepoTests.test_guard_disclaims_cryptographic_tamper_proofing",
        "RealRepoTests.test_guard_helper_sha_matches_its_pin",
        "RealRepoTests.test_real_policy_declares_nine_paths_and_ten_stages",
        "RealRepoTests.test_real_repo_passes_with_the_pinned_values",
        "RealRepoTests.test_real_repo_receipts_are_all_policy_declared",
        "RealRepoTests.test_real_tenant_cli_fingerprints_match_the_pins",
        "Rt016ReaderIntegrationTests.test_frozen_file_checks_never_use_symlink_following_apis",
        "Rt016ReaderIntegrationTests.test_hard_link_with_identical_bytes_is_rejected",
        "Rt016ReaderIntegrationTests.test_list_dir_of_a_file_is_rejected",
        "Rt016ReaderIntegrationTests.test_list_dir_of_a_missing_directory_is_empty_not_an_error",
        "Rt016ReaderIntegrationTests.test_list_dir_rejects_a_symlinked_directory",
        "Rt016ReaderIntegrationTests.test_list_dir_returns_lstat_so_symlinked_entries_are_visible",
        "Rt016ReaderIntegrationTests.test_symlink_with_identical_bytes_is_rejected",
        "SafePathTests.test_read_checked_bytes_case_alias_rejected",
        "SafePathTests.test_read_checked_bytes_directory_rejected",
        "SafePathTests.test_read_checked_bytes_hardlink_rejected",
        "SafePathTests.test_read_checked_bytes_missing_ok_returns_none",
        "SafePathTests.test_read_checked_bytes_missing_required_rejected",
        "SafePathTests.test_read_checked_bytes_oversize_rejected",
        "SafePathTests.test_read_checked_bytes_symlink_component_rejected",
        "SafePathTests.test_read_checked_bytes_symlink_escaping_the_root_rejected",
        "SafePathTests.test_read_checked_bytes_symlink_leaf_rejected",
        "SafePathTests.test_safe_relpath_absolute_path_rejected",
        "SafePathTests.test_safe_relpath_accepts_a_plain_repo_path",
        "SafePathTests.test_safe_relpath_backslash_rejected",
        "SafePathTests.test_safe_relpath_empty_component_rejected",
        "SafePathTests.test_safe_relpath_home_expansion_rejected",
        "SafePathTests.test_safe_relpath_non_nfc_rejected",
        "SafePathTests.test_safe_relpath_non_string_rejected",
        "SafePathTests.test_safe_relpath_nul_byte_rejected",
        "SafePathTests.test_safe_relpath_parent_traversal_rejected",
        "SafePathTests.test_safe_relpath_single_dot_component_rejected",
        "StrictJsonTests.test_strict_json_accepts_a_canonical_object",
        "StrictJsonTests.test_strict_json_bom_rejected",
        "StrictJsonTests.test_strict_json_control_character_in_string_rejected",
        "StrictJsonTests.test_strict_json_deep_nesting_rejected",
        "StrictJsonTests.test_strict_json_duplicate_key_rejected",
        "StrictJsonTests.test_strict_json_embedded_bom_rejected",
        "StrictJsonTests.test_strict_json_infinity_rejected",
        "StrictJsonTests.test_strict_json_invalid_utf8_rejected",
        "StrictJsonTests.test_strict_json_nan_rejected",
        "StrictJsonTests.test_strict_json_non_nfc_key_rejected",
        "StrictJsonTests.test_strict_json_non_nfc_string_rejected",
        "StrictJsonTests.test_strict_json_non_object_root_rejected",
        "StrictJsonTests.test_strict_json_plain_float_rejected",
        "StrictJsonTests.test_strict_json_trailing_data_rejected",
        "TenantCliAstTests.test_annotation_retype_rejected",
        "TenantCliAstTests.test_appending_a_slot_does_not_change_the_ast_fingerprint",
        "TenantCliAstTests.test_baseline_slots_pass_without_receipts",
        "TenantCliAstTests.test_carriage_return_line_endings_rejected",
        "TenantCliAstTests.test_docstring_edit_changes_the_ast_fingerprint",
        "TenantCliAstTests.test_duplicate_slot_rejected",
        "TenantCliAstTests.test_error_class_rename_changes_the_ast_fingerprint",
        "TenantCliAstTests.test_in_span_comment_edit_keeps_both_fingerprints",
        "TenantCliAstTests.test_logic_drift_hidden_behind_a_receipt_is_still_caught",
        "TenantCliAstTests.test_logic_drift_is_caught_end_to_end",
        "TenantCliAstTests.test_nfd_literal_substitution_changes_the_ast_fingerprint",
        "TenantCliAstTests.test_out_of_span_comment_drift_changes_the_comment_fingerprint",
        "TenantCliAstTests.test_plain_assign_instead_of_annassign_rejected",
        "TenantCliAstTests.test_slot_appended_without_a_receipt_rejected",
        "TenantCliAstTests.test_slot_deletion_with_a_receipt_rejected",
        "TenantCliAstTests.test_slot_reorder_rejected",
        "TenantCliAstTests.test_slot_reorder_with_a_receipt_rejected",
        "TenantCliAstTests.test_slot_span_longer_than_the_policy_bound_rejected",
        "TenantCliAstTests.test_symbol_rebound_elsewhere_rejected",
        "TenantCliAstTests.test_two_slots_appended_under_one_receipt_rejected",
        "TenantCliAstTests.test_unknown_ast_node_type_rejected",
        "TenantCliAstTests.test_unknown_slot_name_rejected",
        "TenantCliAstTests.test_weakened_loader_guard_changes_the_ast_fingerprint",
    )
)


class ScriptEvolutionError(Exception):
    """Raised when the append-only evolution contract is violated."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_RT_DIR_RE = re.compile(r"\ART-[0-9]{3}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TEST_REF_RE = re.compile(
    r"\A(tests/test_rt0([0-9]{2})_[a-z0-9_]{1,64}\.py)"
    r"::([A-Za-z_][A-Za-z0-9_]{0,63})::(test_[a-z0-9_]{1,80})\Z"
)


def safe_relpath(rel: Any, *, label: str = "path") -> tuple[str, ...]:
    """Split a repo-relative POSIX path, failing closed on every trick.

    Rejects: non-strings, empty, control characters/NUL, non-NFC Unicode,
    backslashes, absolute paths, drive letters, ``.``/``..`` components,
    empty components and out-of-grammar components.
    """

    if not isinstance(rel, str):
        raise ScriptEvolutionError(f"{label}: path must be a string, got {type(rel).__name__}")
    if not rel:
        raise ScriptEvolutionError(f"{label}: path must not be empty")
    if len(rel) > 512:
        raise ScriptEvolutionError(f"{label}: path too long")
    if _CONTROL_RE.search(rel):
        raise ScriptEvolutionError(f"{label}: path contains a control character")
    if unicodedata.normalize("NFC", rel) != rel:
        raise ScriptEvolutionError(f"{label}: path is not NFC-normalised")
    if rel != rel.strip():
        raise ScriptEvolutionError(f"{label}: path has leading/trailing whitespace")
    if "\\" in rel:
        raise ScriptEvolutionError(f"{label}: path contains a backslash")
    if rel.startswith("/") or rel.startswith("~"):
        raise ScriptEvolutionError(f"{label}: path must be repo-relative, not absolute")
    if os.path.isabs(rel) or (len(rel) > 1 and rel[1] == ":"):
        raise ScriptEvolutionError(f"{label}: path must be repo-relative, not absolute")
    parts = rel.split("/")
    for part in parts:
        if not part:
            raise ScriptEvolutionError(f"{label}: path has an empty component")
        if part in (".", ".."):
            raise ScriptEvolutionError(f"{label}: path traversal component {part!r}")
        if not _COMPONENT_RE.match(part):
            raise ScriptEvolutionError(f"{label}: path component {part!r} is out of grammar")
    return tuple(parts)


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# Every component is resolved relative to a directory *file descriptor* we
# already hold, never by re-traversing a string path.  That is what closes the
# parent-swap race: once a directory fd is open it names a fixed inode, so a
# rename() underneath us cannot redirect the next lookup.
# os.lstat is not itself listed in supports_dir_fd -- the documented spelling
# is os.stat(..., follow_symlinks=False), which is what _lstat_at uses.
_DIR_FD_SUPPORTED = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.listdir in os.supports_fd
)


def _lstat_at(name: str, dir_fd: int) -> os.stat_result:
    """``lstat`` relative to an open directory fd, never following symlinks."""

    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _require_dir_fd(label: str) -> None:
    if not _DIR_FD_SUPPORTED:
        raise ScriptEvolutionError(
            f"{label}: this platform does not support openat()/fstatat() directory "
            "file descriptors, so the guard cannot read pinned files without a "
            "TOCTOU window.  Refusing to verify rather than verifying weakly."
        )


def _listdir_fd(fd: int, *, label: str) -> set[str]:
    try:
        return set(os.listdir(fd))
    except OSError as exc:
        raise ScriptEvolutionError(
            f"{label}: cannot list a path component ({exc.__class__.__name__})"
        ) from None


def _check_exact_name(fd: int, name: str, *, label: str, missing_ok: bool) -> bool:
    """Exact-name membership test.

    macOS/APFS is case- and normalisation-insensitive, so ``open`` happily
    resolves ``SCRIPTS/CWK_TENANT_CLI.PY`` to the real script and
    ``os.path.samefile`` agrees.  Listing the parent and demanding a
    byte-exact match is the only reliable mitigation.
    """

    names = _listdir_fd(fd, label=label)
    if name in names:
        return True
    aliases = sorted(n for n in names if _fold(n) == _fold(name))
    if aliases:
        raise ScriptEvolutionError(
            f"{label}: component {name!r} does not exist with that exact name; "
            f"the filesystem is aliasing it to {aliases!r} (case/Unicode-insensitive FS)"
        )
    if missing_ok:
        return False
    raise ScriptEvolutionError(f"{label}: component {name!r} is missing")


def _open_root_fd(root: Path, *, label: str) -> int:
    _require_dir_fd(label)
    try:
        fd = os.open(str(root), os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    except OSError as exc:
        raise ScriptEvolutionError(f"{label}: unusable root ({exc.__class__.__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise ScriptEvolutionError(f"{label}: root is not a directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _walk_to_parent_fd(
    root: Path, parts: Sequence[str], *, label: str, missing_ok: bool
) -> tuple[int, str] | None:
    """Open the leaf's *parent directory* by fd, one ``openat`` hop at a time.

    Returns ``(parent_fd, leaf_name)``; the caller owns ``parent_fd`` and must
    close it.  Returns ``None`` only when ``missing_ok`` and a component is
    genuinely absent.
    """

    fd = _open_root_fd(root, label=label)
    root_dev = os.fstat(fd).st_dev
    try:
        for name in parts[:-1]:
            if not _check_exact_name(fd, name, label=label, missing_ok=missing_ok):
                return None
            try:
                pre = _lstat_at(name, fd)
            except OSError as exc:
                raise ScriptEvolutionError(
                    f"{label}: cannot stat component {name!r} ({exc.__class__.__name__})"
                ) from None
            if stat.S_ISLNK(pre.st_mode):
                raise ScriptEvolutionError(f"{label}: component {name!r} is a symlink")
            if not stat.S_ISDIR(pre.st_mode):
                raise ScriptEvolutionError(f"{label}: component {name!r} is not a directory")
            if pre.st_dev != root_dev:
                raise ScriptEvolutionError(f"{label}: component {name!r} crosses a mount point")
            try:
                child = os.open(
                    name,
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise ScriptEvolutionError(
                    f"{label}: cannot open component {name!r} ({exc.__class__.__name__})"
                ) from None
            os.close(fd)
            fd = child
            post = os.fstat(fd)
            if (post.st_dev, post.st_ino) != (pre.st_dev, pre.st_ino):
                raise ScriptEvolutionError(
                    f"{label}: component {name!r} was swapped between stat and open (TOCTOU)"
                )
            if post.st_dev != root_dev:
                raise ScriptEvolutionError(f"{label}: component {name!r} crosses a mount point")
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


_STAT_INVARIANTS = (
    "st_dev",
    "st_ino",
    "st_nlink",
    "st_size",
    "st_mode",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _assert_stat_unchanged(pre: os.stat_result, post: os.stat_result, *, label: str) -> None:
    """Re-check every identity/content field across the read window.

    ``st_nlink`` catches a hard link created *after* the open; ``st_size`` /
    ``st_mtime_ns`` / ``st_ctime_ns`` catch an in-place rewrite while we were
    reading; ``st_dev`` / ``st_ino`` catch an inode swap.
    """

    for field in _STAT_INVARIANTS:
        before, after = getattr(pre, field), getattr(post, field)
        if before != after:
            raise ScriptEvolutionError(
                f"{label}: file changed while it was being read ({field}: {before} -> {after}); "
                "a pinned file must be stable for the whole read"
            )


def read_checked_bytes(
    root: Path,
    rel: str,
    *,
    label: str | None = None,
    missing_ok: bool = False,
    max_bytes: int = MAX_FILE_BYTES,
) -> bytes | None:
    """Read a repo-relative regular file, failing closed on every path trick.

    Rejects symlinked components and leaves, hardlinked files
    (``st_nlink != 1``), non-regular files, oversize files, mount-point
    crossings, name aliasing, parent-directory swaps, inode swaps between
    ``lstat`` and ``open``, and any mutation observed across the read window.
    """

    label = label or rel
    parts = safe_relpath(rel, label=label)
    walked = _walk_to_parent_fd(root, parts, label=label, missing_ok=missing_ok)
    if walked is None:
        return None
    parent_fd, leaf = walked
    try:
        if not _check_exact_name(parent_fd, leaf, label=label, missing_ok=missing_ok):
            return None
        try:
            pre = _lstat_at(leaf, parent_fd)
        except OSError as exc:
            raise ScriptEvolutionError(
                f"{label}: cannot stat ({exc.__class__.__name__})"
            ) from None
        if stat.S_ISLNK(pre.st_mode):
            raise ScriptEvolutionError(f"{label}: is a symlink")
        if not stat.S_ISREG(pre.st_mode):
            raise ScriptEvolutionError(f"{label}: not a regular file")
        try:
            fd = os.open(leaf, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent_fd)
        except OSError as exc:
            raise ScriptEvolutionError(f"{label}: cannot open ({exc.__class__.__name__})") from None
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ScriptEvolutionError(f"{label}: not a regular file after open")
            if opened.st_nlink != 1:
                raise ScriptEvolutionError(
                    f"{label}: file has {opened.st_nlink} hard links; "
                    "pinned files must have exactly one"
                )
            if (opened.st_dev, opened.st_ino) != (pre.st_dev, pre.st_ino):
                raise ScriptEvolutionError(
                    f"{label}: file was swapped between stat and open (TOCTOU)"
                )
            if opened.st_size > max_bytes:
                raise ScriptEvolutionError(f"{label}: file larger than {max_bytes} bytes")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ScriptEvolutionError(f"{label}: file larger than {max_bytes} bytes")
                chunks.append(chunk)
            after = os.fstat(fd)
            _assert_stat_unchanged(opened, after, label=label)
            if after.st_nlink != 1:
                raise ScriptEvolutionError(
                    f"{label}: file gained hard links while being read; "
                    "pinned files must have exactly one"
                )
            if total != after.st_size:
                raise ScriptEvolutionError(
                    f"{label}: read {total} bytes but the file reports {after.st_size}"
                )
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    return b"".join(chunks)


def read_required_bytes(root: Path, rel: str, *, label: str | None = None) -> bytes:
    data = read_checked_bytes(root, rel, label=label)
    assert data is not None  # missing_ok=False never returns None
    return data


def file_sha256(root: Path, rel: str, *, label: str | None = None) -> str:
    return hashlib.sha256(read_required_bytes(root, rel, label=label)).hexdigest()


# ---------------------------------------------------------------------------
# Strict JSON
# ---------------------------------------------------------------------------


def _audit_json(value: Any, *, label: str, path: str, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ScriptEvolutionError(f"{label}: JSON nested deeper than {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, float):
        # Also the NaN / Infinity kill switch: json.loads accepts those by
        # default, and no field in either schema is a float.
        raise ScriptEvolutionError(f"{label}: floating-point value at {path} is not permitted")
    if isinstance(value, int):
        if abs(value) > C.IJSON_MAX_SAFE_INT:
            raise ScriptEvolutionError(f"{label}: integer at {path} outside I-JSON safe range")
        return
    if isinstance(value, str):
        if _CONTROL_RE.search(value):
            raise ScriptEvolutionError(f"{label}: control character in string at {path}")
        if unicodedata.normalize("NFC", value) != value:
            raise ScriptEvolutionError(f"{label}: string at {path} is not NFC-normalised")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _audit_json(item, label=label, path=f"{path}[{i}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, sub in value.items():
            if not isinstance(key, str):
                raise ScriptEvolutionError(f"{label}: non-string JSON key at {path}")
            if unicodedata.normalize("NFC", key) != key:
                raise ScriptEvolutionError(f"{label}: JSON key {key!r} is not NFC-normalised")
            _audit_json(sub, label=label, path=f"{path}.{key}", depth=depth + 1)
        return
    raise ScriptEvolutionError(f"{label}: unsupported JSON value type at {path}")


def strict_json_bytes(data: bytes, *, label: str) -> dict[str, Any]:
    """Parse JSON bytes with every strictness knob turned on.

    Rejects BOM, non-UTF-8, duplicate keys (via the RT-011 loader's
    ``object_pairs_hook``), trailing data, non-object roots, floats
    (hence NaN/Infinity), non-NFC strings/keys and over-deep nesting.
    """

    if not isinstance(data, bytes):
        raise ScriptEvolutionError(f"{label}: expected raw bytes")
    if data.startswith(b"\xef\xbb\xbf"):
        raise ScriptEvolutionError(f"{label}: JSON must not start with a UTF-8 BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ScriptEvolutionError(f"{label}: JSON is not valid UTF-8") from None
    if "﻿" in text:
        raise ScriptEvolutionError(f"{label}: JSON contains a byte-order mark")
    try:
        payload = C.strict_json_loads(text)
    except C.ContractError as exc:
        raise ScriptEvolutionError(f"{label}: {exc}") from None
    except ValueError as exc:
        raise ScriptEvolutionError(f"{label}: invalid JSON ({exc})") from None
    if not isinstance(payload, dict):
        raise ScriptEvolutionError(f"{label}: JSON root must be an object")
    _audit_json(payload, label=label, path="$", depth=0)
    return payload


def validate_against_schema(schema: Mapping[str, Any], payload: Any, *, label: str) -> None:
    """Structural gate via the RT-011 Draft 2020-12 subset engine."""

    try:
        C._validate_schema(schema, payload, "$", root_schema=schema)
    except C.ContractError as exc:
        raise ScriptEvolutionError(f"{label}: schema violation: {exc}") from None


# ---------------------------------------------------------------------------
# Domain-separated genesis link (plan section 4.6 length-prefix convention)
# ---------------------------------------------------------------------------


def _lp(value: str) -> bytes:
    raw = unicodedata.normalize("NFC", value).encode("utf-8")
    return len(raw).to_bytes(4, "big") + raw


def genesis_link(*, domain: str, policy_sha256: str, target_path: str) -> str:
    """Ordinal-1 ``previous_receipt_sha256``.

    Domain-separated and bound to both the policy and the target path, so an
    ordinal-1 link cannot be copy-pasted from another path's receipt.
    """

    digest = hashlib.sha256()
    digest.update(domain.encode("ascii"))
    digest.update(b"\x00")
    digest.update(_lp(policy_sha256))
    digest.update(_lp(target_path))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    raw: Mapping[str, Any]
    sha256: str
    receipt_schema: Mapping[str, Any]

    @property
    def stages(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.raw["stages"])

    @property
    def tenant_cli(self) -> Mapping[str, Any]:
        return self.raw["tenant_cli"]

    @property
    def evolvable(self) -> dict[str, Mapping[str, Any]]:
        return {entry["target_path"]: entry for entry in self.raw["evolvable_paths"]}

    def stage_by_index(self, index: int) -> Mapping[str, Any]:
        for stage in self.stages:
            if stage["stage_index"] == index:
                return stage
        raise ScriptEvolutionError(f"policy: no stage with stage_index {index}")

    def stages_for_path(self, target_path: str) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            sorted(
                (s for s in self.stages if s["target_path"] == target_path),
                key=lambda s: s["ordinal"],
            )
        )


def load_policy(
    root: Path,
    *,
    expected_policy_sha256: str = PINNED_POLICY_SHA256,
    expected_policy_schema_sha256: str = PINNED_POLICY_SCHEMA_SHA256,
    expected_receipt_schema_sha256: str = PINNED_RECEIPT_SCHEMA_SHA256,
) -> Policy:
    """Load + pin-check + schema-check + semantically validate the policy."""

    policy_schema_bytes = read_required_bytes(root, POLICY_SCHEMA_REL, label="policy schema")
    actual = hashlib.sha256(policy_schema_bytes).hexdigest()
    if actual != expected_policy_schema_sha256:
        raise ScriptEvolutionError(
            "central pin drift: policy_v1.schema.json\n"
            f"  expected SHA (pinned in the guard helper): {expected_policy_schema_sha256}\n"
            f"  actual   SHA (current worktree):           {actual}"
        )

    receipt_schema_bytes = read_required_bytes(root, RECEIPT_SCHEMA_REL, label="receipt schema")
    actual = hashlib.sha256(receipt_schema_bytes).hexdigest()
    if actual != expected_receipt_schema_sha256:
        raise ScriptEvolutionError(
            "central pin drift: receipt_v1.schema.json\n"
            f"  expected SHA (pinned in the guard helper): {expected_receipt_schema_sha256}\n"
            f"  actual   SHA (current worktree):           {actual}"
        )

    policy_bytes = read_required_bytes(root, POLICY_REL, label="policy")
    policy_sha = hashlib.sha256(policy_bytes).hexdigest()
    if policy_sha != expected_policy_sha256:
        raise ScriptEvolutionError(
            "central pin drift: policy_v1.json\n"
            f"  expected SHA (pinned in the guard helper): {expected_policy_sha256}\n"
            f"  actual   SHA (current worktree):           {policy_sha}\n"
            "  A later RT must NEVER refresh this pin; the policy is frozen at Wave-0."
        )

    policy_schema = strict_json_bytes(policy_schema_bytes, label="policy schema")
    receipt_schema = strict_json_bytes(receipt_schema_bytes, label="receipt schema")
    policy = strict_json_bytes(policy_bytes, label="policy")
    validate_against_schema(policy_schema, policy, label="policy")

    _validate_policy_semantics(policy)
    return Policy(raw=policy, sha256=policy_sha, receipt_schema=receipt_schema)


def _validate_policy_semantics(policy: Mapping[str, Any]) -> None:
    stages = list(policy["stages"])
    indices = [s["stage_index"] for s in stages]
    if indices != list(range(1, len(stages) + 1)):
        raise ScriptEvolutionError(f"policy: stage_index must be 1..{len(stages)} in order, got {indices}")

    evolvable = {entry["target_path"]: entry for entry in policy["evolvable_paths"]}
    if len(evolvable) != len(policy["evolvable_paths"]):
        raise ScriptEvolutionError("policy: duplicate target_path in evolvable_paths")

    staged_paths = {s["target_path"] for s in stages}
    if staged_paths != set(evolvable):
        raise ScriptEvolutionError(
            "policy: stages and evolvable_paths disagree; "
            f"stages-only={sorted(staged_paths - set(evolvable))} "
            f"paths-only={sorted(set(evolvable) - staged_paths)}"
        )

    tenant_cli_path = policy["tenant_cli"]["target_path"]
    if tenant_cli_path not in evolvable:
        raise ScriptEvolutionError("policy: tenant_cli.target_path is not an evolvable path")

    companions = [entry["target_path"] for entry in policy["companion_immutable_paths"]]
    if len(set(companions)) != len(companions):
        raise ScriptEvolutionError(
            f"policy: duplicate target_path in companion_immutable_paths: {companions}"
        )
    # Checked before the exact-set rule so both branches stay reachable: an
    # overlap names the conflicting path, a substitution names the missing pin.
    for entry in policy["companion_immutable_paths"]:
        if entry["target_path"] in evolvable:
            raise ScriptEvolutionError(
                f"policy: {entry['target_path']} cannot be both evolvable and companion-immutable"
            )
    if tuple(sorted(companions)) != tuple(sorted(REQUIRED_COMPANION_PATHS)):
        raise ScriptEvolutionError(
            "policy: companion_immutable_paths must pin exactly "
            f"{sorted(REQUIRED_COMPANION_PATHS)}, got {sorted(companions)}.  These hold the "
            "CommandProviderV1 ABI and are absent from the RT-016 genesis table, so dropping "
            "or substituting one leaves the ABI unguarded."
        )

    receipt_paths: set[str] = set()
    note_paths: set[str] = set()
    for stage in stages:
        index = stage["stage_index"]
        owner = stage["owner_rt"]
        path = stage["target_path"]
        safe_relpath(path, label=f"policy stage {index} target_path")
        safe_relpath(stage["receipt_path"], label=f"policy stage {index} receipt_path")
        safe_relpath(stage["migration_note_path"], label=f"policy stage {index} migration_note_path")

        if not stage["receipt_path"].startswith(f"RT/{owner}/receipts/script-evolution/"):
            raise ScriptEvolutionError(f"policy: stage {index} receipt_path is not owned by {owner}")
        if not stage["migration_note_path"].startswith(f"RT/{owner}/reports/migrations/"):
            raise ScriptEvolutionError(f"policy: stage {index} migration_note_path is not owned by {owner}")
        if stage["acceptance_test_prefix"] != f"tests/test_rt0{owner[-2:]}_":
            raise ScriptEvolutionError(f"policy: stage {index} acceptance_test_prefix is not owned by {owner}")
        if stage["receipt_path"] in receipt_paths:
            raise ScriptEvolutionError(f"policy: duplicate receipt_path at stage {index}")
        if stage["migration_note_path"] in note_paths:
            raise ScriptEvolutionError(f"policy: duplicate migration_note_path at stage {index}")
        receipt_paths.add(stage["receipt_path"])
        note_paths.add(stage["migration_note_path"])

        if owner not in evolvable[path]["owner_rts"]:
            raise ScriptEvolutionError(
                f"policy: stage {index} owner {owner} is not an owner of {path}"
            )
        slot = stage["adds_provider_slot"]
        if path == tenant_cli_path:
            if not isinstance(slot, str):
                raise ScriptEvolutionError(f"policy: stage {index} on the tenant CLI must add a provider slot")
        elif slot is not None:
            raise ScriptEvolutionError(f"policy: stage {index} on {path} must not add a provider slot")

        requires = stage["requires_stage_index"]
        if requires is not None:
            if requires >= index:
                raise ScriptEvolutionError(f"policy: stage {index} requires non-earlier stage {requires}")
            required = next(s for s in stages if s["stage_index"] == requires)
            if required["target_path"] != path:
                raise ScriptEvolutionError(
                    f"policy: stage {index} requires stage {requires} on a different target path"
                )

    for path, entry in evolvable.items():
        ordinals = [s["ordinal"] for s in stages if s["target_path"] == path]
        if sorted(ordinals) != list(range(1, len(ordinals) + 1)):
            raise ScriptEvolutionError(f"policy: ordinals for {path} must be 1..n, got {sorted(ordinals)}")
        if entry["max_ordinal"] != len(ordinals):
            raise ScriptEvolutionError(
                f"policy: {path} declares max_ordinal={entry['max_ordinal']} but has {len(ordinals)} stages"
            )
        if not _SHA256_RE.match(entry["genesis_sha256"]):
            raise ScriptEvolutionError(f"policy: {path} genesis_sha256 is not lowercase hex")

    slots = [s["adds_provider_slot"] for s in stages if s["adds_provider_slot"] is not None]
    if len(set(slots)) != len(slots):
        raise ScriptEvolutionError("policy: duplicate adds_provider_slot across stages")
    baseline = list(policy["tenant_cli"]["baseline_slots"])
    if set(slots) & set(baseline):
        raise ScriptEvolutionError("policy: a stage re-adds a baseline provider slot")


# ---------------------------------------------------------------------------
# Receipt chain replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainResult:
    tips: Mapping[str, str]
    receipts_by_path: Mapping[str, tuple[Mapping[str, Any], ...]]
    tenant_cli_slots: tuple[str, ...]


def _list_dir(root: Path, parts: Sequence[str], *, label: str) -> list[tuple[str, os.stat_result]]:
    """List one directory through directory fds, or ``[]`` if it is absent."""

    walked = _walk_to_parent_fd(root, parts, label=label, missing_ok=True)
    if walked is None:
        return []
    parent_fd, leaf = walked
    try:
        if not _check_exact_name(parent_fd, leaf, label=label, missing_ok=True):
            return []
        pre = _lstat_at(leaf, parent_fd)
        if stat.S_ISLNK(pre.st_mode):
            raise ScriptEvolutionError(f"{label}: is a symlink")
        if not stat.S_ISDIR(pre.st_mode):
            raise ScriptEvolutionError(f"{label}: not a directory")
        try:
            fd = os.open(
                leaf, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent_fd
            )
        except OSError as exc:
            raise ScriptEvolutionError(
                f"{label}: cannot open ({exc.__class__.__name__})"
            ) from None
        try:
            post = os.fstat(fd)
            if (post.st_dev, post.st_ino) != (pre.st_dev, pre.st_ino):
                raise ScriptEvolutionError(f"{label}: directory swapped between stat and open")
            return [
                (name, _lstat_at(name, fd))
                for name in sorted(_listdir_fd(fd, label=label))
            ]
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _scan_for_undeclared_receipts(root: Path, declared: set[str]) -> None:
    for name, st in _list_dir(root, ("RT",), label="RT root"):
        if stat.S_ISLNK(st.st_mode):
            raise ScriptEvolutionError(f"RT/{name}: RT/ must not contain symlinks")
        if not stat.S_ISDIR(st.st_mode) or not _RT_DIR_RE.match(name):
            continue
        rel_dir = f"RT/{name}/receipts/script-evolution"
        entries = _list_dir(
            root, ("RT", name, "receipts", "script-evolution"), label=rel_dir
        )
        for leaf, leaf_st in entries:
            rel = f"{rel_dir}/{leaf}"
            if stat.S_ISLNK(leaf_st.st_mode):
                raise ScriptEvolutionError(f"{rel}: receipt must not be a symlink")
            if not stat.S_ISREG(leaf_st.st_mode):
                raise ScriptEvolutionError(
                    f"{rel}: receipt directory must contain only regular files"
                )
            if rel not in declared:
                raise ScriptEvolutionError(
                    f"{rel}: undeclared script-evolution receipt.  policy_v1.json predeclares "
                    "every legal receipt path; a receipt that is not one of them is rejected."
                )


def _resolve_acceptance_ref(root: Path, ref: str, stage: Mapping[str, Any], label: str) -> None:
    match = _TEST_REF_RE.match(ref)
    if match is None:
        raise ScriptEvolutionError(f"{label}: acceptance_test_ref {ref!r} is out of grammar")
    rel, rt_digits, class_name, method_name = match.groups()
    prefix = stage["acceptance_test_prefix"]
    if not rel.startswith(prefix):
        raise ScriptEvolutionError(
            f"{label}: acceptance_test_ref {ref!r} does not belong to {stage['owner_rt']} "
            f"(expected a file starting with {prefix!r})"
        )
    if rt_digits != stage["owner_rt"][-2:]:
        raise ScriptEvolutionError(
            f"{label}: acceptance_test_ref {ref!r} names RT-0{rt_digits}, not {stage['owner_rt']}"
        )
    data = read_checked_bytes(root, rel, label=f"{label} acceptance test", missing_ok=True)
    if data is None:
        raise ScriptEvolutionError(f"{label}: acceptance test file {rel} does not exist")
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        raise ScriptEvolutionError(f"{label}: acceptance test file {rel} does not parse") from None

    _assert_canonical_acceptance_test(tree, rel, class_name, method_name, label)


def _assert_canonical_acceptance_test(
    tree: ast.Module, rel: str, class_name: str, method_name: str, label: str
) -> None:
    """Require a *canonical* acceptance-test shape, not merely a plausible one.

    Earlier revisions tried to model Python's binding rules well enough to
    decide whether an arbitrary module would really produce a collectable,
    unskipped test.  Each round of review found another way through: a local
    ``class TestCase: pass`` read as the real one, ``@unittest.skip`` on an
    intermediate base inherited through the MRO, a class defined under ``if
    False:``, a second ``def`` of the same name silently winning.  Simulating
    the interpreter is the wrong tool -- for every rule there is one more
    dynamic construct.

    So the surface is narrowed instead of the analysis deepened.  A receipt may
    only cite a test whose shape is mechanically provable:

    * ``import unittest`` (or ``from unittest import TestCase``) as a direct
      top-level statement, and that name never rebound anywhere in the module;
    * the class a single direct ``ClassDef`` in ``module.body``, with no other
      binding of its name, no decorator, and exactly one base -- the proven
      ``unittest.TestCase``.  No local intermediate base, no multiple
      inheritance, no conditional or dynamic definition;
    * the method a single direct ``FunctionDef`` in that class body, with no
      other binding of its name and no decorator;
    * no ``__unittest_skip__``-family assignment anywhere in the module.

    Anything else is refused -- including shapes that would in fact work.  A
    refusal costs the owner RT a small edit to its acceptance test; a false
    accept costs the ledger its meaning.
    """

    _assert_no_skip_flag_assignment(tree, rel, label)
    _assert_not_deleted(tree, rel, (class_name, method_name), label)
    bindings = _collect_bindings(tree.body)
    ref = f"{rel}::{class_name}::{method_name}"

    direct = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name]
    bound = bindings.get(class_name, [])
    if not bound:
        raise ScriptEvolutionError(f"{label}: acceptance test {rel} has no class {class_name}")
    if len(bound) > 1:
        raise ScriptEvolutionError(
            f"{label}: acceptance test {rel} binds {class_name} {len(bound)} times "
            f"(as {', '.join(b.kind for b in bound)}); the last binding wins at import time, "
            "so a duplicate binding can hide the reviewed definition.  Exactly one is required."
        )
    if len(direct) != 1:
        raise ScriptEvolutionError(
            f"{label}: acceptance test {rel} binds {class_name} as {bound[0].kind}, not as a "
            "direct top-level class statement.  A conditional, nested or dynamically built "
            "class is refused: whether unittest ever collects it cannot be decided statically."
        )
    target = direct[0]

    _assert_no_opaque_decorator(target, "class", f"{rel}::{class_name}", label, bindings)
    if len(target.bases) != 1 or target.keywords:
        raise _reject_base(
            rel,
            class_name,
            f"it declares {len(target.bases)} bases; exactly one is required so that no "
            "ancestor can contribute a skip marker through the MRO",
            label,
        )
    if not _is_unittest_testcase_base(target.bases[0], bindings):
        raise _reject_base(
            rel, class_name, f"base {_describe_base(target.bases[0])} is not provably it", label
        )

    members = _collect_bindings(target.body)
    method_bound = members.get(method_name, [])
    direct_defs = [
        n
        for n in target.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method_name
    ]
    if not method_bound:
        raise ScriptEvolutionError(
            f"{label}: acceptance test {rel}::{class_name} has no method {method_name}"
        )
    if len(method_bound) > 1:
        raise ScriptEvolutionError(
            f"{label}: acceptance test {ref} is bound {len(method_bound)} times "
            f"(as {', '.join(b.kind for b in method_bound)}); Python keeps only the last, "
            "so an asserting definition can be silently overwritten by a no-op.  "
            "Exactly one is required."
        )
    if len(direct_defs) != 1:
        raise ScriptEvolutionError(
            f"{label}: acceptance test {ref} is bound as {method_bound[0].kind}, not as a "
            "direct def in the class body"
        )
    _assert_runnable_test_method(direct_defs[0], ref, label, bindings)


class _Binding(NamedTuple):
    """One name-binding statement found in a module or class body."""

    kind: str
    node: ast.AST
    module: str | None = None
    origin: str | None = None
    direct: bool = True


def _collect_bindings(body: Sequence[ast.stmt]) -> dict[str, list[_Binding]]:
    """Every name bound by *body*, without descending into nested scopes.

    Control flow (``if`` / ``try`` / ``for`` / ``while`` / ``with``) is walked
    because a binding hidden in one of those branches still rebinds the name at
    import time; class and function bodies are not, because those open a new
    scope.  Names are collected as *lists* so a duplicate binding is visible to
    the caller rather than silently overwritten the way a ``dict`` would.
    """

    found: dict[str, list[_Binding]] = {}

    def add(name: str, binding: _Binding) -> None:
        found.setdefault(name, []).append(binding)

    def target_names(node: ast.AST) -> list[str]:
        names: list[str] = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for item in node.elts:
                names.extend(target_names(item))
        elif isinstance(node, ast.Starred):
            names.extend(target_names(node.value))
        return names

    def walk(statements: Sequence[ast.stmt], direct: bool) -> None:
        for stmt in statements:
            if isinstance(stmt, ast.ClassDef):
                add(stmt.name, _Binding("class", stmt, direct=direct))
            elif isinstance(stmt, ast.FunctionDef):
                add(stmt.name, _Binding("def", stmt, direct=direct))
            elif isinstance(stmt, ast.AsyncFunctionDef):
                add(stmt.name, _Binding("async-def", stmt, direct=direct))
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.asname is None:
                        add(
                            alias.name.split(".")[0],
                            _Binding("import", stmt, alias.name, direct=direct),
                        )
                    else:
                        add(
                            alias.asname,
                            _Binding("import-as", stmt, alias.name, direct=direct),
                        )
            elif isinstance(stmt, ast.ImportFrom):
                module = stmt.module if stmt.level == 0 else None
                for alias in stmt.names:
                    kind = "from-import" if alias.asname is None else "from-import-as"
                    add(
                        alias.asname or alias.name,
                        _Binding(kind, stmt, module, alias.name, direct=direct),
                    )
            elif isinstance(stmt, ast.Delete):
                # ``del EvolutionTests`` leaves the ClassDef in the AST but
                # removes the name at import time, so unittest collects
                # nothing.  Counting the delete as a second binding event makes
                # the exactly-once rule reject the def/del pair.
                for tgt in stmt.targets:
                    for name in target_names(tgt):
                        add(name, _Binding("del", stmt, direct=direct))
            elif isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    for name in target_names(tgt):
                        add(name, _Binding("assignment", stmt, direct=direct))
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                for name in target_names(stmt.target):
                    add(name, _Binding("assignment", stmt, direct=direct))
            elif isinstance(stmt, (ast.If, ast.While)):
                walk(stmt.body, False)
                walk(stmt.orelse, False)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                for name in target_names(stmt.target):
                    add(name, _Binding("assignment", stmt, direct=False))
                walk(stmt.body, False)
                walk(stmt.orelse, False)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                for item in stmt.items:
                    if item.optional_vars is not None:
                        for name in target_names(item.optional_vars):
                            add(name, _Binding("assignment", stmt, direct=False))
                walk(stmt.body, False)
            elif isinstance(stmt, ast.Try):
                walk(stmt.body, False)
                for handler in stmt.handlers:
                    if handler.name:
                        add(handler.name, _Binding("assignment", handler, direct=False))
                    walk(handler.body, False)
                walk(stmt.orelse, False)
                walk(stmt.finalbody, False)

    walk(body, True)
    return found


def _bound_exactly_once(bindings: Mapping[str, list[_Binding]], name: str) -> _Binding | None:
    entries = bindings.get(name, [])
    return entries[0] if len(entries) == 1 else None


def _is_unittest_module_name(bindings: Mapping[str, list[_Binding]], name: str) -> bool:
    """True only for a name bound once, directly, by a bare ``import unittest``."""

    binding = _bound_exactly_once(bindings, name)
    if binding is None or binding.kind != "import" or not binding.direct:
        return False
    module = binding.module or ""
    return module == "unittest" or module.startswith("unittest.")


def _is_unittest_testcase_base(base: ast.expr, bindings: Mapping[str, list[_Binding]]) -> bool:
    """Resolve *base* to the real ``unittest.TestCase``, or refuse.

    Matching on the bare name ``TestCase`` is not enough: a module can define
    its own ``class TestCase: pass`` and inherit from that, which reads like a
    unittest test but is never collected.  Only two spellings are provably the
    real thing, and only when the name they hang off is bound exactly once:

    * ``unittest.TestCase`` after a bare ``import unittest``
    * ``TestCase`` after ``from unittest import TestCase``

    Aliases (``import unittest as ut``, ``from unittest import TestCase as
    TC``) are refused rather than resolved -- they are rare in real test files
    and cheap to avoid, and refusing keeps this resolver small enough to trust.
    """

    if isinstance(base, ast.Attribute):
        return (
            base.attr == "TestCase"
            and isinstance(base.value, ast.Name)
            and _is_unittest_module_name(bindings, base.value.id)
        )
    if isinstance(base, ast.Name):
        binding = _bound_exactly_once(bindings, base.id)
        return (
            binding is not None
            and binding.direct
            and binding.kind == "from-import"
            and binding.module == "unittest"
            and binding.origin == "TestCase"
        )
    return False


def _assert_not_deleted(
    tree: ast.Module, rel: str, names: Sequence[str], label: str
) -> None:
    """``del`` removes a name that the AST still shows as defined.

    ``del EvolutionTests`` after the class statement, or ``del test_stage_01``
    at the end of the class body, leaves a perfectly well-formed definition in
    the tree while ``unittest`` collects nothing.  ``del Klass.method`` does the
    same through an attribute, which no name-binding table sees at all, so
    every delete in the module is checked against the cited names.
    """

    wanted = set(names)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Delete):
            continue
        for target in ast.walk(node):
            hit = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else None
            )
            if hit in wanted:
                raise ScriptEvolutionError(
                    f"{label}: acceptance test {rel} deletes {hit}; the definition stays in "
                    "the source while unittest collects nothing, so a cited test can be "
                    "removed without touching the receipt"
                )


_SKIP_FLAG_NAMES = frozenset(
    {"__unittest_skip__", "__unittest_skip_why__", "__unittest_expecting_failure__"}
)


def _assert_no_skip_flag_assignment(tree: ast.Module, rel: str, label: str) -> None:
    """``__unittest_skip__ = True`` disables a test without any decorator.

    ``unittest`` reads these attributes off the class and off the bound method,
    so they can be set in a class body, or from anywhere in the module as
    ``Klass.test_x.__unittest_skip__ = True``.  The whole module is scanned
    because either spelling silences the very test a receipt cites as evidence.

    Assignment *targets* are unpacked rather than pattern-matched shallowly:
    ``(__unittest_skip__, marker) = (True, 1)`` binds the flag exactly like a
    plain assignment, and reading only ``Name``/``Attribute`` targets missed it.
    """

    def assigned_names(target: ast.expr) -> Iterable[str]:
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, ast.Attribute):
            yield target.attr
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                yield from assigned_names(item)
        elif isinstance(target, ast.Starred):
            yield from assigned_names(target.value)

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = [node.target]
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets = [node.target]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            targets = [i.optional_vars for i in node.items if i.optional_vars is not None]
        for target in targets:
            for name in assigned_names(target):
                if name in _SKIP_FLAG_NAMES:
                    raise ScriptEvolutionError(
                        f"{label}: acceptance test {rel} assigns {name}; that flag skips the "
                        "test without a decorator, and a skipped test is not evidence"
                    )


_SKIP_DECORATORS = frozenset({"skip", "skipIf", "skipUnless", "expectedFailure"})


def _expression_mentions_skip(node: ast.AST, bindings: Mapping[str, list[_Binding]]) -> bool:
    """True if *node* reaches a unittest skip helper by any visible route."""

    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in _SKIP_DECORATORS:
            return True
        if isinstance(sub, ast.Name):
            if sub.id in _SKIP_DECORATORS:
                return True
            binding = _bound_exactly_once(bindings, sub.id)
            if binding is not None and binding.origin in _SKIP_DECORATORS:
                return True  # from unittest import skip as <alias>
    return False


def _is_skip_decorator(decorator: ast.expr, bindings: Mapping[str, list[_Binding]]) -> bool:
    """Recognise ``@unittest.skip(...)``, ``@skipIf(...)`` and their aliases.

    An alias is the interesting case: ``disable = unittest.skip('later')``
    followed by ``@disable`` carries no skip name at the decoration site at
    all.  Every module-level binding of the decorator's root name is therefore
    followed back to its assigned expression.
    """

    if _expression_mentions_skip(decorator, bindings):
        return True
    root: ast.AST = decorator
    while isinstance(root, (ast.Call, ast.Attribute)):
        root = root.func if isinstance(root, ast.Call) else root.value
    if isinstance(root, ast.Name):
        for binding in bindings.get(root.id, []):
            if binding.origin in _SKIP_DECORATORS:
                return True
            if binding.kind == "assignment" and isinstance(
                binding.node, (ast.Assign, ast.AnnAssign, ast.AugAssign)
            ):
                value = binding.node.value
                if value is not None and _expression_mentions_skip(value, bindings):
                    return True
    return False


def _assert_no_opaque_decorator(
    node: ast.AST, what: str, ref: str, label: str, bindings: Mapping[str, list[_Binding]]
) -> None:
    """Reject every decorator that cannot be proven *not* to be a skip.

    Static analysis cannot follow an arbitrary callable, so ``@disable`` is
    indistinguishable from ``@unittest.skip('later')`` unless the alias happens
    to be resolvable in this module.  Rather than guess, an acceptance-test
    reference must name an undecorated class and an undecorated method; a
    recognised skip gets the specific message below, and anything else is
    refused as unprovable.
    """

    for decorator in getattr(node, "decorator_list", []):
        if _is_skip_decorator(decorator, bindings):
            raise ScriptEvolutionError(
                f"{label}: acceptance test {what} {ref} is decorated with "
                "skip/expectedFailure; a skipped test is not evidence that the migration works"
            )
        raise ScriptEvolutionError(
            f"{label}: acceptance test {what} {ref} carries decorator "
            f"{ast.dump(decorator)[:80]!r}, which cannot be statically proven not to be a skip.  "
            "Cite an undecorated test."
        )


def _reject_base(rel: str, name: str, detail: str, label: str) -> ScriptEvolutionError:
    return ScriptEvolutionError(
        f"{label}: acceptance test {rel}::{name} does not derive from unittest.TestCase, "
        f"so unittest will never collect it ({detail}).  Only 'unittest.TestCase' after "
        "'import unittest', 'TestCase' after 'from unittest import TestCase', or a local "
        "class that itself derives from one of those is accepted; a locally defined or "
        "aliased base of the same name is refused."
    )


def _describe_base(base: ast.expr) -> str:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return f"...{base.attr}"
    return type(base).__name__


def _assert_runnable_test_method(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    ref: str,
    label: str,
    bindings: Mapping[str, list[_Binding]],
) -> None:
    """Reject uncollectable, skipped and no-op acceptance tests.

    This is a **static shape check only**.  The guard deliberately never
    imports or executes an acceptance test: doing so from inside the RT-016
    zero-drift test would recurse (that test calls this guard).  Proving the
    referenced test actually PASSES remains the owner RT's job, in its own
    acceptance run; this check only makes an empty or disabled reference
    impossible to pass off as coverage.
    """

    if isinstance(node, ast.AsyncFunctionDef):
        raise ScriptEvolutionError(
            f"{label}: acceptance test {ref} is an async def; unittest cannot run it"
        )
    args = node.args
    positional = [a.arg for a in args.posonlyargs] + [a.arg for a in args.args]
    if positional[:1] != ["self"]:
        raise ScriptEvolutionError(
            f"{label}: acceptance test {ref} does not take 'self' as its first parameter"
        )
    required = len(positional) - 1 - len(args.defaults)
    if required > 0 or args.vararg is not None:
        raise ScriptEvolutionError(
            f"{label}: acceptance test {ref} takes extra required parameters, so "
            "unittest cannot invoke it"
        )
    if any(default is None for default in args.kw_defaults):
        raise ScriptEvolutionError(
            f"{label}: acceptance test {ref} has required keyword-only parameters, so "
            "unittest cannot invoke it"
        )
    _assert_no_opaque_decorator(node, "method", ref, label, bindings)

    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]  # drop the docstring
    meaningful = [
        stmt
        for stmt in body
        if not isinstance(stmt, ast.Pass)
        and not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        )
    ]
    if not meaningful:
        raise ScriptEvolutionError(
            f"{label}: acceptance test {ref} has an empty body (only pass/.../docstring); "
            "a no-op test is not evidence"
        )
    for stmt in meaningful:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Attribute) and (
                sub.attr.startswith("assert") or sub.attr in ("fail", "skipTest")
            ):
                if sub.attr == "skipTest":
                    raise ScriptEvolutionError(
                        f"{label}: acceptance test {ref} calls skipTest; "
                        "a skipped test is not evidence"
                    )
                return
    raise ScriptEvolutionError(
        f"{label}: acceptance test {ref} makes no assertion (no self.assert*/self.fail call); "
        "it cannot fail, so it is not evidence"
    )


def _verify_migration_note(root: Path, receipt: Mapping[str, Any], stage: Mapping[str, Any], label: str) -> None:
    rel = receipt["migration_note_path"]
    if rel != stage["migration_note_path"]:
        raise ScriptEvolutionError(
            f"{label}: migration_note_path {rel!r} != policy-declared {stage['migration_note_path']!r}"
        )
    data = read_checked_bytes(root, rel, label=f"{label} migration note", missing_ok=True)
    if data is None:
        raise ScriptEvolutionError(f"{label}: migration note {rel} is missing")
    actual = hashlib.sha256(data).hexdigest()
    if actual != receipt["migration_note_sha256"]:
        raise ScriptEvolutionError(
            f"{label}: migration note SHA mismatch for {rel}\n"
            f"  receipt says: {receipt['migration_note_sha256']}\n"
            f"  actual bytes: {actual}"
        )
    if len(data) < MIN_MIGRATION_NOTE_BYTES:
        raise ScriptEvolutionError(
            f"{label}: migration note {rel} is only {len(data)} bytes; "
            f"at least {MIN_MIGRATION_NOTE_BYTES} are required"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ScriptEvolutionError(f"{label}: migration note {rel} is not valid UTF-8") from None
    if stage["owner_rt"] not in text:
        raise ScriptEvolutionError(f"{label}: migration note {rel} never mentions {stage['owner_rt']}")
    basename = stage["target_path"].rsplit("/", 1)[-1]
    if basename not in text:
        raise ScriptEvolutionError(f"{label}: migration note {rel} never mentions {basename}")


def replay_chain(root: Path, policy: Policy, *, genesis: Mapping[str, str]) -> ChainResult:
    """Replay every declared receipt and return each path's expected tip SHA."""

    declared = {stage["receipt_path"] for stage in policy.stages}
    _scan_for_undeclared_receipts(root, declared)

    evolvable = policy.evolvable
    tips: dict[str, str] = {}
    receipts_by_path: dict[str, tuple[Mapping[str, Any], ...]] = {}
    present_stage_indices: set[int] = set()
    tenant_cli_path = policy.tenant_cli["target_path"]
    tenant_cli_slots = list(policy.tenant_cli["baseline_slots"])

    for target_path, entry in sorted(evolvable.items()):
        stages = policy.stages_for_path(target_path)
        tip = entry["genesis_sha256"]
        declared_genesis = genesis.get(target_path)
        if declared_genesis is None:
            raise ScriptEvolutionError(
                f"policy: evolvable path {target_path} is not present in the genesis table"
            )
        if declared_genesis != tip:
            raise ScriptEvolutionError(
                f"policy/genesis disagreement for {target_path}\n"
                f"  policy genesis_sha256: {tip}\n"
                f"  genesis table:         {declared_genesis}"
            )

        chain: list[Mapping[str, Any]] = []
        previous_bytes: bytes | None = None
        seen_absent = False
        for stage in stages:
            rel = stage["receipt_path"]
            label = f"receipt stage {stage['stage_index']} ({rel})"
            raw = read_checked_bytes(root, rel, label=label, missing_ok=True)
            if raw is None:
                # Chains may be short, but never gapped.
                seen_absent = True
                continue

            # Checked BEFORE the generic gap rule so the diagnostic names the
            # RT that is being skipped.  Both branches stay live: a stage with
            # requires_stage_index=null still falls through to the gap check.
            requires = stage["requires_stage_index"]
            if requires is not None and requires not in present_stage_indices:
                required_stage = policy.stage_by_index(requires)
                raise ScriptEvolutionError(
                    f"{label}: stage {stage['stage_index']} ({stage['owner_rt']}) requires stage "
                    f"{requires} ({required_stage['owner_rt']}) on {required_stage['target_path']}, "
                    "which has no receipt yet"
                )

            if seen_absent:
                raise ScriptEvolutionError(
                    f"{label}: receipt chain for {target_path} is gapped — ordinal "
                    f"{stage['ordinal']} is present but an earlier ordinal is missing. "
                    "Receipts may be absent, but only as a closed prefix."
                )

            receipt = strict_json_bytes(raw, label=label)
            validate_against_schema(policy.receipt_schema, receipt, label=label)

            if receipt["policy_id"] != policy.raw["policy_id"]:
                raise ScriptEvolutionError(f"{label}: policy_id mismatch")
            if receipt["policy_sha256"] != policy.sha256:
                raise ScriptEvolutionError(
                    f"{label}: receipt is bound to a different policy\n"
                    f"  receipt policy_sha256: {receipt['policy_sha256']}\n"
                    f"  actual policy SHA:     {policy.sha256}"
                )
            for field in ("stage_index", "owner_rt", "target_path", "ordinal", "adds_provider_slot"):
                if receipt[field] != stage[field]:
                    raise ScriptEvolutionError(
                        f"{label}: {field} is {receipt[field]!r} but the policy declares "
                        f"{stage[field]!r} for this receipt path"
                    )

            if receipt["from_sha256"] == receipt["to_sha256"]:
                raise ScriptEvolutionError(f"{label}: from_sha256 == to_sha256 (no-op receipt)")
            if receipt["from_sha256"] != tip:
                raise ScriptEvolutionError(
                    f"{label}: from_sha256 does not continue the chain for {target_path}\n"
                    f"  receipt from_sha256: {receipt['from_sha256']}\n"
                    f"  expected chain tip:  {tip}"
                )

            if previous_bytes is None:
                expected_link = genesis_link(
                    domain=policy.raw["genesis_link_domain"],
                    policy_sha256=policy.sha256,
                    target_path=target_path,
                )
                link_label = "domain-separated genesis link"
            else:
                expected_link = hashlib.sha256(previous_bytes).hexdigest()
                link_label = "SHA-256 of the previous receipt's raw bytes"
            if receipt["previous_receipt_sha256"] != expected_link:
                raise ScriptEvolutionError(
                    f"{label}: previous_receipt_sha256 is broken\n"
                    f"  receipt says: {receipt['previous_receipt_sha256']}\n"
                    f"  expected ({link_label}): {expected_link}"
                )

            _verify_migration_note(root, receipt, stage, label)
            refs = receipt["acceptance_test_refs"]
            for ref in refs:
                _resolve_acceptance_ref(root, ref, stage, label)

            if target_path == tenant_cli_path:
                slot = receipt["adds_provider_slot"]
                if slot in tenant_cli_slots:
                    raise ScriptEvolutionError(f"{label}: provider slot {slot!r} is already registered")
                tenant_cli_slots.append(slot)

            tip = receipt["to_sha256"]
            chain.append(receipt)
            present_stage_indices.add(stage["stage_index"])
            previous_bytes = raw

        tips[target_path] = tip
        receipts_by_path[target_path] = tuple(chain)

    return ChainResult(
        tips=tips,
        receipts_by_path=receipts_by_path,
        tenant_cli_slots=tuple(tenant_cli_slots),
    )


# ---------------------------------------------------------------------------
# Tenant CLI AST + comment fingerprints
# ---------------------------------------------------------------------------

# Explicit frozen node/field table for scripts/cwk_tenant_cli.py, generated
# under CPython 3.11.  An unknown node type or a changed field set raises
# instead of silently rotating the pinned fingerprint.
_AST_FIELDS: dict[str, tuple[str, ...]] = {
    "AnnAssign": ("target", "annotation", "value", "simple"),
    "Assign": ("targets", "value"),
    "Attribute": ("value", "attr", "ctx"),
    "BinOp": ("left", "op", "right"),
    "BitOr": (),
    "BoolOp": ("op", "values"),
    "Call": ("func", "args", "keywords"),
    "ClassDef": ("name", "bases", "keywords", "body", "decorator_list"),
    "Compare": ("left", "ops", "comparators"),
    "Constant": ("value", "kind"),
    "Continue": (),
    "Dict": ("keys", "values"),
    "DictComp": ("key", "value", "generators"),
    "Div": (),
    "Eq": (),
    "ExceptHandler": ("type", "name", "body"),
    "Expr": ("value",),
    "For": ("target", "iter", "body", "orelse"),
    "FormattedValue": ("value", "conversion", "format_spec"),
    "FunctionDef": ("name", "args", "body", "decorator_list", "returns"),
    "GtE": (),
    "If": ("test", "body", "orelse"),
    "IfExp": ("test", "body", "orelse"),
    "Import": ("names",),
    "ImportFrom": ("module", "names", "level"),
    "In": (),
    "Is": (),
    "IsNot": (),
    "JoinedStr": ("values",),
    "List": ("elts", "ctx"),
    "ListComp": ("elt", "generators"),
    "Load": (),
    "Module": ("body", "type_ignores"),
    "Name": ("id", "ctx"),
    "Not": (),
    "NotEq": (),
    "NotIn": (),
    "Or": (),
    "Raise": ("exc", "cause"),
    "Return": ("value",),
    "Slice": ("lower", "upper", "step"),
    "Store": (),
    "Subscript": ("value", "slice", "ctx"),
    "Try": ("body", "handlers", "orelse", "finalbody"),
    "Tuple": ("elts", "ctx"),
    "USub": (),
    "UnaryOp": ("op", "operand"),
    "alias": ("name", "asname"),
    "arg": ("arg", "annotation"),
    "arguments": (
        "posonlyargs",
        "args",
        "vararg",
        "kwonlyargs",
        "kw_defaults",
        "kwarg",
        "defaults",
    ),
    "comprehension": ("target", "iter", "ifs", "is_async"),
    "keyword": ("arg", "value"),
}

# Fields that exist only on some CPython releases and are therefore omitted
# from the fingerprint -- but ONLY after proving they hold their default.
# Omitting them unconditionally was a real hole: PEP 695 lets
# ``class C[T]:`` / ``def f[T]():`` carry their whole generic signature in
# ``type_params``, so a generic could be introduced on 3.12+ without moving
# the fingerprint at all.  Now a non-default value fails loudly instead.
#
# ``()`` means "must be an empty list"; ``None`` means "must be None".
_AST_DEFAULT_ONLY_FIELDS: dict[str, Any] = {
    "type_comment": None,  # we parse with type_comments=False
    "type_params": [],  # PEP 695 generics: not permitted in the frozen file
}


def _assert_default_only_fields(node: ast.AST, name: str) -> None:
    for field, default in _AST_DEFAULT_ONLY_FIELDS.items():
        if field not in node._fields:
            continue
        value = getattr(node, field, default)
        if value != default:
            raise ScriptEvolutionError(
                f"tenant CLI AST: node {name!r} sets {field}={value!r}, but the frozen "
                f"fingerprint only omits {field} when it holds its default {default!r}.  "
                "PEP 695 type parameters and type comments are not permitted in this file; "
                "they would otherwise carry semantics the fingerprint cannot see."
            )


_SLOT_PLACEHOLDER = "@SLOT@"


def _serialise_scalar(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("none")
        return
    if value is Ellipsis:
        out.append("ellipsis")
        return
    if isinstance(value, bool):  # MUST precede int: True == 1
        out.append("bool:1" if value else "bool:0")
        return
    if isinstance(value, int):
        out.append(f"int:{value}")
        return
    if isinstance(value, float):
        out.append(f"float:{value!r}")
        return
    if isinstance(value, complex):
        out.append(f"complex:{value!r}")
        return
    if isinstance(value, str):
        # Length-prefixed hex of the exact UTF-8 bytes: no delimiter injection
        # via a string literal, and NFC/NFD substitutions are visible.
        raw = value.encode("utf-8")
        out.append(f"s{len(raw)}:{raw.hex()}")
        return
    if isinstance(value, bytes):
        out.append(f"b{len(value)}:{value.hex()}")
        return
    raise ScriptEvolutionError(f"tenant CLI AST: unsupported scalar type {type(value).__name__}")


def _serialise(node: Any, slot_value: Any, out: list[str]) -> None:
    if slot_value is not None and node is slot_value:
        out.append(_SLOT_PLACEHOLDER)
        return
    if isinstance(node, ast.AST):
        name = type(node).__name__
        expected = _AST_FIELDS.get(name)
        if expected is None:
            raise ScriptEvolutionError(
                f"tenant CLI AST: unsupported node type {name!r}; the frozen dispatcher "
                "must not gain new syntax constructs"
            )
        _assert_default_only_fields(node, name)
        actual = tuple(f for f in node._fields if f not in _AST_DEFAULT_ONLY_FIELDS)
        if actual != expected:
            raise ScriptEvolutionError(
                f"tenant CLI AST: node {name!r} field set is {actual} but the frozen table "
                f"says {expected}; regenerate _AST_FIELDS deliberately, never silently"
            )
        out.append("N:" + name + "(")
        for field in expected:
            out.append(field + "=")
            _serialise(getattr(node, field), slot_value, out)
            out.append(";")
        out.append(")")
        return
    if isinstance(node, list):
        out.append(f"L{len(node)}[")
        for item in node:
            _serialise(item, slot_value, out)
            out.append(",")
        out.append("]")
        return
    _serialise_scalar(node, out)


@dataclass(frozen=True)
class TenantCliShape:
    ast_fingerprint: str
    comment_fingerprint: str
    slots: tuple[str, ...]
    annotation: str
    span: tuple[int, int]


def tenant_cli_shape(
    text: str,
    *,
    slot_symbol: str,
    slot_name_pattern: str,
    max_span_lines: int,
    label: str = "scripts/cwk_tenant_cli.py",
) -> TenantCliShape:
    """Compute both fingerprints plus the structured slot list."""

    if "﻿" in text:
        raise ScriptEvolutionError(f"{label}: source must not contain a byte-order mark")
    if "\r" in text:
        raise ScriptEvolutionError(f"{label}: source must use LF line endings")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ScriptEvolutionError(f"{label}: source does not parse ({exc.msg})") from None

    slot_nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == slot_symbol
    ]
    if len(slot_nodes) != 1:
        raise ScriptEvolutionError(
            f"{label}: expected exactly one module-level annotated assignment to "
            f"{slot_symbol}, found {len(slot_nodes)}"
        )
    slot_node = slot_nodes[0]

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id == slot_symbol
            and isinstance(node.ctx, ast.Store)
            and node is not slot_node.target
        ):
            raise ScriptEvolutionError(f"{label}: {slot_symbol} is rebound elsewhere in the module")

    annotation = ast.unparse(slot_node.annotation)
    value = slot_node.value
    if not isinstance(value, ast.Tuple) or not isinstance(value.ctx, ast.Load):
        raise ScriptEvolutionError(f"{label}: {slot_symbol} must be assigned a tuple literal")

    pattern = re.compile(slot_name_pattern)
    slots: list[str] = []
    for element in value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise ScriptEvolutionError(f"{label}: {slot_symbol} must contain only string literals")
        if element.kind is not None:
            raise ScriptEvolutionError(f"{label}: {slot_symbol} must contain only plain string literals")
        if not pattern.match(element.value):
            raise ScriptEvolutionError(
                f"{label}: provider slot {element.value!r} is out of grammar {slot_name_pattern!r}"
            )
        if element.value in slots:
            raise ScriptEvolutionError(f"{label}: duplicate provider slot {element.value!r}")
        slots.append(element.value)

    start, end = slot_node.lineno, slot_node.end_lineno or slot_node.lineno
    if end - start + 1 > max_span_lines:
        raise ScriptEvolutionError(
            f"{label}: the {slot_symbol} assignment spans {end - start + 1} lines; "
            f"at most {max_span_lines} are allowed (comments inside the span are unpinned)"
        )

    out: list[str] = []
    _serialise(tree, value, out)
    ast_fingerprint = hashlib.sha256("".join(out).encode("utf-8")).hexdigest()

    # Comments are invisible to the AST, and the slot's neighbouring comment
    # lines are exactly what RT-019 / RT-026 edit.  Freeze every comment
    # OUTSIDE the assignment span together with its *normalised position* and
    # adjacency, so that moving an unchanged comment -- or sliding a
    # "# type: ignore" / "# noqa" onto a different statement -- rotates the
    # fingerprint.  Hashing the comment texts alone was not enough: the
    # multiset of texts is invariant under exactly those moves.
    #
    # Normalisation: positions above the slot assignment are absolute (the
    # span is below them, so they cannot shift); positions below it are
    # measured relative to the span's last line.  Growing the tuple by one
    # slot therefore leaves every recorded position untouched, while any real
    # move changes one.
    comments: list[tuple[str, int, int, str, str]] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            row, col = token.start
            if start <= row <= end:
                continue
            if row < start:
                region, offset = "above", row
            else:
                region, offset = "below", row - end
            attachment = "inline" if token.line[:col].strip() else "own-line"
            comments.append((region, offset, col, attachment, token.string))
    except (tokenize.TokenError, IndentationError) as exc:
        raise ScriptEvolutionError(
            f"{label}: source does not tokenise ({exc.__class__.__name__})"
        ) from None
    digest = hashlib.sha256()
    for region, offset, col, attachment, comment in comments:
        for part in (region, str(offset), str(col), attachment, comment):
            raw = part.encode("utf-8")
            digest.update(len(raw).to_bytes(4, "big"))
            digest.update(raw)
    comment_fingerprint = digest.hexdigest()

    return TenantCliShape(
        ast_fingerprint=ast_fingerprint,
        comment_fingerprint=comment_fingerprint,
        slots=tuple(slots),
        annotation=annotation,
        span=(start, end),
    )


def verify_tenant_cli(
    root: Path,
    policy: Policy,
    expected_slots: Sequence[str],
    *,
    ast_fingerprint: str = PINNED_TENANT_CLI_AST_FINGERPRINT,
    comment_fingerprint: str = PINNED_TENANT_CLI_COMMENT_FINGERPRINT,
) -> TenantCliShape:
    """Central AST guard: only ordered slot appends are tolerated."""

    spec = policy.tenant_cli
    rel = spec["target_path"]
    data = read_required_bytes(root, rel, label=rel)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ScriptEvolutionError(f"{rel}: source is not valid UTF-8") from None

    shape = tenant_cli_shape(
        text,
        slot_symbol=spec["slot_symbol"],
        slot_name_pattern=spec["slot_name_pattern"],
        max_span_lines=spec["max_slot_span_lines"],
        label=rel,
    )

    if shape.annotation != spec["slot_annotation"]:
        raise ScriptEvolutionError(
            f"{rel}: {spec['slot_symbol']} annotation drifted: "
            f"{shape.annotation!r} != {spec['slot_annotation']!r}"
        )
    baseline = tuple(spec["baseline_slots"])
    if shape.slots[: len(baseline)] != baseline:
        raise ScriptEvolutionError(
            f"{rel}: the Wave-0 baseline slots are no longer a prefix: "
            f"{shape.slots!r} does not start with {baseline!r}"
        )
    if shape.slots != tuple(expected_slots):
        raise ScriptEvolutionError(
            f"{rel}: provider slots do not match the receipt chain\n"
            f"  slots on disk:            {list(shape.slots)}\n"
            f"  slots the chain allows:   {list(expected_slots)}\n"
            "  Slots may only be appended one per policy-declared receipt; reordering, "
            "deleting, duplicating or adding two at once is rejected."
        )
    if shape.ast_fingerprint != ast_fingerprint:
        raise ScriptEvolutionError(
            f"{rel}: AST fingerprint drifted outside the provider-slot tuple\n"
            f"  expected (Wave-0 baseline): {ast_fingerprint}\n"
            f"  actual   (current worktree): {shape.ast_fingerprint}\n"
            "  Loader, CommandProviderV1 ABI, error semantics, security checks and "
            "docstrings are all frozen; only the slot tuple may change."
        )
    if shape.comment_fingerprint != comment_fingerprint:
        raise ScriptEvolutionError(
            f"{rel}: comments outside the provider-slot assignment drifted\n"
            f"  expected (Wave-0 baseline): {comment_fingerprint}\n"
            f"  actual   (current worktree): {shape.comment_fingerprint}\n"
            "  Only comments inside the slot tuple may be edited."
        )
    return shape


# ---------------------------------------------------------------------------
# Top-level verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Report:
    tips: Mapping[str, str]
    receipt_count: int
    tenant_cli_slots: tuple[str, ...]
    immutable_count: int


def verify_evolution(
    root: Path,
    *,
    genesis: Mapping[str, str],
    policy: Policy,
    tenant_cli_ast_fingerprint: str = PINNED_TENANT_CLI_AST_FINGERPRINT,
    tenant_cli_comment_fingerprint: str = PINNED_TENANT_CLI_COMMENT_FINGERPRINT,
) -> Report:
    """Pure verification against an explicit root, genesis table and policy."""

    if len(genesis) != GENESIS_ENTRY_COUNT:
        raise ScriptEvolutionError(
            f"genesis table must have exactly {GENESIS_ENTRY_COUNT} entries, got {len(genesis)}"
        )
    if policy.raw["genesis_entry_count"] != len(genesis):
        raise ScriptEvolutionError(
            f"policy declares genesis_entry_count={policy.raw['genesis_entry_count']} "
            f"but the genesis table has {len(genesis)} entries"
        )
    manifest = C.canonical_sha256(dict(genesis))
    if manifest != policy.raw["genesis_manifest_sha256"]:
        raise ScriptEvolutionError(
            "genesis manifest drift: the pinned genesis table no longer hashes to the value "
            "frozen in policy_v1.json\n"
            f"  policy genesis_manifest_sha256: {policy.raw['genesis_manifest_sha256']}\n"
            f"  actual manifest SHA:            {manifest}\n"
            "  Refreshing a baseline SHA in bulk is exactly what this pin exists to catch."
        )
    for rel, sha in genesis.items():
        safe_relpath(rel, label="genesis entry")
        if not _SHA256_RE.match(sha):
            raise ScriptEvolutionError(f"genesis entry {rel}: SHA is not lowercase hex")

    chain = replay_chain(root, policy, genesis=genesis)

    drifted: list[str] = []
    for rel in sorted(genesis):
        expected = chain.tips.get(rel, genesis[rel])
        actual = file_sha256(root, rel)
        if actual == expected:
            continue
        if rel in chain.tips:
            receipts = chain.receipts_by_path.get(rel, ())
            if receipts:
                detail = (
                    f"  expected SHA (tip of {len(receipts)} receipt(s)): {expected}\n"
                    f"  actual   SHA (current worktree):                 {actual}"
                )
            else:
                detail = (
                    f"  expected SHA (genesis; no evolution receipt yet): {expected}\n"
                    f"  actual   SHA (current worktree):                  {actual}\n"
                    "  This path IS evolvable, but only by appending its policy-declared receipt."
                )
        else:
            detail = (
                f"  expected SHA (pinned genesis, permanently immutable): {expected}\n"
                f"  actual   SHA (current worktree):                      {actual}\n"
                "  This path is NOT evolvable under policy_v1.json."
            )
        drifted.append(f"{rel}\n{detail}")
    if drifted:
        raise ScriptEvolutionError("frozen file drift detected:\n" + "\n".join(drifted))

    for entry in policy.raw["companion_immutable_paths"]:
        rel = entry["target_path"]
        actual = file_sha256(root, rel)
        if actual != entry["sha256"]:
            raise ScriptEvolutionError(
                f"companion immutable file drifted: {rel}\n"
                f"  expected SHA (pinned in policy_v1.json): {entry['sha256']}\n"
                f"  actual   SHA (current worktree):         {actual}\n"
                f"  Reason this file is pinned: {entry['reason']}"
            )

    verify_tenant_cli(
        root,
        policy,
        chain.tenant_cli_slots,
        ast_fingerprint=tenant_cli_ast_fingerprint,
        comment_fingerprint=tenant_cli_comment_fingerprint,
    )

    return Report(
        tips=chain.tips,
        receipt_count=sum(len(v) for v in chain.receipts_by_path.values()),
        tenant_cli_slots=chain.tenant_cli_slots,
        immutable_count=len(genesis) - len(chain.tips),
    )


def verify_repo(
    root: Path,
    genesis: Mapping[str, str],
    *,
    expected_policy_sha256: str = PINNED_POLICY_SHA256,
    expected_policy_schema_sha256: str = PINNED_POLICY_SCHEMA_SHA256,
    expected_receipt_schema_sha256: str = PINNED_RECEIPT_SCHEMA_SHA256,
    tenant_cli_ast_fingerprint: str = PINNED_TENANT_CLI_AST_FINGERPRINT,
    tenant_cli_comment_fingerprint: str = PINNED_TENANT_CLI_COMMENT_FINGERPRINT,
) -> Report:
    """Load the pinned policy under ``root`` and verify everything."""

    policy = load_policy(
        root,
        expected_policy_sha256=expected_policy_sha256,
        expected_policy_schema_sha256=expected_policy_schema_sha256,
        expected_receipt_schema_sha256=expected_receipt_schema_sha256,
    )
    return verify_evolution(
        root,
        genesis=genesis,
        policy=policy,
        tenant_cli_ast_fingerprint=tenant_cli_ast_fingerprint,
        tenant_cli_comment_fingerprint=tenant_cli_comment_fingerprint,
    )


def assert_frozen_baseline(root: Path, genesis: Mapping[str, str]) -> Report:
    """Entry point used by ``tests/test_rt016_schemas.py``.

    Replaces the plain byte-identity assertion with the append-only guard:
    the 17 non-evolvable genesis entries must still be byte-identical, and
    each of the 9 evolvable ones must equal its receipt-chain tip (which is
    its genesis SHA while no receipt exists).
    """

    return verify_repo(Path(root), genesis)


__all__ = [
    "ChainResult",
    "Policy",
    "Report",
    "ScriptEvolutionError",
    "TenantCliShape",
    "assert_frozen_baseline",
    "file_sha256",
    "genesis_link",
    "load_policy",
    "read_checked_bytes",
    "replay_chain",
    "safe_relpath",
    "strict_json_bytes",
    "tenant_cli_shape",
    "verify_evolution",
    "verify_repo",
    "verify_tenant_cli",
]
