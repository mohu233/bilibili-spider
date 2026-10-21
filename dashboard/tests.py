"""Offline tests for scoring, new-data isolation, pagination, and the HTTP boundary."""
import base64
import json
import math
import shutil
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import patch

from .collector import BiliClient,CollectionError,Collector
from .store import DEFAULT_CONFIG,Store,later,now,score_parts
from .web import DashboardServer

RUNS=Path(__file__).parents[1]/'.test-runs'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root=RUNS/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.store=Store(self.root)
        self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full'})

    def tearDown(self):
        resolved=self.root.resolve()
        if resolved.is_relative_to(RUNS.resolve()) and resolved!=RUNS.resolve():
            shutil.rmtree(resolved)


class ScoringTests(Fixture):
    def test_bounded_and_monotonic_with_diminishing_returns(self):
        scores=[score_parts(20,100,k,k)['score'] for k in (0,1,10,100,10000)]
        self.assertEqual(scores,sorted(scores))
        self.assertLessEqual(max(scores),100)
        self.assertEqual(score_parts(10,10000,0,0)['inherited_score'],10)
        self.assertGreater(score_parts(10,50,0,0)['score'],score_parts(10,10,0,0)['score'])
        a=score_parts(10,0,1,1)['score']-score_parts(10,0,0,0)['score']
        b=score_parts(10,0,101,101)['score']-score_parts(10,0,100,100)['score']
        self.assertGreater(a,b)

    def test_unique_relations_and_reclassification_revoke_credit(self):
        self.store.add_seeds('1',10,'yes')
        self.store.relation_page('1','followers',['2','2','1'],1,False)
        self.store.relation_page('1','followings',['2'],1,True)
        self.store.decision('2','yes')
        row=self.store.profile('1')
        self.assertEqual(row['furry_found'],1)
        self.assertEqual(row['evaluated_found'],1)
        self.assertGreater(row['score'],10)
        self.store.decision('2','no')
        row=self.store.profile('1')
        self.assertEqual(row['furry_found'],0)
        self.assertEqual(row['score'],10)

    def test_inheritance_is_a_snapshot_and_cycles_do_not_inflate(self):
        self.store.add_seeds('1',20,'yes')
        self.store.relation_page('1','followers',['2'],1,False)
        first=self.store.profile('2')['inherited_score']
        self.store.set_base('1',0)
        self.store.relation_page('1','followings',['2'],1,True)
        self.assertEqual(self.store.profile('2')['inherited_score'],first)
        self.store.decision('2','yes')
        self.store.relation_page('2','followers',['1'],1,True)
        before=[self.store.profile(u)['score'] for u in ('1','2')]
        for _ in range(5):self.store.decision('2','yes')
        self.assertEqual(before,[self.store.profile(u)['score'] for u in ('1','2')])

    def test_manual_decision_survives_model_rerun(self):
        self.store.add_seeds('1',10,'yes')
        self.store.classification('1','no',{'furry_score':.01})
        self.assertEqual(self.store.profile('1')['verdict'],'yes')
        self.store.decision('1','clear')
        self.assertEqual(self.store.profile('1')['verdict'],'no')

    def test_completed_yes_stays_in_yes_but_leaves_seed_pool(self):
        self.store.add_seeds('1',10,'yes')
        self.store.relation_page('1','followers',[],1,True)
        self.assertFalse(self.store.finish_traversal('1'))
        self.assertEqual(self.store.list_profiles('seeds')['total'],1)
        self.store.relation_page('1','followings',[],1,True)
        self.assertTrue(self.store.finish_traversal('1'))
        self.assertEqual(self.store.list_profiles('seeds')['total'],0)
        self.assertEqual(self.store.list_profiles('yes')['total'],1)
        self.assertEqual(self.store.profile('1')['is_seed'],0)

    def test_bulk_invalid_uid_does_not_partially_write(self):
        with self.assertRaises(ValueError):self.store.add_seeds('123\nnot-a-uid')
        self.assertEqual(self.store.state()['counts']['total'],0)

    def test_duplicate_input_and_review_queue_feedback(self):
        result=self.store.add_seeds('123 123 456')
        self.assertEqual(result,{'added':2,'duplicates':1})
        self.store.update_profile('123',collection_status='needs_review')
        self.store.decision('123','yes')
        self.assertEqual(self.store.state()['counts']['review'],0)

    def test_old_data_is_reference_only(self):
        path=self.root/'data'/'matched_done.json'
        original=json.dumps({'uids':[{'uid':123,'name':'Old','hit':'furry-yolo','seed_weight':999,
                                      'discovered_uids':[456]}]}).encode()
        path.write_bytes(original)
        result=self.store.import_legacy()
        self.assertEqual(result['added'],1)
        self.assertEqual(self.store.state()['counts']['total'],0)
        self.assertEqual(self.store.state()['reference_count'],1)
        self.assertEqual(path.read_bytes(),original)
        self.store.add_seeds('123')
        fresh=self.store.profile('123')
        self.assertEqual((fresh['verdict'],fresh['score'],fresh['traversal'],fresh['name']),('unknown',10,'pending',''))
        self.assertEqual(len(fresh['references']),1)

    def test_benchmark_import_extracts_only_uids(self):
        folder=self.root/'data'/'avatar_benchmark_test';folder.mkdir()
        (folder/'manifest.json').write_text(json.dumps({'records':[{'uid':123,'name':'Old',
            'hit':'furry-yolo','image_path':'avatars/123.jpg','seed_weight':999}]}))
        self.store.import_legacy(True)
        row=self.store.profile('123')
        self.assertEqual(row['verdict'],'unknown')
        self.assertEqual(row['image_path'],'')
        self.assertEqual(row['score'],10)
        self.assertEqual(row['traversal'],'pending')


class CandidateStoreTests(Fixture):
    def test_manual_review_survives_late_model_result_and_finalization(self):
        self.store.add_seeds('1 2')
        for user,manual,automatic in (('1','yes','no'),('2','no','yes')):
            with self.subTest(manual=manual):
                self.store.update_profile(user,collection_status='needs_review')
                self.store.decision(user,manual,'Quick review fixture')
                self.store.classification(user,automatic,{'avatar':{'success':True,'furry_score':.5}})
                self.store.finalize_classification(user)
                row=self.store.profile(user)
                self.assertEqual((row['verdict'],row['manual'],row['automatic']),(manual,manual,automatic))
                self.assertEqual(row['collection_status'],'reviewed')
                self.assertEqual(row['is_seed'],1 if manual=='yes' else 0)
        self.assertEqual(self.store.list_profiles('candidates')['total'],0)
        self.assertEqual(self.store.list_profiles('seeds')['total'],1)
        self.assertEqual(self.store.state()['counts']['review'],0)

    def test_unchecked_uids_are_candidates_and_only_confirmed_yes_are_seeds(self):
        self.store.add_seeds('1 2')
        self.store.add_seeds('3',10,'yes')
        counts=self.store.state()['counts']
        self.assertEqual((counts['candidates'],counts['seeds']),(2,1))
        self.assertEqual(self.store.list_profiles('seeds')['total'],1)
        self.assertEqual(self.store.list_profiles('candidates')['total'],2)
        self.assertEqual(self.store.profile('1')['is_seed'],0)
        self.assertEqual(self.store.profile('3')['is_seed'],1)
        self.assertIsNone(self.store.next_seed({'3'}),
                          'a selected confirmed seed must complete before unrelated candidates')

    def test_automatic_classification_promotes_yes_and_excludes_no(self):
        self.store.add_seeds('1 2')
        self.store.classification('1','yes',{'decision_sources':['intro']})
        self.store.classification('2','no',{'decision_sources':[]})
        self.assertEqual((self.store.profile('1')['is_seed'],self.store.profile('2')['is_seed']),(1,0))
        counts=self.store.state()['counts']
        self.assertEqual((counts['candidates'],counts['seeds']),(0,1))
        self.assertIsNone(self.store.next_seed({'1'}))

    def test_manual_no_revokes_seed_and_manual_decision_overrides_later_models(self):
        self.store.add_seeds('1',10,'yes')
        self.store.classification('1','no',{})
        self.assertEqual((self.store.profile('1')['verdict'],self.store.profile('1')['is_seed']),('yes',1))
        self.store.decision('1','no')
        self.store.classification('1','yes',{})
        row=self.store.profile('1')
        self.assertEqual((row['verdict'],row['is_seed']),('no',0))
        self.assertEqual(self.store.state()['counts']['seeds'],0)
        self.store.decision('1','clear')
        row=self.store.profile('1')
        self.assertEqual((row['verdict'],row['is_seed']),('yes',1))

    def test_relation_discoveries_enter_candidates_without_becoming_seeds(self):
        self.store.add_seeds('1',10,'yes')
        self.store.relation_page('1','followers',['2','3','2'],1,False)
        self.store.relation_page('1','followings',['2'],1,True)
        self.assertEqual(self.store.profile('2')['is_seed'],0)
        self.assertEqual(self.store.profile('3')['verdict'],'unknown')
        counts=self.store.state()['counts']
        self.assertEqual((counts['candidates'],counts['seeds']),(2,1))
        self.assertEqual(self.store.list_profiles('candidates')['total'],2)
        self.assertEqual(self.store.list_profiles('seeds')['total'],1)
        self.assertIsNone(self.store.next_seed({'1'}),
                          'new relations wait until both directions of the selected seed finish')

    def test_legacy_avatar_assessments_are_rechecked_without_losing_manual_decisions_or_edges(self):
        self.store.add_seeds('1',10,'yes')
        self.store.relation_page('1','followers',['2','3','4','5'],1,True)
        self.store.decision('5','no')
        with self.store.db() as connection:
            for user,verdict,status,score in (('2','yes','classified',.99),('3','no','classified',.01),
                                               ('4','unknown','needs_review',.5)):
                connection.execute("""UPDATE profiles SET automatic=?,verdict=?,collection_status=?,
                  evidence=?,is_seed=1,traversal='complete',available_at=?,name='Retained profile'
                  WHERE uid=?""",(verdict,verdict,status,json.dumps({'furry_score':score}),later(3600),user))
            connection.execute("DELETE FROM settings WHERE key='candidate_pool_version'")
        self.store=Store(self.root)
        for user,old in (('2','yes'),('3','no'),('4','unknown')):
            with self.subTest(user=user):
                row=self.store.profile(user)
                self.assertEqual((row['verdict'],row['automatic'],row['is_seed'],row['traversal']),
                                 ('unknown','unknown',0,'pending'))
                self.assertEqual(row['available_at'],'')
                self.assertEqual(row['name'],'Retained profile')
                self.assertIn('furry_score',row['evidence'])
                self.assertEqual(row['evidence']['previous_assessment']['verdict'],old)
        self.assertEqual((self.store.profile('1')['verdict'],self.store.profile('1')['is_seed']),('yes',1))
        self.assertEqual((self.store.profile('5')['verdict'],self.store.profile('5')['is_seed']),('no',0))
        self.assertEqual(self.store.profile('1')['relations_count'],4)
        self.assertEqual(self.store.profile('1')['furry_found'],0)
        self.assertEqual(self.store.get_config()['candidate_pool_version'],1)

    def test_new_policy_assessments_survive_reopening_and_migration(self):
        self.store.add_seeds('1')
        self.store.classification('1','yes',{'decision_sources':['intro']})
        self.assertEqual(self.store.profile('1')['evidence']['decision_policy'],'any_strong_v1')
        with self.store.db() as connection:
            connection.execute("DELETE FROM settings WHERE key='candidate_pool_version'")
        self.store=Store(self.root)
        self.assertEqual((self.store.profile('1')['verdict'],self.store.profile('1')['is_seed']),('yes',1))


