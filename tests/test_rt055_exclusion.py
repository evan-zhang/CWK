"""Synthetic-only behavioral R gates; never connect to OPS or load private data."""
import copy
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'scripts'))
import rt055_builder as builder
import rt055_verifier as verifier
import rt055_exclusion as exclusion


def document(number=1):
    return {'doc_id': 'synthetic-' + str(number), 'title': 'Synthetic title ' + str(number),
            'filename': 'synthetic' + str(number) + '.md',
            'body': '仅供测试来源编号 SYN-' + str(number) + '\n独有内容甲乙丙丁戊己庚辛壬癸' + str(number)}


def authority(field=None, value=None, origin='synthetic-1'):
    result = {'authority_version': exclusion.AUTHORITY_VERSION,
              'doc_ids': [], 'queries': [], 'tokens': [], 'members': []}
    if field:
        result[field].append(value)
        result['members'].append({'kind': field, 'value': value, 'source_ids': [origin]})
    return result


class BuilderBehavior(unittest.TestCase):
    def test_source_doc_id_is_excluded_before_emission(self):
        doc = document()
        result = builder.derive_cases_for_library('cwork-3m', [doc], 'synthetic-seed',
                                                  exclusion=authority('doc_ids', doc['doc_id']))
        self.assertEqual(result['cases'], [])
        self.assertEqual(len(result['excluded_sources']), 1)

    def test_source_query_match_excludes_other_queries_too(self):
        doc = document()
        r = authority('queries', exclusion.normalize(doc['title']))
        result = builder.derive_cases_for_library('cwork-3m', [doc], 'synthetic-seed', exclusion=r)
        self.assertEqual(result['cases'], [])
        self.assertEqual(len(result['excluded_sources']), 1)

    def test_source_token_match_excludes_source_before_selection(self):
        doc = document()
        result = builder.derive_cases_for_library('cwork-3m', [doc], 'synthetic-seed',
                                                  exclusion=authority('tokens', 'syn-1'))
        self.assertEqual(result['cases'], [])
        self.assertEqual(len(result['excluded_sources']), 1)

    def test_eligible_filter_does_not_remove_full_corpus_uniqueness_checks(self):
        a, b = document(1), document(2)
        b['title'] = a['title']
        result = builder.derive_cases_for_library('cwork-3m', [a, b], 'synthetic-seed',
                                                  exclusion=authority(), eligible_ids={a['doc_id']})
        self.assertFalse(any(row['query'] == a['title'].casefold() for row in result['cases']))
        self.assertTrue(all(row['expected_doc_id'] == a['doc_id'] for row in result['cases']))

    def test_matching_source_never_used_as_neighbour(self):
        a, b = document(1), document(2)
        r = authority('doc_ids', b['doc_id'], b['doc_id'])
        result = builder.derive_cases_for_library('cwork-3m', [a, b], 'synthetic-seed', exclusion=r)
        self.assertTrue(all(row.get('neighbour_doc_id') != b['doc_id'] for row in result['cases']))
        self.assertTrue(exclusion.verify_exclusion([a, b], r, result)['verified'])


class VerifierBehavior(unittest.TestCase):
    def generated(self, docs, r):
        result = builder.derive_cases_for_library('cwork-3m', docs, 'synthetic-seed')
        _, ledger = exclusion.filter_sources(docs, r)
        result['excluded_sources'] = ledger
        return result

    def test_verifier_rejects_each_intersection(self):
        doc = document()
        for field, value in [('doc_ids', doc['doc_id']), ('queries', doc['title'].casefold()), ('tokens', 'syn-1')]:
            with self.subTest(field=field):
                r = authority(field, value)
                generated = self.generated([doc], r)
                checked = verifier.verify_library('cwork-3m', [doc], generated, 'synthetic-seed', exclusion=r)
                self.assertFalse(checked['exclusion_verified'])

    def test_verifier_rejects_unaccounted_member(self):
        doc = document()
        r = authority('doc_ids', doc['doc_id'])
        generated = {'cases': [], 'excluded_sources': []}
        checked = verifier.verify_library('cwork-3m', [doc], generated, 'synthetic-seed', exclusion=r)
        self.assertFalse(checked['exclusion_verified'])
        self.assertFalse(exclusion.verify_exclusion([doc], r, generated)['verified'])

    def test_complete_ledger_zero_intersection_passes(self):
        a, b = document(1), document(2)
        r = authority('doc_ids', b['doc_id'], b['doc_id'])
        generated = builder.derive_cases_for_library('cwork-3m', [a, b], 'synthetic-seed', exclusion=r)
        checked = verifier.verify_library('cwork-3m', [a, b], generated, 'synthetic-seed', exclusion=r)
        self.assertTrue(checked['exclusion_verified'])
        proof = exclusion.verify_exclusion([a, b], r, generated)
        self.assertTrue(proof['verified'])
        self.assertEqual(proof['members_unaccounted'], 0)
        self.assertEqual(proof['overlap_counts'], {'doc_ids': 0, 'queries': 0, 'tokens': 0})

    def test_absent_member_is_accounted_not_silently_dropped(self):
        doc = document(2)
        r = authority('doc_ids', 'synthetic-1')
        generated = builder.derive_cases_for_library('cwork-3m', [doc], 'synthetic-seed', exclusion=r)
        proof = exclusion.verify_exclusion([doc], r, generated)
        self.assertTrue(proof['verified'])
        self.assertEqual(proof['members_absent'], 1)

    def test_member_manifest_truncation_rejected(self):
        r = authority('doc_ids', 'synthetic-1')
        r['members'] = []
        with self.assertRaises(ValueError):
            exclusion.verify_exclusion([document()], r, {'cases': []})

    def test_wrong_ledger_or_duplicate_ledger_rejected(self):
        doc = document()
        r = authority('doc_ids', doc['doc_id'])
        _, ledger = exclusion.filter_sources([doc], r)
        bad = copy.deepcopy(ledger)
        bad[0]['matches']['doc_ids'] = []
        for rows in (bad, ledger + ledger):
            self.assertFalse(exclusion.verify_exclusion([doc], r, {'cases': [], 'excluded_sources': rows})['verified'])

    def test_unselected_query_match_still_rejects_emitted_source(self):
        doc = document()
        r = authority('queries', 'syn-1')
        _, ledger = exclusion.filter_sources([doc], r)
        generated = {'cases': [{'expected_doc_id': doc['doc_id'], 'query': doc['title']}], 'excluded_sources': ledger}
        proof = exclusion.verify_exclusion([doc], r, generated)
        self.assertEqual(proof['overlap_counts']['queries'], 0)
        self.assertFalse(proof['verified'])

    def test_nfkc_whitespace_dash_case_normalization(self):
        self.assertEqual(exclusion.normalize(' ＡＢＣ—１  \n Test '), 'abc-1 test')
        doc = document()
        doc['body'] = 'ＳＹＮ—１'
        self.assertIn('syn-1', exclusion.source_tokens(doc))

    def test_current_snapshot_reconstruction_is_deterministic(self):
        corpus = {'libraries': {'cwork-3m': [document(i) for i in range(1, 20)]}}
        r = exclusion.reconstruct(corpus)
        self.assertEqual(r, exclusion.reconstruct(corpus))
        self.assertEqual(r['sampling_version'], 'ops-known-item-stratified-v2')
        self.assertEqual(r['split_version'], 'ops-category-ordinal-calibration-holdout-v1')
        self.assertTrue(all(r[field] for field in exclusion.FIELDS))
        exclusion.validate_r(r)


if __name__ == '__main__':
    unittest.main()
