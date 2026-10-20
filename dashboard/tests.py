"""Offline tests for scoring, new-data isolation, pagination, and the HTTP boundary."""
import base64
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .collector import CollectionError,Collector
from .store import DEFAULT_CONFIG,Store,score_parts
from .web import DashboardServer

RUNS=Path(__file__).parents[1]/'.test-runs'


class Fixture(unittest.TestCase):
    def setUp(self):
        self.root=RUNS/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.store=Store(self.root)

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


class FakeClassifier:
    def availability(self):return {name:{'weights':True,'runtime':True} for name in ('mobilenetv3_v0.1_dist','caformer_s36_v0.1','yolo')}
    def predict(self,*args):return {'furry_score':.95,'model':'mobilenetv3_v0.1_dist'}


class FakeClient:
    def __init__(self):self.pages=[]
    def profile(self,user,*args):return {'name':'Test','intro':'','avatar_url':'https://i0.hdslb.com/test.jpg','fan_count':0,'follow_count':0}
    def download(self,url,path,*args):path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
    def relation(self,user,direction,page,*args):
        self.pages.append((user,direction,page))
        return (['2'],False) if page==1 else ([],True)
    def validate(self,secret):return 'Fixture account'


class CollectorTests(Fixture):
    def prepare(self,client=None):
        self.store.add_seeds('1',10,'yes')
        account=self.store.account_add('Fixture','SESSDATA=TEST_SECRET_NEVER_RETURN')
        client=client or FakeClient()
        worker=Collector(self.store,client,FakeClassifier())
        options={**DEFAULT_CONFIG,'max_users':1,'pages_per_run':1}
        job=self.store.job_create(options)
        return worker,account,client,options,job

    def test_page_cap_is_partial_then_resume_completes(self):
        worker,account,client,options,job=self.prepare()
        worker._process('1',account,options,job)
        row=self.store.profile('1')
        self.assertEqual(row['traversal'],'partial')
        self.assertEqual((row['followers_page'],row['followings_page']),(2,2))
        worker._process('1',account,options,job)
        self.assertEqual(self.store.profile('1')['traversal'],'complete')
        self.assertIn(('1','followers',2),client.pages)
        self.assertIn(('1','followings',2),client.pages)

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


class HTTPTests(Fixture):
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

    def test_cookie_secret_never_leaves_api(self):
        for path in ('/api/state','/api/bootstrap'):
            status,body=self.request(path)
            self.assertEqual(status,200)
            self.assertNotIn(b'TEST_SECRET',body)
        self.assertEqual(self.request('/cookies.json')[0],404)
        self.assertEqual(self.request('/data/panel/panel.sqlite3')[0],404)

    def test_cross_origin_missing_token_and_bad_host_rejected(self):
        self.assertEqual(self.request('/api/seeds',{'uids':'123'}, {'Origin':'https://evil.invalid'})[0],403)
        self.assertEqual(self.request('/api/seeds',{'uids':'123'}, {'X-Panel-Token':''})[0],403)
        self.assertEqual(self.request('/api/state',headers={'Host':'evil.invalid'})[0],403)
        self.assertEqual(self.store.state()['counts']['total'],0)

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

    def test_non_image_file_cannot_be_served_as_avatar(self):
        self.store.add_seeds('123')
        path=self.root/'data'/'panel'/'secret.jpg';path.write_bytes(b'{"secret":"TEST_SECRET"}')
        self.store.update_profile('123',image_path=str(path.relative_to(self.root)))
        status,body=self.request('/avatar/123')
        self.assertEqual(status,400)
        self.assertNotIn(b'TEST_SECRET',body)


if __name__=='__main__':unittest.main()