class SeedPriorityTests(Fixture):
    def evidence(self,avatar=.5,intro=False,title=False,tags=False):
        return {'decision_sources':['avatar'],
                'avatar':{'success':True,'furry_score':avatar},
                'intro':{'success':True,'strong_keywords':['furry'] if intro else []},
                'videos':{'success':True,'strong_title_keywords':['兽设'] if title else [],
                          'strong_tag_keywords':['furry'] if tags else []}}

    def seed(self,user,avatar=.5,intro=False,title=False,tags=False,base=10):
        self.store.add_seeds(user,base,'yes')
        self.store.classification(user,'yes',self.evidence(avatar,intro,title,tags))

    def finish(self,user):
        self.store.relation_page(user,'followings',[],1,True)
        self.store.relation_page(user,'followers',[],1,True)
        self.assertTrue(self.store.finish_traversal(user))

    def test_large_global_candidate_backlog_never_precedes_highest_scoring_seed(self):
        self.seed('1',1,True,True,True,base=20)
        self.seed('2',1,True,True,True,base=20)
        self.store.add_seeds(' '.join(str(n) for n in range(3,103)),0)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.assertIsNone(self.store.next_seed({'1'}))
        self.finish('1')
        self.assertEqual(self.store.next_seed(set()),'2')

    def test_unavailable_and_review_candidates_do_not_block_executable_seed(self):
        self.seed('1')
        self.store.add_seeds('2 3')
        self.store.update_profile('2',available_at=later(3600),collection_status='failed')
        self.store.update_profile('3',collection_status='needs_review')
        self.assertEqual(self.store.next_seed(set()),'1')
        self.assertEqual(self.store.profile('2')['collection_status'],'failed')
        self.assertEqual(self.store.profile('3')['collection_status'],'needs_review')
        self.assertTrue(self.store.expansion_allowed())
        queue=self.store.state()['queue']
        self.assertEqual((queue['pending_candidates'],queue['runnable_candidates']),(1,0))
        self.assertEqual((queue['selected_seed_uid'],queue['mode']),('1','expand_seed'))

    def test_queue_exposes_unchecked_and_runnable_candidates_separately(self):
        self.seed('1')
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.add_seeds('2 3 4')
        self.store.update_profile('3',available_at=later(3600))
        self.store.update_profile('4',collection_status='needs_review')
        self.assertTrue(self.store.expansion_allowed())
        queue=self.store.state()['queue']
        self.assertEqual((queue['pending_candidates'],queue['runnable_candidates']),(2,1))
        self.assertEqual((queue['selected_seed_uid'],queue['mode']),('1','expand_seed'))
        self.assertEqual(queue['cycle_total_candidates'],0)

    def test_identity_score_selects_seed_instead_of_inherited_discovery_score(self):
        self.seed('1',.5,base=20)
        self.seed('2',.95,True,True,True,base=0)
        self.assertGreater(self.store.profile('1')['score'],self.store.profile('2')['score'])
        self.assertEqual(self.store.next_seed(set()),'2')
        row=self.store.profile('2')
        self.assertAlmostEqual(row['identity_score'],98)
        self.assertAlmostEqual(row['evidence']['identity_score'],98)
        self.assertAlmostEqual(sum(row['evidence']['identity_components'].values()),98)

    def test_identity_components_use_the_four_independent_evidence_sources(self):
        cases=[('1',.95,False,False,False,38),('2',0,True,False,False,20),
               ('3',0,False,True,False,20),('4',0,False,False,True,20),
               ('5',1,True,True,True,100)]
        for user,avatar,intro,title,tags,expected in cases:
            with self.subTest(user=user):
                self.seed(user,avatar,intro,title,tags)
                self.assertAlmostEqual(self.store.profile(user)['identity_score'],expected)

    def test_invalid_or_failed_avatar_never_contributes_or_contaminates_identity_score(self):
        cases=[(float('nan'),True),(float('inf'),True),(-float('inf'),True),
               ('0.95',True),(True,True),(False,True),(-.1,True),(1.1,True),(.95,False)]
        for index,(score,success) in enumerate(cases,1):
            with self.subTest(score=repr(score),success=success):
                user=str(index)
                self.store.add_seeds(user,verdict='yes')
                evidence=self.evidence(score,intro=True)
                evidence['avatar']['success']=success
                self.store.classification(user,'yes',evidence)
                row=self.store.profile(user)
                self.assertTrue(math.isfinite(row['identity_score']))
                self.assertEqual(row['identity_score'],20)
                self.assertEqual(row['evidence']['identity_components']['avatar'],0)

    def test_identity_score_is_recovered_from_existing_evidence_after_reopen(self):
        self.seed('1',.95,True,True)
        with self.store.db() as connection:
            evidence=self.evidence(.95,True,True)
            evidence['decision_policy']='any_strong_v1'
            connection.execute('UPDATE profiles SET identity_score=0,evidence=? WHERE uid=?',
                               (json.dumps(evidence),'1'))
        self.store=Store(self.root)
        row=self.store.profile('1')
        self.assertAlmostEqual(row['identity_score'],78)
        self.assertAlmostEqual(row['evidence']['identity_score'],78)

    def test_selected_seed_survives_unrelated_candidate_and_higher_new_seed(self):
        self.seed('1',.9,True)
        self.seed('2',.1)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.seed('3',1,True,True,True)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.add_seeds('4')
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.classification('4','no',{})
        self.assertEqual(self.store.next_seed(set()),'1')
        self.finish('1')
        self.assertEqual(self.store.next_seed(set()),'3')

    def test_selected_seed_does_not_switch_to_another_seed_between_batches(self):
        self.seed('1',.9)
        self.seed('2',.1)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.assertIsNone(self.store.next_seed({'1'}))
        self.assertEqual(self.store.next_seed(set()),'1')

    def test_manual_rejection_releases_selection_but_temporary_unavailability_keeps_it(self):
        self.seed('1',1)
        self.seed('2',.5)
        self.seed('3',.1)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.decision('1','no')
        self.assertEqual(self.store.next_seed(set()),'2')
        self.store.update_profile('2',available_at=later(3600))
        self.assertIsNone(self.store.next_seed(set()))
        self.assertEqual(self.store.get_config()['selected_seed_uid'],'2')
        self.store.update_profile('2',available_at='')
        self.assertEqual(self.store.next_seed(set()),'2')

    def test_completed_yes_retains_data_and_only_explicit_requeue_restores_seed(self):
        self.seed('1',.95,True)
        self.store.update_profile('1',name='Retained profile',intro='Furry artist')
        self.store.relation_page('1','followings',['2'],1,True)
        self.store.relation_page('1','followers',[],1,True)
        self.assertTrue(self.store.finish_traversal('1'))
        for mutate in (lambda:self.store.classification('1','yes',self.evidence(.95,True)),
                       lambda:self.store.decision('1','yes')):
            mutate()
            row=self.store.profile('1')
            self.assertEqual((row['verdict'],row['is_seed'],row['traversal']),('yes',0,'complete'))
            self.assertEqual((row['name'],row['intro'],row['relations_count']),('Retained profile','Furry artist',1))
            self.assertEqual(self.store.list_profiles('yes')['total'],1)
            self.assertEqual(self.store.list_profiles('seeds')['total'],0)
        self.store.requeue('1')
        row=self.store.profile('1')
        self.assertEqual((row['verdict'],row['is_seed'],row['traversal']),('yes',1,'pending'))
        self.assertEqual((row['followers_page'],row['followings_page'],row['relations_count']),(1,1,1))


class SeedCycleStoreTests(Fixture):
    def seed(self,user,score):
        self.store.add_seeds(user,verdict='yes')
        self.store.classification(user,'yes',{'avatar':{'success':True,'furry_score':score}})

    def relations(self,user,followers=(),followings=()):
        self.store.relation_page(user,'followers',list(followers),1,True)
        self.store.relation_page(user,'followings',list(followings),1,True)
        self.assertTrue(self.store.finish_traversal(user))

    def test_relation_phase_ignores_new_candidates_until_both_directions_are_complete(self):
        self.seed('1',.9);self.seed('9',.8)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.relation_page('1','followers',['2'],1,True)
        self.assertFalse(self.store.finish_traversal('1'))
        self.assertEqual(self.store.next_seed(set()),'1')
        self.assertIsNone(self.store.next_seed(set(),candidates_only=True))
        self.assertEqual(self.store.profile('2')['collection_status'],'pending')
        self.store.relation_page('1','followings',['3'],1,True)
        self.assertTrue(self.store.finish_traversal('1'))
        self.assertIn(self.store.next_seed(set()),('2','3'))
        self.assertEqual(self.store.get_config()['selected_seed_phase'],'classify_candidates')

    def test_only_current_root_children_are_scheduled_and_dual_direction_uid_is_unique(self):
        self.seed('1',.9);self.seed('9',.8)
        self.store.add_seeds('20 21')
        self.assertEqual(self.store.next_seed(set()),'1')
        self.relations('1',followers=['2','3','2'],followings=['2'])
        first=self.store.next_seed(set())
        second=self.store.next_seed({first},candidates_only=True)
        self.assertEqual({first,second},{'2','3'})
        self.assertIsNone(self.store.next_seed({'2','3'},candidates_only=True))
        self.store.classification('2','no',{})
        self.store.update_profile('3',collection_status='needs_review')
        self.assertEqual(self.store.next_seed(set()),'9')
        self.assertTrue(all(self.store.profile(user)['collection_status']=='pending' for user in ('20','21')))
        self.assertEqual(self.store.profile('1')['relations_count'],2)
        with self.store.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM edges WHERE parent_uid=?',('1',)).fetchone()[0],3,
                             'both directed edges are retained while candidate processing is deduplicated')

    def test_promoted_highest_scoring_child_waits_until_its_siblings_finish(self):
        self.seed('1',.9);self.seed('9',.8)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.relations('1',followers=['2','3'])
        self.store.classification('2','yes',{'avatar':{'success':True,'furry_score':1},
            'intro':{'strong_keywords':['furry']},'videos':{'strong_title_keywords':['兽设'],'strong_tag_keywords':['furry']}})
        self.assertEqual(self.store.next_seed(set()),'3')
        self.store.classification('3','no',{})
        self.assertEqual(self.store.next_seed(set()),'2')

    def test_failed_deferred_child_keeps_current_cycle_instead_of_starting_next_seed(self):
        self.seed('1',.9);self.seed('9',.8)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.relations('1',followers=['2'])
        deferred=later(3600)
        self.store.update_profile('2',available_at=deferred,collection_status='failed')
        self.assertIsNone(self.store.next_seed(set()))
        queue=self.store.state()['queue']
        self.assertEqual((queue['selected_seed_uid'],queue['cycle_phase'],queue['mode']),
                         ('1','classify_candidates','waiting'))
        self.assertEqual((queue['cycle_pending_candidates'],queue['cycle_runnable_candidates']),(1,0))
        self.store.update_profile('2',available_at='')
        self.assertEqual(self.store.next_seed(set()),'2')

    def test_completed_root_and_child_phase_survive_store_reopening(self):
        self.seed('1',.9);self.seed('9',.8)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.relations('1',followers=['2'])
        self.assertEqual(self.store.profile('1')['is_seed'],0)
        self.store=Store(self.root)
        self.assertEqual(self.store.get_config()['selected_seed_uid'],'1')
        self.assertEqual(self.store.next_seed(set()),'2')
        self.store.classification('2','no',{})
        self.assertEqual(self.store.next_seed(set()),'9')

    def test_partial_cursor_survives_store_reopening_without_switching_to_higher_new_seed(self):
        self.seed('1',.9)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.relation_page('1','followers',['2'],1,False)
        self.store.finish_traversal('1')
        self.seed('9',1)
        self.store=Store(self.root)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.assertEqual(self.store.profile('1')['followers_page'],2)
        self.assertEqual(self.store.profile('2')['collection_status'],'pending')

    def test_inflight_committed_verdict_cannot_release_cycle_or_select_next_root(self):
        self.seed('1',.9);self.seed('9',.8)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.relations('1',followers=['2'])
        self.assertEqual(self.store.next_seed(set(),candidates_only=True),'2')
        self.store.classification('2','no',{})
        self.assertIsNone(self.store.next_seed({'2'},candidates_only=True))
        self.assertEqual(self.store.get_config()['selected_seed_uid'],'1')
        self.assertEqual(self.store.next_seed(set()),'9')

    def test_manual_rejection_of_active_root_cancels_its_remaining_relation_work(self):
        self.seed('1',.9);self.seed('9',.8)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.store.relation_page('1','followers',['2'],1,False)
        self.store.decision('1','no')
        self.assertFalse(self.store.relation_page('1','followers',['3'],2,True))
        self.assertEqual(self.store.next_seed(set()),'9')
        self.assertEqual(self.store.profile('1')['followers_page'],2)
        with self.assertRaises(ValueError):self.store.profile('3')

    def test_review_children_complete_automatic_cycle_without_waiting_for_human(self):
        self.seed('1',.9);self.seed('9',.8)
        self.assertEqual(self.store.next_seed(set()),'1')
        self.relations('1',followers=['2'])
        self.store.update_profile('2',collection_status='needs_review')
        queue=self.store.state()['queue']
        self.assertEqual((queue['cycle_total_candidates'],queue['cycle_pending_candidates'],queue['cycle_review_candidates']),
                         (1,0,1))
        self.assertEqual(self.store.next_seed(set()),'9')
        self.assertEqual(self.store.profile('2')['collection_status'],'needs_review')


class RecentVideoClientTests(unittest.TestCase):
    def test_risk_response_stops_waiting_request_before_it_uses_another_account(self):
        for risk in (-352,-101,429):
            with self.subTest(risk=risk):
                client=BiliClient();stop=threading.Event()
                opened=threading.Event();second_attempt=threading.Event();release=threading.Event()
                requests=[];outcomes=[]
                class Response:
                    def __enter__(self):return self
                    def __exit__(self,*args):return False
                    def read(self,limit):
                        if not release.wait(3):raise RuntimeError('test release was not delivered')
                        return json.dumps({'code':risk}).encode()
                class Opener:
                    def open(self,request,timeout):
                        requests.append(request)
                        opened.set()
                        if risk==429:
                            if not release.wait(3):raise RuntimeError('test release was not delivered')
                            raise urllib.error.HTTPError(request.full_url,429,'Fixture limit',{},None)
                        return Response()
                client.opener=Opener()
                def call(account,interval,attempt=None):
                    if attempt:attempt.set()
                    try:
                        client._request('https://api.bilibili.com/fixture',account,stop,interval)
                        outcomes.append('unexpected success')
                    except CollectionError as error:outcomes.append(error.code)
                    except InterruptedError:outcomes.append('cancelled')
                first=threading.Thread(target=call,args=('SESSDATA=FIRST_FIXTURE',0))
                second=threading.Thread(target=call,args=('SESSDATA=SECOND_FIXTURE',60,second_attempt))
                try:
                    first.start();self.assertTrue(opened.wait(3))
                    second.start();self.assertTrue(second_attempt.wait(3))
                    release.set();first.join(3);second.join(3)
                    self.assertFalse(first.is_alive());self.assertFalse(second.is_alive())
                    self.assertEqual(len(requests),1)
                    self.assertEqual(set(outcomes),{'auth' if risk==-101 else str(risk),'cancelled'})
                    self.assertTrue(stop.is_set())
                finally:
                    stop.set();release.set();first.join(3)
                    if second.ident is not None:second.join(3)

    def client_with(self,videos,tags=None):
        client=BiliClient()
        calls=[]
        nav={'wbi_img':{'img_url':'https://i0.hdslb.com/bfs/wbi/'+'a'*32+'.png',
                        'sub_url':'https://i0.hdslb.com/bfs/wbi/'+'b'*32+'.png'}}
        def request(url,cookie='',stop=None,interval=1.5,**options):
            calls.append((url,cookie,stop,interval))
            path=urllib.parse.urlsplit(url).path
            if path=='/x/web-interface/nav':return nav
            if path=='/x/space/wbi/arc/search':return {'list':{'vlist':videos}}
            if path=='/x/tag/archive/tags':
                if isinstance(tags,Exception):raise tags
                return tags if tags is not None else [{'tag_name':'furry'},{'tag_name':'日常'},{'tag_name':'furry'}]
            self.fail('unexpected request path: '+path)
        client._request=request
        return client,calls

    def test_latest_three_video_sample_uses_same_cookie_cancellation_and_request_spacing(self):
        videos=[{'bvid':'BV'+str(n).zfill(10),'title':'Fixture '+str(n)} for n in range(5)]
        client,calls=self.client_with(videos)
        stop=threading.Event()
        result=client.recent_video_tags('123','SESSDATA=TEST_VIDEO_SAMPLE',stop,1.75)
        self.assertTrue(result['success'])
        self.assertEqual(result['sample_size'],3)
        self.assertEqual([video['bvid'] for video in result['videos']],[video['bvid'] for video in videos[:3]])
        self.assertTrue(all(video['tags']==['furry','日常'] and video['tags_success'] for video in result['videos']))
        self.assertEqual(len(calls),5)
        self.assertTrue(all(cookie=='SESSDATA=TEST_VIDEO_SAMPLE' and event is stop and interval==1.75
                            for _,cookie,event,interval in calls))
        query=urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1][0]).query)
        self.assertEqual((query['ps'],query['pn'],query['order']),(['3'],['1'],['pubdate']))
        self.assertEqual(len(query['w_rid'][0]),32)

    def test_successful_empty_video_list_does_not_request_tags(self):
        client,calls=self.client_with([])
        result=client.recent_video_tags('123','SESSDATA=TEST_VIDEO_SAMPLE',threading.Event(),1.5)
        self.assertEqual(result,{'videos':[],'sample_size':0,'success':True})
        self.assertEqual(len(calls),2)

    def test_malformed_video_or_tags_are_errors_instead_of_successful_empty_samples(self):
        for videos,tags,code in ((None,None,'video_unavailable'),
                                 ([{'bvid':'invalid','title':'Fixture'}],None,'video_format'),
                                 ([{'bvid':'BV0000000001','title':'Fixture'}],{},'video_tags_unavailable')):
            with self.subTest(code=code):
                client,_=self.client_with(videos,tags)
                with self.assertRaises(CollectionError) as caught:
                    client.recent_video_tags('123','SESSDATA=TEST_VIDEO_SAMPLE',threading.Event(),1.5)
                self.assertEqual(caught.exception.code,code)

    def test_risk_during_tag_request_propagates_without_retry_and_preserves_partial_sample(self):
        client,calls=self.client_with([{'bvid':'BV0000000001','title':'兽装日常'},
                                      {'bvid':'BV0000000002','title':'Unreached'}],
                                     CollectionError('Fixture platform restriction','-352',True))
        with self.assertRaises(CollectionError) as caught:
            client.recent_video_tags('123','SESSDATA=TEST_VIDEO_SAMPLE',threading.Event(),1.5)
        self.assertTrue(caught.exception.stop_all)
        self.assertEqual(len(calls),3)
        self.assertEqual(caught.exception.video_items,[{'bvid':'BV0000000001','title':'兽装日常',
                                                       'tags':[],'tags_success':False}])


