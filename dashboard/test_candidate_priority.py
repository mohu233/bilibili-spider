"""Offline regressions for backlog priority and bounded score updates."""
import tempfile
import unittest
from pathlib import Path

from .store import CANDIDATE_WHERE, DEFAULT_CONFIG, Store, later, now


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix='bili-candidate-priority-')
        self.root=Path(self.directory.name)
        self.store=Store(self.root)

    def tearDown(self):
        self.directory.cleanup()

    def config(self,**values):
        return self.store.set_config({**DEFAULT_CONFIG,**values})


class CandidatePriorityTests(StoreFixture):
    def test_default_scope_and_evidence_mode_are_validated_and_persisted(self):
        config=self.store.get_config()
        self.assertEqual((config['collection_scope'],config['evidence_mode']),('candidates_first','fast'))
        self.config(collection_scope='seed_cycle',evidence_mode='full')
        config=Store(self.root).get_config()
        self.assertEqual((config['collection_scope'],config['evidence_mode']),('seed_cycle','full'))
        for key in ('collection_scope','evidence_mode'):
            for value in ('invalid',None,False,[],{}):
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                    self.config(**{key:value})

    def test_legacy_database_receives_candidate_priority_default(self):
        self.store.add_seeds('1',20,'yes')
        self.store.add_seeds('2')
        with self.store.db() as c:
            c.execute("DELETE FROM settings WHERE key IN ('collection_scope','evidence_mode')")
        reopened=Store(self.root)
        self.assertEqual(reopened.next_seed(set()),'2')
        self.assertEqual(reopened.get_config()['evidence_mode'],'fast')

    def test_global_pending_fetched_and_failed_precede_highest_seed(self):
        self.store.add_seeds('1',20,'yes')
        self.store.add_seeds('2 3 4')
        self.store.update_profile('3',collection_status='fetched')
        self.store.update_profile('4',collection_status='failed')
        self.assertFalse(self.store.expansion_allowed())
        selected=[]
        for _ in range(3):
            user=self.store.next_seed(set(selected),candidates_only=bool(selected))
            selected.append(user)
        self.assertEqual(selected,['2','3','4'])
        self.assertIsNone(self.store.next_seed(set(selected)))
        self.assertNotIn('selected_seed_uid',self.store.get_config())
        for user in selected:self.store.classification(user,'no',{})
        self.assertTrue(self.store.expansion_allowed())
        self.assertEqual(self.store.next_seed(set()),'1')

    def test_backlog_pauses_and_resumes_existing_seed_cycle(self):
        self.config(collection_scope='seed_cycle')
        self.store.add_seeds('1',20,'yes')
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.add_seeds('2')
        self.store.relation_page('1','followers',['3'],1,False)
        self.config()
        self.assertEqual(self.store.next_seed(set()),'2')
        queue=self.store.state()['queue']
        self.assertEqual((queue['mode'],queue['selected_seed_uid'],queue['cycle_phase']),
                         ('candidate_backlog','1','expand_seed'))
        self.assertEqual(queue['collection_scope'],'candidates_first')
        for user in ('2','3'):
            self.assertEqual(self.store.next_seed(set()),user)
            self.store.classification(user,'no',{})
        self.assertEqual(self.store.next_seed(set()),'1')
        self.assertEqual(self.store.profile('1')['followers_page'],2)

    def test_delayed_retry_blocks_expansion_until_due_and_review_does_not(self):
        self.store.add_seeds('1',20,'yes')
        self.store.add_seeds('2 3')
        retry=later(3600)
        self.store.update_profile('2',collection_status='failed',available_at=retry)
        self.store.update_profile('3',collection_status='needs_review',available_at=later(7200))
        self.assertIsNone(self.store.next_seed(set()))
        self.assertFalse(self.store.expansion_allowed())
        queue=self.store.state()['queue']
        self.assertEqual((queue['pending_candidates'],queue['runnable_candidates'],queue['mode']),
                         (1,0,'waiting'))
        self.assertEqual(queue['candidate_wait_until'],retry)
        self.store.update_profile('2',available_at=later(-1))
        self.assertEqual(self.store.next_seed(set()),'2')
        self.store.classification('2','no',{})
        self.assertEqual(self.store.next_seed(set()),'1')
        self.assertEqual(self.store.profile('3')['collection_status'],'needs_review')

    def test_due_retry_precedes_new_candidates_and_completed_records_are_skipped(self):
        self.store.add_seeds('1',20,'yes')
        self.store.add_seeds('2')
        self.store.update_profile('2',collection_status='failed',available_at=later(-1))
        self.store.add_seeds('3 4 5')
        self.store.update_profile('4',traversal='complete')
        self.store.update_profile('5',traversal='legacy_done')
        self.assertEqual(self.store.next_seed(set()),'2')
        self.assertEqual(self.store.next_seed({'2'},candidates_only=True),'3')
        self.assertEqual(self.store.pending_candidates(),2)

    def test_in_flight_verdict_commit_cannot_start_or_resume_seed(self):
        self.store.add_seeds('1',20,'yes')
        self.store.add_seeds('2')
        self.assertEqual(self.store.next_seed(set()),'2')
        self.store.classification('2','yes',{})
        self.assertIsNone(self.store.next_seed({'2'},candidates_only=True))
        self.assertNotIn('selected_seed_uid',self.store.get_config())
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.add_seeds('3')
        self.store.classification('3','no',{})
        self.assertIsNone(self.store.next_seed({'3'},candidates_only=True))
        self.assertEqual(self.store.get_config()['selected_seed_uid'],'1')

    def test_unknown_finalization_leaves_automatic_queue(self):
        self.store.add_seeds('1',20,'yes')
        self.store.add_seeds('2')
        self.store.classification('2','unknown',{'intro':{'success':True}})
        self.store.finalize_classification('2')
        self.assertEqual(self.store.pending_candidates(),0)
        self.assertEqual(self.store.next_seed(set()),'1')

    def test_seed_cycle_option_preserves_seed_first_behavior(self):
        self.config(collection_scope='seed_cycle')
        self.store.add_seeds('1',20,'yes')
        self.store.add_seeds('2')
        self.assertEqual(self.store.next_seed(set()),'1')
        self.assertIsNone(self.store.next_seed({'1'}))
        self.assertTrue(self.store.expansion_allowed())

    def test_cooldown_preserves_longer_retry_after_and_extends_when_needed(self):
        self.store.cooldown(1200)
        longer=self.store.get_config()['cooldown_until']
        self.store.cooldown(300)
        self.assertEqual(self.store.get_config()['cooldown_until'],longer)
        self.store.cooldown(2400)
        self.assertGreater(self.store.get_config()['cooldown_until'],longer)
        with self.store.db() as c:
            c.execute("UPDATE settings SET value=? WHERE key='cooldown_until'",('"2000-01-01T00:00:00+00:00"',))
        self.store.cooldown(300)
        self.assertGreater(self.store.get_config()['cooldown_until'],now())

    def test_large_fifo_queue_uses_partial_index_without_score_sort(self):
        stamp=now()
        with self.store.db() as c:
            c.executemany('INSERT INTO profiles(uid,created_at,updated_at) VALUES(?,?,?)',
                          ((str(user),stamp,stamp) for user in range(100000,140000)))
            c.execute("UPDATE profiles SET identity_score=100,score=100 WHERE uid='139999'")
            plan=[row['detail'] for row in c.execute(
                'EXPLAIN QUERY PLAN SELECT uid FROM profiles INDEXED BY profiles_candidate_fifo WHERE '+
                CANDIDATE_WHERE+' AND available_at<=? ORDER BY created_at ASC,uid ASC LIMIT 1',(stamp,))]
        self.assertTrue(any('profiles_candidate_fifo' in detail for detail in plan),plan)
        self.assertFalse(any('TEMP B-TREE' in detail for detail in plan),plan)
        self.assertEqual(self.store.next_seed(set()),'100000')
        self.assertEqual(self.store.next_seed({'100000'},candidates_only=True),'100001')
        self.assertEqual(self.store.pending_candidates(),40000)


class IncrementalScoringTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.store.add_seeds('1 9',20,'yes')
        self.store.relation_page('1','followers',['2','3'],1,True)
        self.store.relation_page('1','followings',['2'],1,True)
        self.store.relation_page('9','followers',['3','4'],1,True)
        with self.store.db() as c:
            c.executescript('''CREATE TABLE score_writes(uid TEXT);
                CREATE TRIGGER audit_score_writes AFTER UPDATE OF score,furry_found ON profiles
                BEGIN INSERT INTO score_writes VALUES(NEW.uid); END;''')

    def writes(self):
        with self.store.db() as c:
            result=[row['uid'] for row in c.execute('SELECT uid FROM score_writes')]
            c.execute('DELETE FROM score_writes')
        return result

    def test_verdict_changes_update_only_direct_parents(self):
        self.store.classification('2','yes',{})
        self.assertEqual(self.writes(),['1'])
        self.store.classification('3','yes',{})
        self.assertEqual(set(self.writes()),{'1','9'})
        self.store.classification('3','yes',{'intro':{'strong_keywords':['furry']}})
        self.assertEqual(self.writes(),[])
        self.store.classification('4','unknown',{})
        self.assertEqual(self.writes(),[])
        self.store.decision('2','no')
        self.assertEqual(self.writes(),['1'])
        self.store.decision('2','no')
        self.assertEqual(self.writes(),[])
        self.store.set_base('3',7)
        self.assertEqual(self.writes(),['3'])

    def test_incremental_scores_match_full_recalculation(self):
        self.store.classification('2','yes',{})
        self.store.classification('3','no',{})
        self.store.decision('4','yes')
        self.store.set_base('1',13)
        self.store.relation_page('9','followings',['2','4'],1,True)
        self.store.decision('2','no')
        columns='uid,inherited_score,discovery_score,quality_score,score,furry_found,evaluated_found'
        with self.store.db() as c:
            before=[tuple(row) for row in c.execute('SELECT '+columns+' FROM profiles ORDER BY uid')]
            Store._recalculate(c)
            after=[tuple(row) for row in c.execute('SELECT '+columns+' FROM profiles ORDER BY uid')]
        self.assertEqual(before,after)
        self.assertEqual(self.store.profile('1')['furry_found'],0)
        self.assertEqual(self.store.profile('9')['furry_found'],1)


if __name__=='__main__':
    unittest.main()
