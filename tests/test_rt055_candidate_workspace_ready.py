"""Public READY evidence contract; no OPS access or candidate execution."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import rt055_zero_exposure as zero


class ReadyEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = ROOT / 'RT/RT-055/evidence'
        cls.value = json.loads((base / 'candidate-workspace-ready.json').read_text())
        schema = json.loads((base / 'candidate-workspace-ready.schema.json').read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        cls.validator = jsonschema.Draft202012Validator(schema)

    def test_actual_ready_projection_is_valid_and_counts_are_consistent(self):
        self.validator.validate(self.value)
        row = self.value['independent_verification']
        self.assertEqual(row['new_archived_files'], self.value['archived_original_files'])
        self.assertEqual(row['previous_archived_files'], self.value['previous_archived_original_files'])
        self.assertEqual(row['failed_synthetic_predecessors'], len(self.value['preserved_synthetic_failures']))
        self.assertEqual(row['global_legacy_claims'], self.value['old_claims_retained'])
        self.assertEqual(row['new_legacy_claims'], 0)

    def test_no_required_field_can_disappear(self):
        for key in self.value:
            with self.subTest(key=key):
                bad = copy.deepcopy(self.value)
                del bad[key]
                self.assertFalse(self.validator.is_valid(bad))

    def test_unknown_private_fields_rejected_at_every_object_depth(self):
        paths = [(), ('independent_verification',), ('privacy_observations',),
                 ('workspace_probe',), ('workspace_probe', 'policies', 0),
                 ('preserved_synthetic_failures', 0), ('verifier_reconciliation',)]
        for path in paths:
            for key in ('query', 'title', 'filename', 'path', 'hash', 'private_digest'):
                with self.subTest(path=path, key=key):
                    bad = copy.deepcopy(self.value)
                    target = bad
                    for part in path:
                        target = target[part]
                    target[key] = 'PUBLIC_NEGATIVE'
                    self.assertFalse(self.validator.is_valid(bad))

    def test_nonzero_formal_activity_or_replayed_after_is_rejected(self):
        for key in ('new_attempts', 'new_arms', 'new_exposure', 'new_queries',
                    'new_scores', 'new_results', 'new_after', 'new_legacy_claims'):
            with self.subTest(key=key):
                bad = copy.deepcopy(self.value)
                bad['independent_verification'][key] = 1
                self.assertFalse(self.validator.is_valid(bad))

    def test_history_or_cleanup_loss_is_rejected(self):
        for key in ('global_legacy_claims', 'global_voids'):
            bad = copy.deepcopy(self.value)
            bad['independent_verification'][key] = 0
            self.assertFalse(self.validator.is_valid(bad))
        for action in ('remove', 'cleanup', 'rewrite_status'):
            bad = copy.deepcopy(self.value)
            if action == 'remove':
                bad['preserved_synthetic_failures'].pop()
            elif action == 'cleanup':
                bad['preserved_synthetic_failures'][0]['cleanup_zero'] = False
            else:
                bad['preserved_synthetic_failures'][0]['status'] = 'PASS'
            self.assertFalse(self.validator.is_valid(bad))

    def test_order_source_and_archive_byte_drift_are_rejected(self):
        bad = copy.deepcopy(self.value)
        bad['run_order'].reverse()
        self.assertFalse(self.validator.is_valid(bad))
        bad = copy.deepcopy(self.value)
        bad['source_commit'] = '0' * 40
        self.assertFalse(self.validator.is_valid(bad))
        for key in ('new_archive_bytes', 'previous_archive_bytes', 'retained_bytes'):
            bad = copy.deepcopy(self.value)
            bad['independent_verification'][key] += 1
            self.assertFalse(self.validator.is_valid(bad))


class CountScopeTests(unittest.TestCase):
    def test_legacy_claim_is_global_while_attempts_are_window_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old, new = str(uuid.uuid4()), str(uuid.uuid4())
            claim = root / 'consumption/a/public.claim'
            claim.parent.mkdir(parents=True)
            claim.write_text(json.dumps({'window_id': old}))
            attempt = root / 'formal-windows' / old / 'run-a/attempts' / str(uuid.uuid4()) / 'claim.json'
            attempt.parent.mkdir(parents=True)
            attempt.write_text('{}')
            self.assertEqual(zero._counts(root, old)['a']['attempts'], 1)
            self.assertEqual(zero._counts(root, new)['a'],
                             {'claims': 1, 'attempts': 0, 'scores': 0, 'results': 0})
            self.assertTrue(claim.is_file())

    def test_new_attempt_score_and_result_are_counted_not_hidden_by_old_claim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wid = str(uuid.uuid4())
            base = root / 'formal-windows' / wid / 'run-a'
            for rel in ('attempts/' + str(uuid.uuid4()) + '/claim.json', 'scores/public.json', 'result.json'):
                dest = base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text('{}')
            self.assertEqual(zero._counts(root, wid)['a'],
                             {'claims': 0, 'attempts': 1, 'scores': 1, 'results': 1})


if __name__ == '__main__':
    unittest.main()