class FakeClassifier:
    def availability(self):return {name:{'weights':True,'runtime':True} for name in ('mobilenetv3_v0.1_dist','caformer_s36_v0.1','yolo')}
    def predict(self,*args):return {'furry_score':.95,'model':'mobilenetv3_v0.1_dist'}


class CookieTests(Fixture):
    def test_delete_expired_account_removes_metadata_credentials_and_fingerprint(self):
        ident=self.store.account_add('Expired fixture','SESSDATA=DELETE_EXPIRED_FIXTURE')
        keep=self.store.account_add('Retained fixture','SESSDATA=DELETE_RETAINED_FIXTURE')
        self.store.account_result(ident,'expired','Expired identity')
        with self.store.db() as connection:
            fingerprint=connection.execute('SELECT fingerprint FROM accounts WHERE id=?',(ident,)).fetchone()[0]
        self.store.account_delete(ident)
        self.assertEqual([item['id'] for item in self.store.accounts()],[keep])
        with self.assertRaisesRegex(ValueError,'账号不存在'):self.store.account_secret(ident)
        with self.store.db() as connection:
            self.assertIsNone(connection.execute('SELECT secret,fingerprint FROM accounts WHERE id=? OR fingerprint=?',
                                                (ident,fingerprint)).fetchone())
        self.assertNotIn('DELETE_EXPIRED_FIXTURE',json.dumps(self.store.state()))
        self.assertNotIn('Expired identity',json.dumps(self.store.state()))
        self.assertEqual(self.store.next_account(),keep)

    def test_delete_preserves_profiles_edges_jobs_references_and_original_cookie_file(self):
        path=self.root/'cookies.json'
        original=b'["SESSDATA=DELETE_SOURCE_FILE_FIXTURE"]'
        path.write_bytes(original)
        self.store.add_seeds('1',10,'yes')
        self.store.relation_page('1','followers',['2'],1,True)
        self.store.decision('2','no','Retain manual review')
        self.store.job_create(DEFAULT_CONFIG)
        self.store.import_cookies()
        ident=self.store.accounts()[0]['id']
        def captured():
            with self.store.db() as connection:
                return {table:[tuple(row) for row in connection.execute('SELECT * FROM '+table)]
                        for table in ('profiles','edges','jobs','reference_profiles','settings')}
        before=captured()
        self.store.account_delete(ident)
        self.assertEqual(captured(),before)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(self.store.accounts(),[])

    def test_deleted_cookie_can_be_readded_but_repeat_delete_is_missing(self):
        ident=self.store.account_add('SESSDATA=LABEL_IS_NOT_LOGGED','SESSDATA=DELETE_READD_FIXTURE')
        self.store.account_delete(ident)
        events_before=self.store.state()['events']
        with self.assertRaisesRegex(ValueError,'账号不存在'):self.store.account_delete(ident)
        self.assertEqual(self.store.state()['events'],events_before)
        self.assertNotIn('LABEL_IS_NOT_LOGGED',json.dumps(events_before))
        replacement=self.store.account_add('Replacement','SESSDATA=DELETE_READD_FIXTURE')
        self.assertNotEqual(replacement,ident)
        self.assertEqual(self.store.next_account(),replacement)

    def test_reserved_accounts_are_not_reassigned_and_disabled_or_expired_are_ineligible(self):
        first=self.store.account_add('First','SESSDATA=TEST_RESERVATION_FIRST')
        second=self.store.account_add('Second','SESSDATA=TEST_RESERVATION_SECOND')
        disabled=self.store.account_add('Disabled','SESSDATA=TEST_RESERVATION_DISABLED')
        expired=self.store.account_add('Expired','SESSDATA=TEST_RESERVATION_EXPIRED')
        self.store.account_enabled(disabled,False)
        self.store.account_result(expired,'expired')
        selected=self.store.next_account()
        self.assertIn(selected,(first,second))
        other=self.store.next_account(exclude={selected})
        self.assertEqual({selected,other},{first,second})
        self.assertIsNone(self.store.next_account(exclude={first,second}))
        counts={item['id']:item['request_count'] for item in self.store.accounts()}
        self.assertEqual(counts,{first:1,second:1,disabled:0,expired:0})

    def test_header_and_surrounding_whitespace_preserve_values(self):
        ident=self.store.account_add('Fixture','\r\n Cookie: buvid3=fixture; SESSDATA=TEST%2CSESSION==; empty= \r\n')
        self.assertEqual(self.store.account_secret(ident),'buvid3=fixture; SESSDATA=TEST%2CSESSION==; empty=')

    def test_extract_cookie_only_from_full_request_headers(self):
        ident=self.store.account_add('Fixture','GET / HTTP/1.1\r\nHost: example.invalid\r\ncookie: SESSDATA=TEST_SESSION; x=1\r\nAccept: */*')
        self.assertEqual(self.store.account_secret(ident),'SESSDATA=TEST_SESSION; x=1')

    def test_equivalent_copies_are_duplicates(self):
        self.store.account_add('Fixture','SESSDATA=TEST_SESSION; x=1')
        with self.assertRaisesRegex(ValueError,'已在池中'):
            self.store.account_add('Copy',' Cookie: SESSDATA=TEST_SESSION;x=1;\r\n')

    def test_bad_copies_have_specific_errors_and_do_not_save(self):
        cases=[('SESSDATA=TEST\u2026SECRET','省略号'),('SESSDATA=TEST...SECRET','省略号'),
               ('TEST_SECRET','SESSDATA='),('buvid3=TEST_SECRET','SESSDATA='),
               ('SESSDATA=','SESSDATA='),('SESSDATA=""','SESSDATA='),
               ('SESSDATA=TEST\x00SECRET','特殊字符'),('SESSDATA=TEST\x7fSECRET','特殊字符'),
               ('SESSDATA=测试SECRET','特殊字符'),
               ('Cookie: SESSDATA=TEST_SECRET\nCookie: SESSDATA=OTHER_SECRET','多个'),
               ('SESSDATA=TEST_SECRET;SESSDATA=OTHER_SECRET','重复'),
               ('Cookie: SESSDATA=TEST_SECRET\n remaining','换行拆开'),
               ('SESSDATA=TEST_SECRET\nx=1','多行')]
        for secret,message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError,message) as raised:
                    self.store.account_add('Fixture',secret)
                self.assertNotIn('SECRET',str(raised.exception))
        self.assertEqual(self.store.accounts(),[])


class CookieImportTests(Fixture):
    def write_cookies(self,value):
        path=self.root/'cookies.json'
        path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8-sig')
        return path

    def test_legacy_string_array_remains_supported(self):
        self.write_cookies(['SESSDATA=TEST_ONE','SESSDATA=TEST_TWO'])
        self.assertEqual(self.store.import_cookies(),{'added':2,'skipped':0})

    def test_account_field_maps_preserve_values_and_source_file(self):
        path=self.write_cookies({'first':{'SESSDATA':'TEST%2CSESSION==','bili_jct':'TEST+CSRF','empty':''},
                                 'second':{'SESSDATA':'TEST_OTHER'}})
        original=path.read_bytes()
        self.assertEqual(self.store.import_cookies(),{'added':2,'skipped':0})
        secrets={self.store.account_secret(a['id']) for a in self.store.accounts()}
        self.assertIn('SESSDATA=TEST%2CSESSION==; bili_jct=TEST+CSRF; empty=',secrets)
        self.assertEqual(path.read_bytes(),original)
        self.assertNotIn('TEST%2CSESSION',json.dumps(self.store.state()))
        self.assertTrue(all(a['status']=='unverified' for a in self.store.accounts()))

    def test_single_cookie_field_map(self):
        self.write_cookies({'SESSDATA':'TEST_SESSION','buvid3':'TEST_DEVICE'})
        self.assertEqual(self.store.import_cookies(),{'added':1,'skipped':0})

    def test_grouped_header_strings_and_duplicate_import(self):
        self.write_cookies({'first':'Cookie: SESSDATA=TEST_ONE','second':'SESSDATA=TEST_TWO'})
        self.assertEqual(self.store.import_cookies(),{'added':2,'skipped':0})
        self.assertEqual(self.store.import_cookies(),{'added':0,'skipped':2})

    def test_invalid_accounts_are_skipped_without_inserting_truncated_values(self):
        self.write_cookies({'good':{'SESSDATA':'TEST_GOOD'},'truncated':{'SESSDATA':'TEST\u2026SECRET'},
                           'missing':{'buvid3':'TEST_DEVICE'},'nontext':{'SESSDATA':['TEST_BAD']},
                           'separator':{'SESSDATA':'TEST; injected=VALUE'},
                           'badname':{'SESSDATA':'TEST','bad;name':'VALUE'}})
        self.assertEqual(self.store.import_cookies(),{'added':1,'skipped':5})
        self.assertEqual(len(self.store.accounts()),1)

    def test_invalid_json_does_not_expose_file_content(self):
        (self.root/'cookies.json').write_text('{"SESSDATA":"TEST_SECRET"',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'JSON') as error:self.store.import_cookies()
        self.assertNotIn('TEST_SECRET',str(error.exception))

    def test_unsupported_root_has_an_actionable_error(self):
        self.write_cookies(42)
        with self.assertRaisesRegex(ValueError,'按账号分组'):self.store.import_cookies()


class FakeClient:
    def __init__(self):self.pages=[]
    def profile(self,user,*args):return {'name':'Test','intro':'','avatar_url':'https://i0.hdslb.com/test.jpg','fan_count':0,'follow_count':0}
    def download(self,url,path,*args):path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
    def recent_video_tags(self,user,*args):return {'videos':[],'sample_size':0,'success':True}
    def relation(self,user,direction,page,*args):
        self.pages.append((user,direction,page))
        return (['2'],False) if page==1 else ([],True)
    def validate(self,secret):return 'Fixture account'


class CookieDeleteCollectorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.workers=[];self.releases=[];self.threads=[]

    def tearDown(self):
        try:
            for event in self.releases:event.set()
            for worker in self.workers:worker.stop()
            for thread in self.threads:thread.join(4)
            for worker in self.workers:
                if worker.thread:worker.thread.join(4)
                self.assertFalse(worker.active())
            self.assertTrue(all(not thread.is_alive() for thread in self.threads))
        finally:super().tearDown()

    def release_event(self):
        event=threading.Event();self.releases.append(event)
        return event

    def prepare(self,users='1 2',accounts=2,worker_count=0):
        self.store.add_seeds(users)
        identifiers=[self.store.account_add('Delete fixture '+str(index),'SESSDATA=DELETE_WORKER_FIXTURE_'+str(index))
                     for index in range(accounts)]
        self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','worker_count':worker_count,'max_users':len(users.split())})
        worker=Collector(self.store,FakeClient(),FakeClassifier());self.workers.append(worker)
        return worker,identifiers

    def start_thread(self,operation):
        thread=threading.Thread(target=operation,daemon=True);self.threads.append(thread);thread.start()
        return thread

    def finish(self,user):
        self.store.classification(user,'no',{'decision_sources':[]})
        self.store.finalize_classification(user)

    def test_validation_reserves_only_its_account_and_releases_before_idle_delete(self):
        worker,accounts=self.prepare()
        entered=threading.Event();release=self.release_event();results=[]
        class WaitingValidation(FakeClient):
            def validate(self,secret):
                entered.set()
                if not release.wait(3):raise RuntimeError('validation release missing')
                return 'Validated fixture'
        worker.client=WaitingValidation()
        validation=self.start_thread(lambda:results.append(worker.validate_account(accounts[0])))
        self.assertTrue(entered.wait(2))
        self.assertEqual(worker.busy_account_ids(),{accounts[0]})
        with self.assertRaisesRegex(ValueError,'正在采集或验证'):worker.delete_account(accounts[0])
        with self.assertRaisesRegex(ValueError,'正在采集或验证'):worker.validate_account(accounts[0])
        worker.delete_account(accounts[1])
        self.assertEqual([account['id'] for account in self.store.accounts()],[accounts[0]])
        release.set();validation.join(3)
        self.assertFalse(validation.is_alive())
        self.assertEqual(results,[{'valid':True,'message':'账号验证成功'}])
        self.assertEqual(worker.busy_account_ids(),set())
        worker.delete_account(accounts[0]);self.assertEqual(self.store.accounts(),[])

    def test_validation_failure_always_releases_the_account(self):
        worker,accounts=self.prepare()
        class FailedValidation(FakeClient):
            def validate(self,secret):raise CollectionError('Fixture expired','auth',True)
        worker.client=FailedValidation()
        self.assertFalse(worker.validate_account(accounts[0])['valid'])
        self.assertEqual(worker.busy_account_ids(),set())
        self.assertEqual(next(account['status'] for account in self.store.accounts() if account['id']==accounts[0]),'expired')
        worker.delete_account(accounts[0])
        class CrashedValidation(FakeClient):
            def validate(self,secret):raise RuntimeError('Fixture validation crash')
        worker.client=CrashedValidation()
        with self.assertRaisesRegex(RuntimeError,'Fixture validation crash'):worker.validate_account(accounts[1])
        self.assertEqual(worker.busy_account_ids(),set())
        worker.delete_account(accounts[1])

    def test_disable_wait_then_delete_while_other_collection_task_keeps_running(self):
        worker,accounts=self.prepare('1 2 3')
        releases={account:self.release_event() for account in accounts}
        entered=threading.Event();target_finished=threading.Event();lock=threading.Lock();calls=[]
        def process(user,account,config,job):
            with lock:
                calls.append((user,account))
                if len(calls)>=2:entered.set()
            if not releases[account].wait(3):raise RuntimeError('collection release missing')
            self.finish(user)
        worker._process=process
        original_release=worker._release_account
        def observed_release(account):
            original_release(account)
            if account==accounts[0]:target_finished.set()
        worker._release_account=observed_release
        worker.start();self.assertTrue(entered.wait(3))
        with self.assertRaisesRegex(ValueError,'正在采集或验证'):worker.delete_account(accounts[0])
        self.store.account_enabled(accounts[0],False)
        releases[accounts[0]].set();self.assertTrue(target_finished.wait(3))
        self.assertTrue(worker.active(),'the other account must still be collecting')
        worker.delete_account(accounts[0])
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['available_accounts'],snapshot['requested'],snapshot['effective']),(1,1,1))
        self.assertNotIn(accounts[0],{account['id'] for account in self.store.accounts()})
        releases[accounts[1]].set();worker.thread.join(4)
        self.assertFalse(worker.active())
        self.assertEqual(self.store.state()['job']['processed'],3)
        self.assertEqual(sum(account==accounts[0] for _,account in calls),1)
        self.assertEqual(worker.busy_account_ids(),set())

    def test_reservation_protects_gap_before_worker_is_visible(self):
        worker,accounts=self.prepare('1',accounts=1)
        scheduled=threading.Event();release=self.release_event();original=worker._execute_task
        def delayed_execute(*args):
            scheduled.set()
            if not release.wait(3):raise RuntimeError('registration release missing')
            return original(*args)
        worker._execute_task=delayed_execute
        worker._process=lambda user,*args:self.finish(user)
        worker.start();self.assertTrue(scheduled.wait(3))
        self.assertEqual(worker.workers_snapshot()['active'],0)
        self.assertEqual(worker.busy_account_ids(),set(accounts))
        with self.assertRaisesRegex(ValueError,'正在采集或验证'):worker.delete_account(accounts[0])
        release.set();worker.thread.join(4)
        self.assertFalse(worker.active());self.assertEqual(worker.busy_account_ids(),set())
        worker.delete_account(accounts[0])

    def test_selection_and_reservation_are_atomic_against_deletion(self):
        worker,accounts=self.prepare('1',accounts=1)
        selected=threading.Event();release=self.release_event();deleting=threading.Event();deleted=threading.Event()
        original=self.store.next_account;claimed=[];errors=[]
        def delayed_selection(*args,**kwargs):
            result=original(*args,**kwargs);selected.set()
            if not release.wait(3):raise RuntimeError('selection release missing')
            return result
        self.store.next_account=delayed_selection
        claim=self.start_thread(lambda:claimed.append(worker._claim_account()))
        self.assertTrue(selected.wait(2))
        def attempt_delete():
            deleting.set()
            try:worker.delete_account(accounts[0])
            except ValueError as error:errors.append(str(error))
            finally:deleted.set()
        deletion=self.start_thread(attempt_delete)
        self.assertTrue(deleting.wait(2))
        self.assertFalse(deleted.wait(.1),'delete entered between selection and account reservation')
        release.set();claim.join(3);deletion.join(3)
        self.assertEqual(claimed,accounts)
        self.assertEqual(len(errors),1);self.assertIn('正在采集或验证',errors[0])
        worker._release_account(accounts[0]);worker.delete_account(accounts[0])

    def test_executor_submit_failure_releases_reservation(self):
        worker,accounts=self.prepare('1',accounts=1)
        with patch('dashboard.collector.ThreadPoolExecutor.submit',side_effect=RuntimeError('Fixture submit failure')):
            worker.start();worker.thread.join(4)
        self.assertFalse(worker.active())
        self.assertEqual(self.store.state()['job']['state'],'failed')
        self.assertEqual(worker.busy_account_ids(),set())
        worker.delete_account(accounts[0])

    def test_validation_occupied_account_waits_then_collects_without_false_auth_failure(self):
        worker,accounts=self.prepare('1',accounts=1)
        entered=threading.Event();release=self.release_event();processed=threading.Event()
        class WaitingValidation(FakeClient):
            def validate(self,secret):
                entered.set()
                if not release.wait(3):raise RuntimeError('validation release missing')
                return 'Fixture account'
        worker.client=WaitingValidation()
        validation=self.start_thread(lambda:worker.validate_account(accounts[0]))
        self.assertTrue(entered.wait(2))
        def process(user,*args):self.finish(user);processed.set()
        worker._process=process;worker.start()
        self.assertFalse(processed.wait(.15))
        self.assertTrue(worker.active())
        self.assertNotEqual(self.store.state()['job']['state'],'blocked')
        release.set();validation.join(3);worker.thread.join(4)
        self.assertFalse(worker.active());self.assertTrue(processed.is_set())
        self.assertEqual(self.store.state()['job']['state'],'completed')


class CollectorTests(Fixture):
    def prepare(self,client=None):
        self.store.add_seeds('1',10,'yes')
        account=self.store.account_add('Fixture','SESSDATA=TEST_SECRET_NEVER_RETURN')
        client=client or FakeClient()
        worker=Collector(self.store,client,FakeClassifier())
        options={**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','max_users':1,'pages_per_run':1}
        job=self.store.job_create(options)
        return worker,account,client,options,job

    def test_page_cap_is_partial_then_resume_completes(self):
        class NoDiscoveries(FakeClient):
            def relation(self,user,direction,page,*args):
                self.pages.append((user,direction,page))
                return [],page>1
        worker,account,client,options,job=self.prepare(NoDiscoveries())
        worker._process('1',account,options,job)
        row=self.store.profile('1')
        self.assertEqual(row['traversal'],'partial')
        self.assertEqual((row['followers_page'],row['followings_page']),(2,2))
        worker._process('1',account,options,job)
        self.assertEqual(self.store.profile('1')['traversal'],'complete')
        self.assertIn(('1','followers',2),client.pages)
        self.assertIn(('1','followings',2),client.pages)

    def test_both_relation_directions_finish_before_discovered_candidate_is_classified(self):
        worker,account,client,options,job=self.prepare()
        options={**options,'pages_per_run':3}
        self.assertEqual(self.store.next_seed(set()),'1')
        worker._process('1',account,options,job)
        self.assertEqual(client.pages,[('1','followings',1),('1','followings',2),
                                       ('1','followers',1),('1','followers',2)])
        row=self.store.profile('1')
        self.assertEqual((row['followings_page'],row['followers_page'],row['traversal']),(3,3,'complete'))
        self.assertEqual((row['is_seed'],self.store.get_config()['selected_seed_uid']),(0,'1'))
        self.assertEqual(self.store.profile('2')['verdict'],'unknown')
        self.assertEqual(self.store.next_seed(set()),'2')
        worker._process('2',account,options,job)
        self.assertEqual(client.pages,[('1','followings',1),('1','followings',2),
                                       ('1','followers',1),('1','followers',2)])
        self.assertEqual(self.store.next_seed(set()),'2')
        row=self.store.profile('1')
        self.assertEqual((row['verdict'],row['is_seed'],row['traversal']),('yes',0,'complete'))
        self.assertEqual(self.store.profile('2')['is_seed'],1)

    def test_failed_list_is_not_a_completed_empty_list(self):
        class BadList(FakeClient):
            def relation(self,*args):raise CollectionError('Hidden relation list','unavailable')
        worker,account,client,options,job=self.prepare(BadList())
        worker._process('1',account,options,job)
        row=self.store.profile('1')
        self.assertEqual(row['traversal'],'partial')
        self.assertEqual(row['followers_state'],'failed')
        self.assertEqual(row['followers_page'],1)

    def test_pause_keeps_work_resumable(self):
        entered=threading.Event()
        class Waiting(FakeClient):
            def profile(self,user,cookie,stop,interval):
                entered.set();stop.wait(2);raise InterruptedError()
        worker,account,client,options,job=self.prepare(Waiting())
        worker.start()
        self.assertTrue(entered.wait(2))
        worker.stop();worker.thread.join(3)
        self.assertFalse(worker.active())
        self.assertEqual(self.store.state()['job']['state'],'paused')
        self.assertEqual(self.store.profile('1')['traversal'],'pending')

    def test_model_failure_is_not_a_negative_classification(self):
        class Missing(FakeClassifier):
            def predict(self,*args):raise CollectionError('Missing model','model_missing')
        worker,account,client,options,job=self.prepare()
        self.store.decision('1','clear')
        worker.classifier=Missing()
        worker._process('1',account,options,job)
        row=self.store.profile('1')
        self.assertEqual(row['verdict'],'unknown')
        self.assertEqual(row['collection_status'],'needs_review')
        self.assertEqual(client.pages,[])


class PrivateRelationTests(Fixture):
    def prepare(self,case,client):
        store=Store(self.root/case)
        store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full'})
        for user,score in (('1',1),('9',.98)):
            store.add_seeds(user,verdict='yes')
            store.classification(user,'yes',{'automatic_reason':'Fixture confirmed seed',
                'intro':{'success':True,'strong_keywords':[]},
                'videos':{'success':True,'strong_title_keywords':[],'strong_tag_keywords':[]},
                'avatar':{'success':True,'furry_score':score}})
        account=store.account_add('Fixture','SESSDATA=PRIVATE_RELATION_FIXTURE')
        worker=Collector(store,client,FakeClassifier())
        config={**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','pages_per_run':1}
        job=store.job_create(config)
        self.assertEqual(store.next_seed(set()),'1')
        return store,worker,account,config,job

    def test_each_privacy_code_and_direction_keeps_available_children_and_resumes_after_reopen(self):
        for code in ('22115','22118'):
            for private in ('followings','followers'):
                with self.subTest(code=code,direction=private):
                    class OnePrivate(FakeClient):
                        def relation(self,user,direction,page,*args):
                            self.pages.append((user,direction,page))
                            if direction==private:raise CollectionError('Fixture private relation list',code)
                            return ['2'],True
                    client=OnePrivate()
                    store,worker,account,config,job=self.prepare(code+'-'+private,client)
                    worker._process('1',account,config,job)
                    row=store.profile('1')
                    other='followers' if private=='followings' else 'followings'
                    self.assertEqual((row[private+'_state'],row[other+'_state']),('unavailable','complete'))
                    self.assertEqual((row['traversal'],row['is_seed'],row['available_at']),('complete',0,''))
                    self.assertEqual(store.state()['counts']['restricted'],1)
                    reopened=Store(store.root)
                    self.assertEqual(reopened.profile('1')[private+'_state'],'unavailable')
                    self.assertEqual(reopened.profile('1')['is_seed'],0)
                    self.assertEqual(reopened.get_config()['selected_seed_uid'],'1')
                    self.assertEqual(reopened.next_seed(set()),'2')
                    worker._process('2',account,config,job)
                    self.assertEqual(reopened.next_seed(set()),'9')
                    self.assertEqual(client.pages,[('1','followings',1),('1','followers',1)])

    def test_two_private_directions_finish_cycle_without_pretending_empty_lists_were_fetched(self):
        class BothPrivate(FakeClient):
            def relation(self,user,direction,page,*args):
                self.pages.append((user,direction,page))
                raise CollectionError('Fixture private relation list','22118')
        client=BothPrivate()
        store,worker,account,config,job=self.prepare('both',client)
        worker._process('1',account,config,job)
        row=store.profile('1')
        self.assertEqual((row['followings_state'],row['followers_state']),('unavailable','unavailable'))
        self.assertEqual((row['traversal'],row['is_seed'],row['relations_count']),('complete',0,0))
        self.assertEqual(store.next_seed(set()),'9')
        self.assertEqual(store.state()['counts']['restricted'],1)
        self.assertEqual(client.pages,[('1','followings',1),('1','followers',1)])

    def test_private_direction_is_not_retried_when_other_direction_needs_a_later_page(self):
        class PrivateAndPaged(FakeClient):
            def relation(self,user,direction,page,*args):
                self.pages.append((user,direction,page))
                if direction=='followings':raise CollectionError('Fixture private relation list','22115')
                return (['2'],False) if page==1 else ([],True)
        client=PrivateAndPaged()
        store,worker,account,config,job=self.prepare('paged',client)
        worker._process('1',account,config,job)
        self.assertEqual((store.profile('1')['followings_state'],store.profile('1')['traversal']),('unavailable','partial'))
        self.assertEqual(store.next_seed(set()),'1')
        worker._process('1',account,config,job)
        self.assertEqual(client.pages,[('1','followings',1),('1','followers',1),('1','followers',2)])
        self.assertEqual(store.profile('1')['is_seed'],0)
        self.assertEqual(store.next_seed(set()),'2')

    def test_privacy_after_a_saved_page_preserves_previously_discovered_users(self):
        class BecomesPrivate(FakeClient):
            def relation(self,user,direction,page,*args):
                self.pages.append((user,direction,page))
                if direction=='followers':return [],True
                if page==1:return ['2'],False
                raise CollectionError('Fixture private relation list','22118')
        client=BecomesPrivate()
        store,worker,account,config,job=self.prepare('saved',client)
        worker._process('1',account,config,job)
        worker._process('1',account,config,job)
        row=store.profile('1')
        self.assertEqual((row['followings_state'],row['followings_page'],row['relations_count']),('unavailable',2,1))
        self.assertEqual(store.next_seed(set()),'2')
        self.assertEqual(client.pages,[('1','followings',1),('1','followers',1),('1','followings',2)])

    def test_fatal_relation_error_is_never_downgraded_to_private_completion(self):
        for code in ('-352','auth','22118'):
            with self.subTest(code=code):
                class Fatal(FakeClient):
                    def relation(self,user,direction,page,*args):
                        self.pages.append((user,direction,page))
                        raise CollectionError('Fixture fatal relation failure',code,True)
                client=Fatal()
                store,worker,account,config,job=self.prepare('fatal-'+code,client)
                with self.assertRaises(CollectionError) as raised:worker._process('1',account,config,job)
                self.assertTrue(raised.exception.stop_all)
                row=store.profile('1')
                self.assertEqual((row['followings_state'],row['followers_state']),('failed','pending'))
                self.assertEqual((row['traversal'],row['is_seed']),('partial',1))
                self.assertEqual(store.state()['counts']['restricted'],0)
                self.assertEqual(client.pages,[('1','followings',1)])


class SignalClient(FakeClient):
    def __init__(self,intro='',tags=(),title='',video_error=None):
        super().__init__()
        self.intro=intro
        self.tags=list(tags)
        self.title=title
        self.video_error=video_error
        self.checked=[]

    def profile(self,user,*args):
        self.checked.append('profile')
        return {**super().profile(user,*args),'intro':self.intro}

    def recent_video_tags(self,user,*args):
        self.checked.append('videos')
        if self.video_error:raise self.video_error
        videos=[{'bvid':'BV_TEST_OFFLINE','title':self.title,'tags':self.tags}] if self.tags or self.title else []
        return {'videos':videos,'sample_size':len(videos),'success':True}

    def download(self,url,path,*args):
        self.checked.append('avatar')
        return super().download(url,path,*args)

    def relation(self,user,direction,page,*args):
        self.pages.append((user,direction,page))
        return [],True


class SignalClassifier(FakeClassifier):
    def __init__(self,score):self.score=score
    def predict(self,*args):return {'furry_score':self.score,'model':'mobilenetv3_v0.1_dist'}


class FurryEvidenceTests(Fixture):
    def prepare(self,client=None,score=.01,manual=None,classifier=None):
        user=str(getattr(self,'next_user',0)+1)
        self.next_user=int(user)
        self.store.add_seeds(user)
        if manual:self.store.decision(user,manual)
        if not hasattr(self,'account'):
            self.account=self.store.account_add('Fixture','SESSDATA=TEST_SIGNAL_ACCOUNT')
        client=client or SignalClient()
        worker=Collector(self.store,client,classifier or SignalClassifier(score))
        options={**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','max_users':1,'pages_per_run':1}
        job=self.store.job_create(options)
        return worker,self.account,client,options,job,user

    def process(self,**options):
        worker,account,client,config,job,user=self.prepare(**options)
        worker._process(user,account,config,job)
        return self.store.profile(user),client

    def test_strong_intro_alone_promotes_seed_despite_low_avatar_and_checks_all_sources(self):
        row,client=self.process(client=SignalClient(intro='分享我的兽装日常'),score=.01)
        self.assertEqual((row['verdict'],row['is_seed']),('yes',1))
        self.assertEqual(client.checked,['profile','videos','avatar'])
        evidence=row['evidence']
        self.assertIn('intro',evidence['decision_sources'])
        self.assertIn('兽装',evidence['intro']['strong_keywords'])
        self.assertEqual(evidence['avatar']['furry_score'],.01)
        self.assertEqual(evidence['rule'],'any_strong_signal')
        self.assertEqual(client.pages,[])

    def test_promoted_candidate_only_expands_when_scheduled_again_as_seed(self):
        worker,account,client,config,job,user=self.prepare(client=SignalClient(intro='Furry artist'))
        self.assertEqual(self.store.next_seed(set()),user)
        worker._process(user,account,config,job)
        self.assertEqual(client.checked,['profile','videos','avatar'])
        self.assertEqual(client.pages,[])
        row=self.store.profile(user)
        self.assertEqual((row['is_seed'],row['traversal'],row['followers_page'],row['followings_page']),
                         (1,'pending',1,1))
        self.assertEqual(self.store.next_seed(set()),user)
        worker._process(user,account,config,job)
        self.assertEqual(client.pages,[(user,'followings',1),(user,'followers',1)])
        self.assertEqual(client.checked,['profile','videos','avatar'],
                         'an already checked seed must resume relations without fetching its evidence again')
        row=self.store.profile(user)
        self.assertEqual((row['verdict'],row['is_seed'],row['traversal']),('yes',0,'complete'))

    def test_recent_video_tag_alone_promotes_seed_despite_low_avatar(self):
        row,_=self.process(client=SignalClient(tags=['FurSuit'],title='周末出门'),score=.01)
        self.assertEqual((row['verdict'],row['is_seed']),('yes',1))
        self.assertIn('video_tag',row['evidence']['decision_sources'])
        self.assertTrue(row['evidence']['videos']['success'])
        self.assertEqual(row['evidence']['videos']['sample_size'],1)
        self.assertTrue(row['evidence']['videos']['strong_tag_keywords'])

    def test_recent_video_title_alone_is_a_strong_signal(self):
        row,_=self.process(client=SignalClient(title='我的兽设展示'),score=.01)
        self.assertEqual(row['verdict'],'yes')
        self.assertIn('video_title',row['evidence']['decision_sources'])

    def test_high_avatar_alone_promotes_seed(self):
        row,_=self.process(score=.95)
        self.assertEqual((row['verdict'],row['is_seed']),('yes',1))
        self.assertEqual(row['evidence']['decision_sources'],['avatar'])

    def test_negated_intro_does_not_promote_or_become_a_false_negative(self):
        for intro in ('不是furry，不喜欢兽装','not a furry'):
            with self.subTest(intro=intro):
                row,client=self.process(client=SignalClient(intro=intro),score=.01)
                self.assertEqual((row['verdict'],row['is_seed']),('unknown',0))
                self.assertEqual(row['collection_status'],'needs_review')
                self.assertFalse(row['evidence']['intro']['strong_keywords'])
                self.assertTrue(row['evidence']['intro']['negated_keywords'])
                self.assertEqual(client.pages,[])

    def test_weak_text_is_reviewed_and_does_not_expand_relations(self):
        row,client=self.process(client=SignalClient(intro='喜欢毛茸茸的小动物'),score=.01)
        self.assertEqual((row['verdict'],row['is_seed']),('unknown',0))
        self.assertTrue(row['evidence']['intro']['weak_keywords'])
        self.assertEqual(client.pages,[])

    def test_low_avatar_is_no_only_when_other_sources_succeed_without_signals(self):
        row,client=self.process(score=.01)
        self.assertEqual((row['verdict'],row['is_seed']),('no',0))
        self.assertTrue(row['evidence']['intro']['success'])
        self.assertTrue(row['evidence']['videos']['success'])
        self.assertTrue(row['evidence']['avatar']['success'])
        self.assertEqual(client.pages,[])

    def test_uncertain_avatar_remains_candidate_for_review_without_expansion(self):
        row,client=self.process(score=.5)
        self.assertEqual((row['verdict'],row['is_seed'],row['collection_status']),('unknown',0,'needs_review'))
        self.assertEqual(self.store.state()['counts']['candidates'],1)
        self.assertIsNone(self.store.next_seed(set()))
        self.assertEqual(client.pages,[])

    def test_video_failure_preserves_error_but_does_not_cancel_high_avatar(self):
        row,_=self.process(client=SignalClient(video_error=CollectionError('Fixture video unavailable','network')),score=.95)
        self.assertEqual((row['verdict'],row['is_seed']),('yes',1))
        videos=row['evidence']['videos']
        self.assertFalse(videos['success'])
        self.assertIn('Fixture video unavailable',videos['error'])
        self.assertEqual(videos['error_code'],'network')
        self.assertIn('avatar',row['evidence']['decision_sources'])

    def test_video_failure_prevents_low_avatar_from_becoming_no(self):
        row,client=self.process(client=SignalClient(video_error=CollectionError('Fixture video unavailable','network')),score=.01)
        self.assertEqual((row['verdict'],row['is_seed'],row['collection_status']),('unknown',0,'needs_review'))
        self.assertEqual(client.pages,[])

    def test_strong_intro_remains_yes_when_avatar_model_fails(self):
        class Missing(SignalClassifier):
            def predict(self,*args):raise CollectionError('Fixture model unavailable','model_missing')
        row,_=self.process(client=SignalClient(intro='Furry artist'),classifier=Missing(.01))
        self.assertEqual((row['verdict'],row['is_seed']),('yes',1))
        self.assertFalse(row['evidence']['avatar']['success'])
        self.assertEqual(row['evidence']['avatar']['error_code'],'model_missing')
        self.assertIn('intro',row['evidence']['decision_sources'])

    def test_manual_no_overrides_all_positive_sources(self):
        row,client=self.process(client=SignalClient(intro='兽装',tags=['furry']),score=.95,manual='no')
        self.assertEqual((row['automatic'],row['verdict'],row['is_seed']),('yes','no',0))
        self.assertEqual(client.pages,[])

    def test_video_risk_blocks_job_and_sets_cooldown_without_relations(self):
        worker,account,client,config,job,user=self.prepare(
            client=SignalClient(video_error=CollectionError('Fixture video risk','-352',True)),score=.95)
        worker._run(job,{**config,'continuous':True})
        state=self.store.state()
        self.assertEqual(state['job']['state'],'blocked')
        self.assertEqual(state['job']['processed'],0)
        self.assertGreater(state['config']['cooldown_until'],now())
        self.assertEqual(client.pages,[])
        self.assertFalse(self.store.profile(user)['evidence']['videos']['success'])


class ContinuousConfigTests(Fixture):
    def test_worker_count_defaults_to_one_and_accepts_auto_or_one_to_twenty_integers(self):
        self.assertEqual(self.store.get_config()['worker_count'],1)
        for value in (True,False,-1,21,1.0,2.0,0.0,20.0,'0','1','2','20',None,[],{}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','worker_count':value})
                self.assertEqual(self.store.get_config()['worker_count'],1)
        for value in range(21):
            with self.subTest(allowed=value):
                self.assertEqual(self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','worker_count':value})['worker_count'],value)
        self.assertEqual(self.store.set_config(DEFAULT_CONFIG)['worker_count'],1)

    def test_continuous_defaults_off_and_requires_a_boolean(self):
        self.assertIs(self.store.get_config()['continuous'],False)
        for value in (0,1,'false','true',None,[],{}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError,'连续采集开关'):
                    self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','continuous':value})
                self.assertIs(self.store.get_config()['continuous'],False)
        self.assertIs(self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','continuous':True})['continuous'],True)
        self.assertIs(self.store.set_config(DEFAULT_CONFIG)['continuous'],False)

    def test_review_items_are_not_retried_until_manually_decided(self):
        self.store.add_seeds('1 2')
        self.store.update_profile('1',collection_status='needs_review',available_at='')
        self.assertEqual(self.store.next_seed(set()),'2')
        self.assertIsNone(self.store.next_seed({'2'}))
        self.store.decision('1','yes')
        self.assertEqual(self.store.next_seed({'2'}),'1')
        self.assertEqual(self.store.next_seed(set()),'1')


class ObservedStopEvent:
    """Expose entry into a real interruptible wait without polling or sleeping."""
    def __init__(self):
        self.event=threading.Event()
        self.waiting=threading.Event()
        self.timeouts=[]

    def set(self):self.event.set()
    def clear(self):self.event.clear()
    def is_set(self):return self.event.is_set()
    def wait(self,timeout=None):
        self.timeouts.append(timeout)
        self.waiting.set()
        return self.event.wait(timeout)


class ContinuousCollectorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.workers=[]

    def tearDown(self):
        try:
            for worker in self.workers:self.stop_worker(worker)
        finally:super().tearDown()

    def prepare(self,users='',continuous=True,max_users=1,idle_wait=60):
        if users:self.store.add_seeds(users,10,'yes')
        account=self.store.account_add('Fixture','SESSDATA=TEST_CONTINUOUS_ACCOUNT')
        self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','continuous':continuous,'max_users':max_users})
        worker=Collector(self.store,FakeClient(),FakeClassifier())
        worker.IDLE_WAIT_SECONDS=idle_wait
        worker.ROUND_WAIT_SECONDS=.01
        worker.stop_event=ObservedStopEvent()
        calls=[]
        def complete(user,selected,config,job):
            calls.append((user,selected,job))
            if self.store.profile(user)['verdict']=='unknown':
                self.store.classification(user,'no',{})
                self.store.finalize_classification(user)
            else:
                for direction in ('followers','followings'):
                    self.store.relation_page(user,direction,[],1,True)
                self.store.finish_traversal(user)
        worker._process=complete
        self.workers.append(worker)
        return worker,account,calls

    def stop_worker(self,worker):
        worker.stop()
        if worker.thread:worker.thread.join(2)
        self.assertFalse(worker.active(),'collector must stop before its fixture is removed')

    def watch_processed(self,count):
        reached=threading.Event()
        update=self.store.job_update
        def observe(ident,**fields):
            update(ident,**fields)
            if fields.get('processed',0)>=count:reached.set()
        self.store.job_update=observe
        return reached

    def test_continuous_crosses_batch_limit_and_accumulates_in_one_job(self):
        worker,account,calls=self.prepare('1 2 3',max_users=1)
        reached=self.watch_processed(3)
        ident=worker.start()
        self.assertTrue(reached.wait(2),'the second and third batches did not run')
        self.stop_worker(worker)
        job=self.store.state()['job']
        self.assertEqual((job['id'],job['state'],job['processed']),(ident,'paused',3))
        self.assertEqual({user for user,_,_ in calls},{'1','2','3'})
        self.assertEqual(len(calls),3)
        self.assertTrue(all(selected==account and job_id==ident for _,selected,job_id in calls))
        with self.store.db() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],1)

    def test_empty_queue_waits_without_busy_loop_and_pause_interrupts_wait(self):
        worker,_,calls=self.prepare()
        searches=[]
        next_seed=self.store.next_seed
        def observe(exclude,**options):
            searches.append(None)
            return next_seed(exclude,**options)
        self.store.next_seed=observe
        worker.start()
        self.assertTrue(worker.stop_event.waiting.wait(2),'empty queue did not enter a wait')
        job=self.store.state()['job']
        self.assertEqual((job['state'],job['phase'],job['current_uid']),('running','等待新任务',None))
        self.assertEqual(job['processed'],0)
        self.assertEqual(calls,[])
        self.assertGreater(worker.stop_event.timeouts[0],0)
        self.assertLessEqual(len(searches),3,'empty queue repeatedly queried instead of waiting')
        self.stop_worker(worker)
        self.assertEqual(self.store.state()['job']['state'],'paused')

    def test_partial_traversal_is_revisited_in_a_later_batch(self):
        worker,_,calls=self.prepare('1',max_users=2)
        reached=self.watch_processed(2)
        def partial_then_complete(user,selected,config,job):
            calls.append((user,selected,job))
            if len(calls)==1:self.store.update_profile(user,traversal='partial')
            else:
                for direction in ('followers','followings'):
                    self.store.relation_page(user,direction,[],1,True)
                self.store.finish_traversal(user)
        worker._process=partial_then_complete
        worker.start()
        self.assertTrue(reached.wait(2),'partially traversed seed was not resumed')
        self.stop_worker(worker)
        self.assertEqual([user for user,_,_ in calls],['1','1'])
        self.assertEqual(self.store.profile('1')['traversal'],'complete')

    def test_deferred_seed_is_left_untouched_while_waiting(self):
        worker,_,calls=self.prepare('1')
        deferred=later(3600)
        self.store.update_profile('1',available_at=deferred)
        worker.start()
        self.assertTrue(worker.stop_event.waiting.wait(2))
        self.assertEqual(self.store.state()['job']['phase'],'等待新任务')
        self.assertEqual(self.store.profile('1')['available_at'],deferred)
        self.assertEqual(calls,[])
        self.stop_worker(worker)

    def test_new_seed_added_during_idle_is_collected_automatically(self):
        worker,_,calls=self.prepare(idle_wait=.02)
        reached=self.watch_processed(1)
        ident=worker.start()
        self.assertTrue(worker.stop_event.waiting.wait(2))
        self.store.add_seeds('123')
        self.assertTrue(reached.wait(2),'idle collector did not pick up the new seed')
        self.stop_worker(worker)
        job=self.store.state()['job']
        self.assertEqual((job['id'],job['processed']),(ident,1))
        self.assertEqual([user for user,_,_ in calls],['123'])

    def test_single_round_stops_at_configured_limit(self):
        worker,_,calls=self.prepare('1 2 3',continuous=False,max_users=2)
        worker.start();worker.thread.join(2)
        self.assertFalse(worker.active())
        job=self.store.state()['job']
        self.assertEqual((job['state'],job['processed']),('completed',2))
        self.assertEqual(len(calls),2)
        self.assertEqual(self.store.list_profiles('seeds')['total'],1)
        self.assertEqual(worker.stop_event.timeouts,[])

    def test_risk_stops_all_accounts_and_preserves_cooldown(self):
        worker,first,calls=self.prepare('1 2')
        second=self.store.account_add('Spare','SESSDATA=TEST_SPARE_ACCOUNT')
        def restricted(user,selected,config,job):
            calls.append((user,selected,job))
            raise CollectionError('Simulated platform restriction','-352',True)
        worker._process=restricted
        worker.start();worker.thread.join(2)
        self.assertFalse(worker.active())
        state=self.store.state()
        self.assertEqual(state['job']['state'],'blocked')
        self.assertEqual([selected for _,selected,_ in calls],[first])
        self.assertEqual(next(a['request_count'] for a in state['accounts'] if a['id']==second),0)
        cooldown=state['config']['cooldown_until']
        self.assertGreater(cooldown,now())
        with self.assertRaisesRegex(ValueError,'冷却'):worker.start()
        self.assertEqual(self.store.get_config()['cooldown_until'],cooldown)
        self.assertEqual(self.store.state()['job']['id'],state['job']['id'])

    def test_expired_cookie_blocks_without_automatically_using_spare_account(self):
        worker,first,calls=self.prepare('1 2')
        second=self.store.account_add('Spare','SESSDATA=TEST_SPARE_ACCOUNT')
        def expired(user,selected,config,job):
            calls.append((user,selected,job))
            raise CollectionError('Simulated expired credential','auth',True)
        worker._process=expired
        worker.start();worker.thread.join(2)
        self.assertFalse(worker.active())
        state=self.store.state()
        self.assertEqual(state['job']['state'],'blocked')
        self.assertEqual([selected for _,selected,_ in calls],[first])
        statuses={account['id']:account for account in state['accounts']}
        self.assertEqual(statuses[first]['status'],'expired')
        self.assertEqual(statuses[second]['request_count'],0)
        self.assertEqual(worker.stop_event.timeouts,[])


class ParallelCollectorTests(Fixture):
    """Coordinate real worker threads with Events, without network or model calls."""
    def setUp(self):
        super().setUp()
        self.workers=[]
        self.releases=[]

    def tearDown(self):
        try:
            for event in self.releases:event.set()
            for worker in self.workers:
                worker.stop()
                if worker.thread:worker.thread.join(4)
                self.assertFalse(worker.active(),'all worker tasks must exit before fixture removal')
        finally:super().tearDown()

    def prepare(self,users='1 2',accounts=2,max_users=2,worker_count=2):
        self.store.add_seeds(users)
        identifiers=[self.store.account_add('Fixture '+str(index),'SESSDATA=PARALLEL_TEST_SECRET_'+str(index))
                     for index in range(accounts)]
        self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','worker_count':worker_count,'max_users':max_users})
        worker=Collector(self.store,FakeClient(),FakeClassifier())
        self.workers.append(worker)
        return worker,identifiers

    def release_event(self):
        event=threading.Event();self.releases.append(event)
        return event

    def finish_candidate(self,user):
        self.store.classification(user,'no',{'decision_sources':[]})
        self.store.finalize_classification(user)

    def join_completed(self,worker,processed):
        worker.thread.join(4)
        self.assertFalse(worker.active())
        job=self.store.state()['job']
        self.assertEqual((job['state'],job['processed']),('completed',processed))
        self.assertEqual(worker.workers_snapshot()['active'],0)

    def test_two_candidates_overlap_with_distinct_uids_accounts_and_safe_snapshot(self):
        worker,accounts=self.prepare()
        entered=threading.Event();release=self.release_event();lock=threading.Lock();calls=[]
        def process(user,account,config,job):
            with lock:
                calls.append((user,account,threading.current_thread()))
                if len(calls)==2:entered.set()
            if not release.wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
        worker._process=process
        worker.start()
        self.assertTrue(entered.wait(3),'two evidence tasks did not execute concurrently')
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['requested'],snapshot['effective'],snapshot['active']),(2,2,2))
        self.assertEqual({item['uid'] for item in snapshot['items']},{'1','2'})
        self.assertEqual({item['account_id'] for item in snapshot['items']},set(accounts))
        self.assertEqual({item['slot'] for item in snapshot['items']},{1,2})
        self.assertTrue(all(item['kind']=='candidate' for item in snapshot['items']))
        self.assertTrue(all(item['account_label'] and item['phase'] for item in snapshot['items']))
        self.assertNotIn('PARALLEL_TEST_SECRET',json.dumps(snapshot))
        self.assertEqual(len({thread.ident for _,_,thread in calls}),2)
        release.set();self.join_completed(worker,2)
        self.assertTrue(all(not thread.is_alive() for _,_,thread in calls))

    def test_auto_mode_runs_three_candidates_on_three_distinct_accounts_and_threads(self):
        worker,accounts=self.prepare(users='1 2 3',accounts=3,max_users=3,worker_count=0)
        entered=threading.Event();release=self.release_event();lock=threading.Lock();calls=[]
        def process(user,account,config,job):
            with lock:
                calls.append((user,account,threading.current_thread()))
                if len(calls)==3:entered.set()
            if not release.wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
        worker._process=process;worker.start()
        self.assertTrue(entered.wait(3),'three candidate evidence tasks did not overlap')
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['mode'],snapshot['configured'],snapshot['limit']),('auto',0,20))
        self.assertEqual((snapshot['available_accounts'],snapshot['requested'],snapshot['effective'],snapshot['active']),
                         (3,3,3,3))
        self.assertEqual({item['uid'] for item in snapshot['items']},{'1','2','3'})
        self.assertEqual({item['account_id'] for item in snapshot['items']},set(accounts))
        self.assertEqual(len({thread.ident for _,_,thread in calls}),3)
        self.assertNotIn('PARALLEL_TEST_SECRET',json.dumps(snapshot))
        release.set();self.join_completed(worker,3)
        self.assertTrue(all(not thread.is_alive() for _,_,thread in calls))

    def test_manual_three_can_overlap_three_evidence_tasks(self):
        worker,accounts=self.prepare(users='1 2 3',accounts=3,max_users=3,worker_count=3)
        entered=threading.Event();release=self.release_event();lock=threading.Lock();calls=[]
        def process(user,account,config,job):
            with lock:
                calls.append((user,account))
                if len(calls)==3:entered.set()
            if not release.wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
        worker._process=process;worker.start()
        self.assertTrue(entered.wait(3))
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['mode'],snapshot['configured'],snapshot['available_accounts'],snapshot['active']),
                         ('manual',3,3,3))
        self.assertEqual({user for user,_ in calls},{'1','2','3'})
        self.assertEqual({account for _,account in calls},set(accounts))
        release.set();self.join_completed(worker,3)

    def test_auto_mode_adds_a_third_account_while_two_tasks_are_still_running(self):
        worker,accounts=self.prepare(users='1 2 3 4',accounts=2,max_users=4,worker_count=0)
        initial=threading.Event();scaled=threading.Event();release=self.release_event()
        lock=threading.Lock();calls=[]
        def process(user,account,config,job):
            with lock:
                calls.append((user,account))
                if len(calls)==2:initial.set()
                if len(calls)==3:scaled.set()
            if not release.wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
        worker._process=process;worker.start()
        self.assertTrue(initial.wait(3))
        self.assertEqual(worker.workers_snapshot()['active'],2)
        third=self.store.account_add('Added while running','SESSDATA=PARALLEL_TEST_SECRET_DYNAMIC')
        self.assertTrue(scaled.wait(3),'new enabled account did not add a live evidence slot')
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['available_accounts'],snapshot['requested'],snapshot['effective'],snapshot['active']),
                         (3,3,3,3))
        self.assertEqual({account for _,account in calls},set(accounts+[third]))
        self.assertEqual(len({user for user,_ in calls}),3)
        release.set();self.join_completed(worker,4)
        self.assertEqual(len(calls),4)
        self.assertEqual(len({user for user,_ in calls}),4)

    def test_disabled_account_finishes_its_task_but_is_not_assigned_another_uid(self):
        worker,accounts=self.prepare(users='1 2 3 4 5 6',accounts=3,max_users=6,worker_count=0)
        initial=threading.Event();dispatched_more=threading.Event();completed_one=threading.Event()
        releases={account:self.release_event() for account in accounts}
        lock=threading.Lock();calls=[];active_accounts=set();duplicates=[]
        update=self.store.job_update
        def observe(job,**fields):
            update(job,**fields)
            if fields.get('processed',0)>=1:completed_one.set()
        self.store.job_update=observe
        def process(user,account,config,job):
            with lock:
                if account in active_accounts:duplicates.append(account)
                active_accounts.add(account);calls.append((user,account))
                if len(calls)==3:initial.set()
                if len(calls)>3:dispatched_more.set()
            if not releases[account].wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
            with lock:active_accounts.remove(account)
        worker._process=process;worker.start()
        self.assertTrue(initial.wait(3))
        disabled=calls[0][1]
        self.store.account_enabled(disabled,False)
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['available_accounts'],snapshot['requested'],snapshot['effective'],snapshot['active']),
                         (2,2,2,3),'disabling an account must allow its existing task to finish')
        releases[disabled].set()
        self.assertTrue(completed_one.wait(3))
        self.assertFalse(dispatched_more.wait(.15),'a replacement task exceeded the reduced account capacity')
        for release in releases.values():release.set()
        self.join_completed(worker,6)
        self.assertEqual(sum(account==disabled for _,account in calls),1)
        self.assertEqual(len({user for user,_ in calls}),6)
        self.assertEqual(duplicates,[],'an account was assigned to simultaneous UIDs')

    def test_disabling_every_account_drains_active_tasks_and_blocks_without_a_busy_loop(self):
        worker,accounts=self.prepare(users='1 2 3 4',accounts=2,max_users=4,worker_count=0)
        initial=threading.Event();release=self.release_event();lock=threading.Lock();calls=[]
        def process(user,account,config,job):
            with lock:
                calls.append((user,account))
                if len(calls)==2:initial.set()
            if not release.wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
        worker._process=process;worker.start()
        self.assertTrue(initial.wait(3))
        for account in accounts:self.store.account_enabled(account,False)
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['available_accounts'],snapshot['requested'],snapshot['effective'],snapshot['active']),
                         (0,0,0,2))
        release.set();worker.thread.join(4)
        self.assertFalse(worker.active(),'zero available accounts must terminate after in-flight tasks finish')
        job=self.store.state()['job']
        self.assertEqual((job['state'],job['processed']),('blocked',2))
        self.assertIn('没有可用 Cookie',job['message'])
        self.assertEqual(len(calls),2)
        self.assertTrue(all(account['status']!='expired' for account in self.store.accounts()),
                        'disabling accounts must not fabricate an expired authentication result')

    def test_manual_two_reports_three_available_accounts_without_dispatching_a_third(self):
        worker,_=self.prepare(users='1 2 3',accounts=3,max_users=3,worker_count=2)
        initial=threading.Event();extra=threading.Event();release=self.release_event();lock=threading.Lock();calls=[]
        def process(user,account,config,job):
            with lock:
                calls.append((user,account))
                if len(calls)==2:initial.set()
                if len(calls)>2:extra.set()
            if not release.wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
        worker._process=process;worker.start()
        self.assertTrue(initial.wait(3))
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['mode'],snapshot['configured'],snapshot['available_accounts'],
                          snapshot['requested'],snapshot['effective'],snapshot['active']),('manual',2,3,2,2,2))
        self.assertFalse(extra.wait(.15),'manual concurrency limit was ignored')
        release.set();self.join_completed(worker,3)

    def test_auto_mode_caps_at_twenty_and_reserves_single_batch_capacity(self):
        worker,_=self.prepare(users=' '.join(str(user) for user in range(1,23)),
                              accounts=22,max_users=21,worker_count=0)
        first_twenty=threading.Event();extra=threading.Event();release=self.release_event()
        lock=threading.Lock();calls=[];active_accounts=set();duplicates=[]
        def process(user,account,config,job):
            with lock:
                if account in active_accounts:duplicates.append(account)
                active_accounts.add(account);calls.append((user,account))
                if len(calls)==20:first_twenty.set()
                if len(calls)>20:extra.set()
            if not release.wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
            with lock:active_accounts.remove(account)
        worker._process=process;worker.start()
        self.assertTrue(first_twenty.wait(3),'auto mode did not reach the twenty-account cap')
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['available_accounts'],snapshot['requested'],snapshot['effective'],snapshot['active']),
                         (22,20,20,20))
        self.assertEqual(len({item['uid'] for item in snapshot['items']}),20)
        self.assertEqual(len({item['account_id'] for item in snapshot['items']}),20)
        self.assertNotIn('PARALLEL_TEST_SECRET',json.dumps(snapshot))
        self.assertFalse(extra.wait(.15),'auto mode exceeded its twenty-account cap')
        release.set();self.join_completed(worker,21)
        self.assertEqual(len(calls),21)
        self.assertEqual(len({user for user,_ in calls}),21)
        self.assertEqual(duplicates,[])
        self.assertEqual(self.store.list_profiles('candidates')['total'],1)

    def test_shared_model_inference_is_serialized_while_evidence_tasks_overlap(self):
        worker,_=self.prepare()
        downloaded=threading.Event();predicting=threading.Event();release=self.release_event()
        lock=threading.Lock();downloads=[];predictions=[];inference_active=[];overlap=[]
        class Downloading(FakeClient):
            def download(self,url,path,*args):
                super().download(url,path,*args)
                with lock:
                    downloads.append(path.stem)
                    if len(downloads)==2:downloaded.set()
        class ObservedClassifier(FakeClassifier):
            def predict(self,path,model):
                with lock:
                    inference_active.append(path.stem)
                    if len(inference_active)>1:overlap.append(list(inference_active))
                    predictions.append(path.stem);predicting.set()
                if not release.wait(3):raise RuntimeError('test release was not delivered')
                with lock:inference_active.remove(path.stem)
                return {'furry_score':.5,'model':model}
        worker.client=Downloading();worker.classifier=ObservedClassifier();worker.start()
        self.assertTrue(predicting.wait(3))
        self.assertTrue(downloaded.wait(3),'second task could not gather evidence during first inference')
        self.assertEqual(worker.workers_snapshot()['active'],2)
        release.set();self.join_completed(worker,2)
        self.assertEqual(set(predictions),{'1','2'})
        self.assertEqual(overlap,[],'shared model inference was entered concurrently')

    def test_manual_confirmation_of_claimed_candidate_does_not_expand_it_in_place(self):
        worker,_=self.prepare()
        both=threading.Event();lock=threading.Lock();requested=[]
        store=self.store
        class ConfirmDuringProfile(FakeClient):
            def profile(self,user,*args):
                with lock:
                    requested.append(user)
                    if len(requested)==2:both.set()
                if not both.wait(3):raise RuntimeError('second candidate was not claimed')
                store.decision(user,'yes','Manual confirmation during evidence request')
                return super().profile(user,*args)
        worker.client=ConfirmDuringProfile();worker.start();self.join_completed(worker,2)
        self.assertEqual(set(requested),{'1','2'})
        self.assertEqual(worker.client.pages,[],'candidate evidence task changed into relation expansion')
        self.assertEqual(self.store.list_profiles('seeds')['total'],2)
        self.assertTrue(all(self.store.profile(user)['collection_status']=='reviewed' for user in requested))

    def test_one_account_falls_back_to_one_inflight_task(self):
        worker,accounts=self.prepare(accounts=1)
        first=threading.Event();second=threading.Event();release=self.release_event();calls=[]
        def process(user,account,config,job):
            calls.append((user,account))
            if len(calls)==1:
                first.set()
                if not release.wait(3):raise RuntimeError('test release was not delivered')
            else:second.set()
            self.finish_candidate(user)
        worker._process=process;worker.start()
        self.assertTrue(first.wait(3))
        self.assertFalse(second.wait(.1),'one account was assigned to simultaneous tasks')
        snapshot=worker.workers_snapshot()
        self.assertEqual((snapshot['requested'],snapshot['effective'],snapshot['active']),(2,1,1))
        release.set();self.join_completed(worker,2)
        self.assertEqual({user for user,_ in calls},{'1','2'})
        self.assertEqual({account for _,account in calls},set(accounts))

    def test_single_round_reserves_capacity_before_dispatch(self):
        worker,_=self.prepare(users='1 2 3 4',max_users=3)
        both=threading.Event();release=self.release_event();calls=[];lock=threading.Lock()
        def process(user,account,config,job):
            with lock:
                calls.append(user)
                count=len(calls)
                if count==2:both.set()
            if count<=2 and not release.wait(3):raise RuntimeError('test release was not delivered')
            self.finish_candidate(user)
        worker._process=process;worker.start()
        self.assertTrue(both.wait(3))
        release.set();self.join_completed(worker,3)
        self.assertEqual(len(calls),3)
        self.assertEqual(len(set(calls)),3)
        self.assertEqual(self.store.list_profiles('candidates')['total'],1)
        self.assertEqual(sum(account['request_count'] for account in self.store.accounts()),3)

    def test_single_user_limit_does_not_dispatch_a_second_account(self):
        worker,_=self.prepare(users='1 2 3',max_users=1)
        calls=[]
        def process(user,account,config,job):
            calls.append(user);self.finish_candidate(user)
        worker._process=process;worker.start();self.join_completed(worker,1)
        self.assertEqual(len(calls),1)
        self.assertEqual(sum(account['request_count'] for account in self.store.accounts()),1)

    def test_pause_waits_for_every_child_before_reporting_inactive(self):
        worker,_=self.prepare()
        both=threading.Event();cancelled=threading.Event();allow_exit=self.release_event()
        lock=threading.Lock();threads=[];cancelled_count=[]
        def process(user,account,config,job):
            with lock:
                threads.append(threading.current_thread())
                if len(threads)==2:both.set()
            if not worker.stop_event.wait(3):raise RuntimeError('pause was not delivered')
            with lock:
                cancelled_count.append(user)
                if len(cancelled_count)==2:cancelled.set()
            if not allow_exit.wait(3):raise RuntimeError('test release was not delivered')
            raise InterruptedError()
        worker._process=process;worker.start()
        self.assertTrue(both.wait(3));worker.stop()
        self.assertTrue(cancelled.wait(3),'pause did not reach both running tasks')
        self.assertTrue(worker.active(),'coordinator exited while children were still active')
        self.assertEqual(worker.workers_snapshot()['active'],2)
        allow_exit.set();worker.thread.join(4)
        self.assertFalse(worker.active())
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(worker.workers_snapshot()['active'],0)
        self.assertEqual((self.store.state()['job']['state'],self.store.state()['job']['processed']),('paused',0))

    def test_fatal_risk_cancels_other_task_and_preserves_global_cooldown(self):
        worker,_=self.prepare(users='1 2 3',max_users=3)
        both=threading.Event();other_cancelled=threading.Event();lock=threading.Lock();calls=[]
        def process(user,account,config,job):
            with lock:
                calls.append(user)
                if len(calls)==2:both.set()
            if not both.wait(3):raise RuntimeError('concurrent peer did not start')
            if user=='1':raise CollectionError('Fixture platform risk','-352',True)
            if not worker.stop_event.wait(3):raise RuntimeError('global stop was not delivered')
            other_cancelled.set();raise InterruptedError()
        worker._process=process;worker.start();worker.thread.join(4)
        self.assertFalse(worker.active())
        self.assertTrue(other_cancelled.is_set())
        self.assertEqual(set(calls),{'1','2'})
        state=self.store.state()
        self.assertEqual((state['job']['state'],state['job']['processed']),('blocked',0))
        self.assertGreater(state['config']['cooldown_until'],now())
        self.assertEqual(worker.workers_snapshot()['active'],0)
        with self.assertRaisesRegex(ValueError,'冷却'):worker.start()

    def test_next_root_waits_for_all_current_children_even_after_their_verdicts_commit(self):
        worker,_=self.prepare(users='1 2 20',max_users=4)
        self.store.add_seeds('98 99',verdict='yes')
        self.store.classification('98','yes',{'avatar':{'success':True,'furry_score':.9}})
        self.store.classification('99','yes',{'avatar':{'success':True,'furry_score':.1}})
        both=threading.Event();release=self.release_event();lock=threading.Lock();active=set();calls=[];overlap=[];committed=[]
        def process(user,account,config,job):
            with lock:
                active.add(user);calls.append(user)
                if user=='99' and len(active)>1:overlap.append(set(active))
            if user=='98':
                self.store.relation_page(user,'followers',['1','2'],1,True)
                self.store.relation_page(user,'followings',['1'],1,True)
                self.store.finish_traversal(user)
            elif user in ('1','2'):
                self.finish_candidate(user)
                with lock:
                    committed.append(user)
                    if len(committed)==2:both.set()
                if not release.wait(3):raise RuntimeError('test release was not delivered')
            else:
                for direction in ('followers','followings'):
                    self.store.relation_page(user,direction,[],1,True)
                self.store.finish_traversal(user)
            with lock:active.remove(user)
        worker._process=process;worker.start()
        self.assertTrue(both.wait(3))
        self.assertNotIn('99',calls)
        self.assertEqual(self.store.get_config()['selected_seed_uid'],'98')
        self.assertEqual(self.store.state()['queue']['cycle_pending_candidates'],0)
        release.set();self.join_completed(worker,4)
        self.assertEqual(calls[0],'98')
        self.assertEqual(set(calls[1:3]),{'1','2'})
        self.assertEqual(calls[3:],['99'])
        self.assertEqual(self.store.profile('20')['collection_status'],'pending')
        self.assertEqual(overlap,[])

    def test_continuous_cycle_resumes_both_directions_then_assesses_each_related_uid_once(self):
        worker,_=self.prepare(users='20 21',accounts=2,max_users=1)
        self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','worker_count':2,'continuous':True,
                               'max_users':1,'pages_per_run':1})
        for user,score in (('1',.99),('9',.97)):
            self.store.add_seeds(user,verdict='yes')
            self.store.classification(user,'yes',{'automatic_reason':'Fixture confirmed seed',
                'intro':{'success':True,'strong_keywords':[]},
                'videos':{'success':True,'strong_title_keywords':[],'strong_tag_keywords':[]},
                'avatar':{'success':True,'furry_score':score}})
        events=[];next_root=threading.Event()
        class ObservedClient(FakeClient):
            def profile(self,user,*args):
                events.append(('profile',user))
                return super().profile(user,*args)
            def relation(self,user,direction,page,*args):
                events.append(('relation',user,direction,page))
                return super().relation(user,direction,page,*args)
        worker.client=ObservedClient();worker.ROUND_WAIT_SECONDS=.001;worker.IDLE_WAIT_SECONDS=.01
        calls=[];original=worker._process
        def process(user,account,config,job):
            calls.append(user)
            if user=='9':
                next_root.set();worker.stop();raise InterruptedError()
            return original(user,account,config,job)
        worker._process=process;worker.start()
        self.assertTrue(next_root.wait(4),'the completed seed cycle never selected the next highest seed')
        worker.thread.join(4)
        self.assertFalse(worker.active())
        self.assertEqual(calls,['1','1','2','9'])
        self.assertEqual(events,[('relation','1','followings',1),('relation','1','followers',1),
                                 ('relation','1','followings',2),('relation','1','followers',2),
                                 ('profile','2')])
        self.assertEqual(self.store.profile('1')['is_seed'],0)
        self.assertEqual(self.store.profile('2')['is_seed'],1)
        self.assertTrue(all(self.store.profile(user)['collection_status']=='pending' for user in ('20','21')))


class AvatarOnlyFilterTests(Fixture):
    def add_automatic(self,user,sources,automatic='yes',**fields):
        self.store.add_seeds(user)
        evidence={'avatar':{'success':True,'furry_score':.97},
                  'intro':{'success':True,'strong_keywords':[]},
                  'videos':{'success':True,'strong_title_keywords':[],'strong_tag_keywords':[]}}
        if sources is not None:evidence['decision_sources']=sources
        self.store.classification(user,automatic,evidence)
        if fields:self.store.update_profile(user,**fields)

    def test_exact_avatar_only_automatic_yes_excludes_other_evidence_and_manual_decisions(self):
        cases=(('1',['avatar'],'yes'),('2',['avatar','intro'],'yes'),
               ('3',['intro'],'yes'),('4',['video_title'],'yes'),
               ('5',['video_tag'],'yes'),('6',['avatar'],'unknown'),
               ('7',['avatar'],'no'),('8',None,'yes'),('9',[],'yes'),
               ('10',['avatar','avatar'],'yes'))
        for user,sources,automatic in cases:self.add_automatic(user,sources,automatic)
        self.add_automatic('11',['avatar']);self.store.decision('11','yes')
        self.add_automatic('12',['avatar']);self.store.decision('12','no')
        rows=self.store.list_profiles('yes',basis='avatar_only')
        self.assertEqual((rows['total'],[r['uid'] for r in rows['results']]),(1,['1']))
        self.assertEqual(self.store.list_profiles('yes')['total'],9)

    def test_malformed_missing_non_array_or_non_avatar_sources_are_not_matches(self):
        self.add_automatic('1',['avatar'])
        payloads=[{}, {'decision_sources':None}, {'decision_sources':'avatar'},
                  {'decision_sources':{'0':'avatar'}}, {'decision_sources':True},
                  {'decision_sources':[None]}, {'decision_sources':[1]},
                  {'decision_sources':['Avatar']}, {'decision_sources':['avatar','intro']},
                  {'previous_assessment':{'decision_sources':['avatar']}}, [], None]
        for index,payload in enumerate(payloads,20):
            user=str(index);self.store.add_seeds(user)
            with self.store.db() as c:
                c.execute("UPDATE profiles SET verdict='yes',automatic='yes',manual=NULL,evidence=? WHERE uid=?",
                          (json.dumps(payload),user))
        self.store.add_seeds('40')
        with self.store.db() as c:
            c.execute("UPDATE profiles SET verdict='yes',automatic='yes',manual=NULL,evidence='not-json' WHERE uid='40'")
        rows=self.store.list_profiles('yes',basis='avatar_only')
        self.assertEqual((rows['total'],[r['uid'] for r in rows['results']]),(1,['1']))

    def test_rejects_unknown_basis_and_basis_outside_yes_view(self):
        for basis in ('unknown',"avatar_only' OR 1=1 --",None,[],{}):
            with self.subTest(basis=basis),self.assertRaises(ValueError):
                self.store.list_profiles('yes',basis=basis)
        for view in ('seeds','candidates','no','all','missing'):
            with self.subTest(view=view),self.assertRaises(ValueError):
                self.store.list_profiles(view,basis='avatar_only')

    def test_filter_combines_search_status_sort_pagination_and_total(self):
        for user,name,score,traversal in (('101','Orange alpha',.90,'pending'),
                ('102','Orange beta',.99,'pending'),('103','Orange gamma',.95,'pending'),
                ('104','Orange complete',.98,'complete'),('105','Other',.97,'pending')):
            self.add_automatic(user,['avatar'],name=name,traversal=traversal)
            self.store.classification(user,'yes',{'decision_sources':['avatar'],
                'avatar':{'success':True,'furry_score':score}})
        self.add_automatic('106',['avatar','intro'],name='Orange multiple')
        first=self.store.list_profiles('yes',search='Orange',status='pending',sort='score',limit=2,basis='avatar_only')
        second=self.store.list_profiles('yes',page=2,search='Orange',status='pending',sort='score',limit=2,basis='avatar_only')
        self.assertEqual((first['total'],first['page'],first['page_size']),(3,1,2))
        self.assertEqual([r['uid'] for r in first['results']],['102','103'])
        self.assertEqual((second['total'],second['page']), (3,2))
        self.assertEqual([r['uid'] for r in second['results']],['101'])
        complete=self.store.list_profiles('yes',search='104',status='complete',basis='avatar_only')
        self.assertEqual([r['uid'] for r in complete['results']],['104'])
        self.assertEqual(self.store.list_profiles('yes',page=5,search='absent',basis='avatar_only')['total'],0)

    def test_manual_yes_and_no_leave_review_queue_without_deleting_profiles_or_edges(self):
        self.add_automatic('1',['avatar']);self.add_automatic('3',['avatar'])
        self.store.update_profile('1',name='Retained account',intro='Retained intro',image_path='avatars/1.jpg')
        self.store.relation_page('1','followers',['2'],1,True)
        before=self.store.profile('1');counts=self.store.state()['counts']['total']
        self.store.decision('1','no','Human exclusion fixture')
        self.store.decision('3','yes','Human confirmation fixture')
        self.assertEqual(self.store.list_profiles('yes',basis='avatar_only')['total'],0)
        row=self.store.profile('1')
        self.assertEqual((row['verdict'],row['manual'],row['automatic'],row['is_seed']),('no','no','yes',0))
        self.assertEqual(row['manual_reason'],'Human exclusion fixture')
        for field in ('name','intro','image_path','evidence','relations_count','children'):
            self.assertEqual(row[field],before[field])
        self.assertEqual(self.store.state()['counts']['total'],counts)
        self.assertEqual((self.store.profile('3')['verdict'],self.store.profile('3')['manual']),('yes','yes'))
        self.assertEqual(self.store.list_profiles('yes')['total'],1)
        self.assertEqual(self.store.list_profiles('no')['total'],1)

    def test_filter_is_read_only_and_requires_matching_automatic_verdict(self):
        self.add_automatic('1',['avatar']);self.add_automatic('2',['avatar'])
        with self.store.db() as c:c.execute("UPDATE profiles SET automatic='unknown' WHERE uid='2'")
        before=self.store.profile('1')
        rows=self.store.list_profiles('yes',basis='avatar_only')
        self.assertEqual([r['uid'] for r in rows['results']],['1'])
        self.assertEqual(self.store.profile('1'),before)


class HTTPTests(Fixture):
    def test_optimized_settings_are_available_and_round_trip_through_http(self):
        status,body=self.request('/api/bootstrap')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['collector_revision'],2)
        status,body=self.request('/api/settings',{**DEFAULT_CONFIG,'collection_scope':'candidates_first',
                                                 'evidence_mode':'fast','max_users':100})
        self.assertEqual(status,200)
        saved=json.loads(body)
        self.assertEqual((saved['collection_scope'],saved['evidence_mode'],saved['max_users']),
                         ('candidates_first','fast',100))
        state=json.loads(self.request('/api/state')[1])
        self.assertEqual(state['config']['collection_scope'],'candidates_first')
        self.assertEqual(state['config']['evidence_mode'],'fast')

    def setUp(self):
        super().setUp()
        self.ident=self.store.account_add('Fixture','SESSDATA=TEST_SECRET_NEVER_RETURN')
        worker=Collector(self.store,FakeClient(),FakeClassifier())
        self.server=DashboardServer(self.root,0,self.store,worker)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        super().tearDown()

    def request(self,path,data=None,headers=None):
        combined={'Content-Type':'application/json'}
        if data is not None:combined.update({'Origin':self.base,'X-Panel-Token':self.server.token})
        combined.update(headers or {})
        request=urllib.request.Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,headers=combined)
        try:
            with self.opener.open(request,timeout=5) as response:return response.status,response.read()
        except urllib.error.HTTPError as error:return error.code,error.read()

    def add_avatar_filter_fixture(self):
        self.store.add_seeds('101 102 103 104 105 106')
        for user,sources in (('101',['avatar']),('102',['avatar']),('103',['intro']),
                              ('104',['avatar','video_tag']),('105',['avatar']),('106',['avatar'])):
            self.store.classification(user,'yes',{'decision_sources':sources,
                'avatar':{'success':True,'furry_score':.97}})
        self.store.update_profile('101',name='Orange target')
        self.store.update_profile('102',name='Orange complete',traversal='complete')
        self.store.update_profile('103',name='Orange text')
        self.store.decision('105','yes');self.store.decision('106','no')

    def test_avatar_only_profiles_returns_only_automatic_matches_and_no_credentials(self):
        self.add_avatar_filter_fixture()
        status,body=self.request('/api/profiles?view=yes&basis=avatar_only&sort=score')
        self.assertEqual(status,200)
        data=json.loads(body)
        self.assertEqual((data['total'],{r['uid'] for r in data['results']}),(2,{'101','102'}))
        self.assertNotIn(b'TEST_SECRET',body)
        self.assertEqual(json.loads(self.request('/api/profiles?view=yes')[1])['total'],5)

    def test_avatar_only_export_applies_same_search_status_sort_and_basis(self):
        self.add_avatar_filter_fixture()
        self.store.add_seeds('107 108')
        for user,score in (('107',.99),('108',.91)):
            self.store.classification(user,'yes',{'decision_sources':['avatar'],
                'avatar':{'success':True,'furry_score':score}})
            self.store.update_profile(user,name='Orange extra')
        query='/api/export?view=yes&basis=avatar_only&q=Orange&status=pending&sort=score'
        status,body=self.request(query)
        self.assertEqual(status,200)
        data=json.loads(body)
        self.assertEqual(data['meta']['total_count'],3)
        self.assertEqual([r['uid'] for r in data['uids']],[107,101,108])
        self.assertNotIn(b'TEST_SECRET',body)
        self.assertNotIn(b'fingerprint',body)
        status,body=self.request('/api/export?view=yes&basis=avatar_only&q=absent')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['uids'],[])

    def test_avatar_only_export_reads_all_pages_without_duplicates(self):
        users=[str(value) for value in range(1000,1103)]
        self.store.add_seeds(' '.join(users))
        for user in users:self.store.classification(user,'yes',{'decision_sources':['avatar']})
        self.store.add_seeds('2000');self.store.classification('2000','yes',{'decision_sources':['intro']})
        status,body=self.request('/api/export?view=yes&basis=avatar_only')
        self.assertEqual(status,200)
        data=json.loads(body)
        self.assertEqual((data['meta']['total_count'],len(data['uids'])),(103,103))
        self.assertEqual({r['uid'] for r in data['uids']},{int(user) for user in users})
        self.assertNotIn(b'TEST_SECRET',body)

    def test_avatar_only_api_rejects_unknown_or_wrong_view_basis_and_preserves_host_guard(self):
        self.add_avatar_filter_fixture()
        for endpoint in ('/api/profiles','/api/export'):
            for query in ('view=yes&basis=unknown','view=seeds&basis=avatar_only',
                          'view=candidates&basis=avatar_only','view=no&basis=avatar_only',
                          'view=all&basis=avatar_only','basis=avatar_only',
                          'view=yes&basis='+urllib.parse.quote("avatar_only' OR 1=1 --")):
                with self.subTest(endpoint=endpoint,query=query):
                    status,body=self.request(endpoint+'?'+query)
                    self.assertEqual(status,400)
                    self.assertNotIn(b'TEST_SECRET',body)
            self.assertEqual(self.request(endpoint+'?view=yes&basis=avatar_only',headers={'Host':'evil.invalid'})[0],403)

    def test_avatar_only_manual_exclusion_keeps_evidence_and_edges_and_requires_csrf(self):
        self.add_avatar_filter_fixture()
        self.store.relation_page('101','followers',['500'],1,True)
        evidence=self.store.profile('101')['evidence']
        for headers in ({'Origin':'https://evil.invalid'},{'X-Panel-Token':''}):
            status,body=self.request('/api/decision',{'uid':'101','decision':'no'},headers)
            self.assertEqual(status,403)
            self.assertEqual(self.store.profile('101')['verdict'],'yes')
        status,body=self.request('/api/decision',{'uid':'101','decision':'no','reason':'Not a furry avatar'})
        self.assertEqual(status,200)
        self.assertNotIn(b'TEST_SECRET',body)
        rows=json.loads(self.request('/api/profiles?view=yes&basis=avatar_only')[1])
        self.assertEqual([r['uid'] for r in rows['results']],['102'])
        row=self.store.profile('101')
        self.assertEqual((row['verdict'],row['manual'],row['is_seed'],row['relations_count']),('no','no',0,1))
        self.assertEqual(row['evidence'],evidence)
        self.assertEqual(self.store.profile('500')['parent_uid'],'101')
        self.assertEqual(self.request('/api/decision',{'uid':'102','decision':'yes'})[0],200)
        self.assertEqual(json.loads(self.request('/api/profiles?view=yes&basis=avatar_only')[1])['total'],0)

    def test_cookie_secret_never_leaves_api(self):
        for path in ('/api/state','/api/bootstrap'):
            status,body=self.request(path)
            self.assertEqual(status,200)
            self.assertNotIn(b'TEST_SECRET',body)
        self.assertEqual(self.request('/cookies.json')[0],404)
        self.assertEqual(self.request('/data/panel/panel.sqlite3')[0],404)

    def test_delete_cookie_checks_origin_token_host_and_never_returns_credentials(self):
        for headers in ({'Origin':'https://evil.invalid'},{'Origin':''},{'X-Panel-Token':''},{'Host':'evil.invalid'}):
            with self.subTest(headers=headers):
                status,body=self.request('/api/cookies/delete',{'id':self.ident},headers)
                self.assertEqual(status,403)
                self.assertNotIn(b'TEST_SECRET',body)
                self.assertEqual([item['id'] for item in self.store.accounts()],[self.ident])
        self.store.account_result(self.ident,'expired')
        status,body=self.request('/api/cookies/delete',{'id':self.ident})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body),{'ok':True})
        self.assertNotIn(b'TEST_SECRET',body)
        self.assertEqual(json.loads(self.request('/api/state')[1])['accounts'],[])
        self.assertEqual(self.request('/api/cookies/delete',{'id':self.ident})[0],400)
        self.assertEqual(self.request('/api/cookies/delete',{})[0],400)
        self.assertEqual(self.request('/api/cookies/delete',{'id':['invalid']})[0],400)

    def test_delete_cookie_refuses_validation_reservation_and_exposes_only_busy_boolean(self):
        worker=self.server.collector
        entered=threading.Event();release=threading.Event();results=[]
        class WaitingValidation(FakeClient):
            def validate(self,secret):
                entered.set()
                if not release.wait(3):raise RuntimeError('HTTP fixture validation release missing')
                return 'Fixture account'
        worker.client=WaitingValidation()
        validation=threading.Thread(target=lambda:results.append(self.request('/api/cookies/validate',{'id':self.ident})),daemon=True)
        validation.start()
        try:
            self.assertTrue(entered.wait(2))
            status,body=self.request('/api/state')
            self.assertEqual(status,200)
            account=json.loads(body)['accounts'][0]
            self.assertIs(account['busy'],True)
            self.assertNotIn('secret',account);self.assertNotIn('fingerprint',account)
            self.assertNotIn(b'TEST_SECRET',body)
            status,body=self.request('/api/cookies/delete',{'id':self.ident})
            self.assertEqual(status,400)
            self.assertIn('正在采集或验证',json.loads(body)['error'])
            self.assertNotIn(b'TEST_SECRET',body)
        finally:release.set();validation.join(4)
        self.assertFalse(validation.is_alive())
        self.assertEqual(results[0][0],200)
        self.assertIs(json.loads(self.request('/api/state')[1])['accounts'][0]['busy'],False)
        self.assertEqual(self.request('/api/cookies/delete',{'id':self.ident})[0],200)

    def test_worker_snapshot_is_available_without_exposing_credentials(self):
        status,body=self.request('/api/state')
        self.assertEqual(status,200)
        workers=json.loads(body)['workers']
        self.assertEqual((workers['requested'],workers['effective'],workers['active']),(1,1,0))
        self.assertEqual((workers['mode'],workers['configured'],workers['available_accounts'],workers['limit']),
                         ('manual',1,1,20))
        self.assertEqual(workers['items'],[])
        self.assertNotIn(b'TEST_SECRET',body)

    def test_auto_worker_snapshot_counts_only_enabled_nonexpired_accounts(self):
        self.store.account_add('Usable second','SESSDATA=TEST_SECRET_SECOND')
        disabled=self.store.account_add('Disabled','SESSDATA=TEST_SECRET_DISABLED')
        expired=self.store.account_add('Expired','SESSDATA=TEST_SECRET_EXPIRED')
        self.store.account_enabled(disabled,False)
        self.store.account_result(expired,'expired')
        self.store.set_config({**DEFAULT_CONFIG,'collection_scope':'seed_cycle','evidence_mode':'full','worker_count':0})
        status,body=self.request('/api/state')
        self.assertEqual(status,200)
        workers=json.loads(body)['workers']
        self.assertEqual((workers['mode'],workers['configured'],workers['available_accounts'],
                          workers['requested'],workers['effective'],workers['active']),('auto',0,2,2,2,0))
        self.assertEqual(workers['items'],[])
        self.assertNotIn(b'TEST_SECRET',body)

    def test_quick_review_can_read_evidence_and_decide_without_touching_other_candidates(self):
        self.store.add_seeds('123 456',base=12)
        evidence={'avatar':{'success':True,'furry_score':.6},
                  'intro':{'success':True,'strong_keywords':[]},
                  'videos':{'success':True,'strong_title_keywords':[],'strong_tag_keywords':[],
                            'items':[{'title':'Fixture title','tags':['Fixture tag']} ]}}
        self.store.update_profile('123',name='Review fixture',intro='Fixture introduction',collection_status='needs_review')
        self.store.classification('123','unknown',evidence)
        status,body=self.request('/api/profile?uid=123')
        self.assertEqual(status,200)
        row=json.loads(body)
        self.assertEqual((row['base_score'],row['identity_score']),(12,24))
        self.assertEqual(row['evidence']['videos']['items'][0]['tags'],['Fixture tag'])
        status,body=self.request('/api/decision',{'uid':'123','decision':'yes','reason':'Quick review fixture'})
        self.assertEqual(status,200)
        self.store.classification('123','no',evidence)
        self.store.finalize_classification('123')
        row=self.store.profile('123')
        self.assertEqual((row['verdict'],row['is_seed'],row['collection_status']),('yes',1,'reviewed'))
        candidates=json.loads(self.request('/api/profiles?view=candidates')[1])
        self.assertEqual([item['uid'] for item in candidates['results']],['456'])
        self.assertEqual(self.store.profile('456')['manual'],None)

    def test_cross_origin_missing_token_and_bad_host_rejected(self):
        self.assertEqual(self.request('/api/seeds',{'uids':'123'}, {'Origin':'https://evil.invalid'})[0],403)
        self.assertEqual(self.request('/api/seeds',{'uids':'123'}, {'X-Panel-Token':''})[0],403)
        self.assertEqual(self.request('/api/state',headers={'Host':'evil.invalid'})[0],403)
        self.assertEqual(self.store.state()['counts']['total'],0)

    def test_copied_cookie_header_is_accepted_without_exposing_secret(self):
        status,body=self.request('/api/cookies',{'label':'Copied','cookie':'Cookie: SESSDATA=ANOTHER_TEST_SECRET\r\n'})
        self.assertEqual(status,200)
        self.assertNotIn(b'TEST_SECRET',body)
        ident=json.loads(body)['id']
        self.assertEqual(self.store.account_secret(ident),'SESSDATA=ANOTHER_TEST_SECRET')
        self.assertNotIn(b'TEST_SECRET',self.request('/api/state')[1])

    def test_truncated_cookie_returns_actionable_error_without_saving(self):
        status,body=self.request('/api/cookies',{'label':'Bad','cookie':'SESSDATA=ANOTHER_TEST\u2026SECRET'})
        self.assertEqual(status,400)
        self.assertIn('省略号',json.loads(body)['error'])
        self.assertNotIn(b'ANOTHER_TEST',body)
        self.assertEqual(len(self.store.accounts()),1)

    def test_account_map_import_through_http_never_returns_secret(self):
        (self.root/'cookies.json').write_text(json.dumps({'fixture':{'SESSDATA':'IMPORTED_TEST_SECRET'}}),encoding='utf-8')
        status,body=self.request('/api/cookies/import',{})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body),{'added':1,'skipped':0})
        self.assertNotIn(b'IMPORTED_TEST_SECRET',body)
        self.assertNotIn(b'IMPORTED_TEST_SECRET',self.request('/api/state')[1])

    def test_add_and_decide_through_real_http(self):
        self.assertEqual(self.request('/api/seeds',{'uids':'123 123 456','base_score':12})[0],200)
        self.assertEqual(self.store.state()['counts']['total'],2)
        self.assertEqual(self.request('/api/decision',{'uid':'123','decision':'yes'})[0],200)
        status,body=self.request('/api/profiles?view=yes')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['total'],1)
        status,body=self.request('/api/export?view=yes')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['uids'][0]['uid'],123)

    def test_candidate_view_and_counts_change_only_after_confirmation(self):
        self.assertEqual(self.request('/api/seeds',{'uids':'123 456'})[0],200)
        status,body=self.request('/api/profiles?view=candidates')
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['total'],2)
        counts=json.loads(self.request('/api/state')[1])['counts']
        self.assertEqual((counts['candidates'],counts['seeds']),(2,0))
        self.assertEqual(self.request('/api/decision',{'uid':'123','decision':'yes'})[0],200)
        counts=json.loads(self.request('/api/state')[1])['counts']
        self.assertEqual((counts['candidates'],counts['seeds']),(1,1))
        self.assertEqual(self.request('/api/decision',{'uid':'123','decision':'no'})[0],200)
        counts=json.loads(self.request('/api/state')[1])['counts']
        self.assertEqual((counts['candidates'],counts['seeds']),(1,0))

    def test_non_image_file_cannot_be_served_as_avatar(self):
        self.store.add_seeds('123')
        path=self.root/'data'/'panel'/'secret.jpg';path.write_bytes(b'{"secret":"TEST_SECRET"}')
        self.store.update_profile('123',image_path=str(path.relative_to(self.root)))
        status,body=self.request('/avatar/123')
        self.assertEqual(status,400)
        self.assertNotIn(b'TEST_SECRET',body)


class SeedAdmissionGuardTests(Fixture):
    def test_unconfirmed_candidate_cannot_create_relation_children(self):
        self.store.add_seeds('1')
        self.assertIs(self.store.relation_page('1','followers',['2'],1,True),False)
        self.assertEqual(self.store.state()['relation_count'],0)
        self.assertEqual(self.store.state()['counts']['total'],1)

    def test_manual_rejection_during_request_prevents_new_relation_write(self):
        self.store.add_seeds('1',verdict='yes')
        self.store.decision('1','no')
        self.assertIs(self.store.relation_page('1','followers',['2'],1,True),False)
        self.assertEqual(self.store.state()['relation_count'],0)


if __name__=='__main__':unittest.main()
