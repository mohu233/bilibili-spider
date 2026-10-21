"""Request-saving decisions must preserve conservative exclusions and manual choices."""
import unittest
import threading

from .collector import BiliClient, CollectionError, Collector
from .store import DEFAULT_CONFIG
from . import tests as legacy
from .tests import Fixture, SignalClient, SignalClassifier


class FastCollectionTests(Fixture):
    def prepare(self,client=None,score=.95):
        self.store.add_seeds('123')
        account=self.store.account_add('Offline fixture','SESSDATA=OFFLINE_FAST_FIXTURE')
        config={**DEFAULT_CONFIG,'evidence_mode':'fast','collection_scope':'candidates_first'}
        client=client or SignalClient()
        worker=Collector(self.store,client,SignalClassifier(score))
        return worker,account,config,self.store.job_create(config),client

    def process(self,**options):
        worker,account,config,job,client=self.prepare(**options)
        worker._process('123',account,config,job)
        return self.store.profile('123'),client

    def test_positive_avatar_skips_all_video_requests_with_explicit_evidence(self):
        row,client=self.process()
        self.assertEqual(client.checked,['profile','avatar'])
        self.assertEqual(row['verdict'],'yes')
        self.assertEqual(row['evidence']['decision_sources'],['avatar'])
        self.assertTrue(row['evidence']['videos']['skipped'])
        self.assertFalse(row['evidence']['videos']['success'])
        self.assertNotIn('error',row['evidence']['videos'])
        self.assertEqual(client.pages,[])

    def test_strong_intro_skips_videos_even_with_low_avatar(self):
        row,client=self.process(client=SignalClient(intro='Furry artist'),score=.01)
        self.assertEqual(client.checked,['profile','avatar'])
        self.assertEqual(row['verdict'],'yes')
        self.assertEqual(row['evidence']['decision_sources'],['intro'])

    def test_no_requires_complete_video_evidence(self):
        row,client=self.process(score=.01)
        self.assertEqual(client.checked,['profile','avatar','videos'])
        self.assertEqual(row['verdict'],'no')
        self.assertTrue(row['evidence']['videos']['success'])

    def test_missing_videos_with_low_avatar_remains_unknown(self):
        row,_=self.process(score=.01,client=SignalClient(video_error=CollectionError('Offline failure')))
        self.assertEqual(row['verdict'],'unknown')
        self.assertEqual(row['collection_status'],'needs_review')

    def test_negated_intro_still_requires_review_with_low_avatar(self):
        row,_=self.process(score=.01,client=SignalClient(intro='不是furry'))
        self.assertEqual(row['verdict'],'unknown')
        self.assertTrue(row['evidence']['intro']['negated_keywords'])

    def test_video_signal_can_confirm_when_avatar_is_low(self):
        row,client=self.process(score=.01,client=SignalClient(title='我的兽设'))
        self.assertEqual(row['verdict'],'yes')
        self.assertEqual(row['evidence']['decision_sources'],['video_title'])
        self.assertEqual(client.checked,['profile','avatar','videos'])

    def test_manual_rejection_wins_over_fast_positive(self):
        worker,account,config,job,_=self.prepare()
        self.store.decision('123','no')
        worker._process('123',account,config,job)
        row=self.store.profile('123')
        self.assertEqual((row['automatic'],row['verdict'],row['is_seed']),('yes','no',0))

    def test_valid_avatar_cache_reused_only_for_same_url(self):
        worker,account,config,job,client=self.prepare()
        path=self.store.directory/'avatars'/'123.img'
        path.parent.mkdir(parents=True,exist_ok=True)
        from PIL import Image
        Image.new('RGB',(2,2),'white').save(path,format='PNG')
        self.store.update_profile('123',avatar_url='https://i0.hdslb.com/test.jpg',
                                  image_path=str(path.relative_to(self.store.root)))
        worker._process('123',account,config,job)
        self.assertEqual(client.checked,['profile'])
        self.assertTrue(self.store.profile('123')['evidence']['avatar']['cache_hit'])

    def test_changed_avatar_url_downloads_again(self):
        worker,account,config,job,client=self.prepare()
        path=self.store.directory/'avatars'/'123.img'
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'old fixture')
        self.store.update_profile('123',avatar_url='https://i0.hdslb.com/old.jpg',
                                  image_path=str(path.relative_to(self.store.root)))
        worker._process('123',account,config,job)
        self.assertEqual(client.checked,['profile','avatar'])
        self.assertFalse(self.store.profile('123')['evidence']['avatar']['cache_hit'])

    def test_failed_changed_avatar_download_never_binds_old_image_to_new_url(self):
        class RetryDownload(SignalClient):
            attempts=0
            def download(self,url,path,*args):
                self.attempts+=1
                if self.attempts==1:raise CollectionError('Offline image failure')
                return super().download(url,path,*args)
        worker,account,config,job,client=self.prepare(client=RetryDownload(),score=.01)
        path=self.store.directory/'avatars'/'123.img'
        path.parent.mkdir(parents=True,exist_ok=True)
        from PIL import Image
        Image.new('RGB',(2,2),'white').save(path,format='PNG')
        self.store.update_profile('123',avatar_url='https://i0.hdslb.com/old.jpg',
                                  image_path=str(path.relative_to(self.store.root)))
        worker._process('123',account,config,job)
        self.assertEqual(self.store.profile('123')['image_path'],'')
        worker._process('123',account,config,job)
        self.assertEqual(client.attempts,2)
        self.assertFalse(self.store.profile('123')['evidence']['avatar']['cache_hit'])

    def test_video_risk_preserves_avatar_and_long_retry_after(self):
        error=CollectionError('Offline rate limit','-799',True)
        error.cooldown_seconds=1200
        worker,_,config,job,_=self.prepare(score=.01,client=SignalClient(video_error=error))
        worker._run(job,config)
        state=self.store.state()
        self.assertEqual(state['job']['state'],'blocked')
        self.assertTrue(self.store.profile('123')['evidence']['avatar']['success'])
        from datetime import datetime,timezone
        self.assertGreater((datetime.fromisoformat(state['config']['cooldown_until'])-datetime.now(timezone.utc)).total_seconds(),1190)

    def test_legacy_http_cdn_image_upgrades_to_https_without_cookie(self):
        import io
        from PIL import Image
        raw=io.BytesIO();Image.new('RGB',(2,2),'white').save(raw,format='PNG')
        client=BiliClient();calls=[]
        def request(url,**options):
            calls.append((url,options));return raw.getvalue()
        client._request=request
        path=self.store.directory/'avatars'/'http.img'
        client.download('http://i0.hdslb.com/bfs/face/test.jpg',path,threading.Event(),1.5)
        self.assertEqual(calls[0][0],'https://i0.hdslb.com/bfs/face/test.jpg')
        self.assertNotIn('cookie',calls[0][1])
        self.assertTrue(path.is_file())


class FastVideoSamplingTests(unittest.TestCase):
    client_with=legacy.RecentVideoClientTests.client_with

    def test_title_match_does_not_request_tags_or_claim_complete_sample(self):
        client,calls=self.client_with([{'bvid':'BV0000000001','title':'我的兽装'},
                                      {'bvid':'BV0000000002','title':'Other'}])
        result=client.recent_video_tags('123','SESSDATA=OFFLINE_FAST_FIXTURE',threading.Event(),1.5,True)
        self.assertEqual(len(calls),2)
        self.assertTrue(result['skipped'])
        self.assertFalse(result['success'])
        self.assertTrue(result['videos'][0]['tags_skipped'])

    def test_tag_match_does_not_fetch_later_video_tags(self):
        client,calls=self.client_with([{'bvid':'BV0000000001','title':'Day one'},
                                      {'bvid':'BV0000000002','title':'Day two'}])
        result=client.recent_video_tags('123','SESSDATA=OFFLINE_FAST_FIXTURE',threading.Event(),1.5,True)
        self.assertEqual(len(calls),3)
        self.assertTrue(result['skipped'])
        self.assertFalse(result['success'])

    def test_later_title_match_skips_all_tag_requests(self):
        client,calls=self.client_with([{'bvid':'BV0000000001','title':'Day one'},
                                      {'bvid':'BV0000000002','title':'我的兽设'}])
        result=client.recent_video_tags('123','SESSDATA=OFFLINE_FAST_FIXTURE',threading.Event(),1.5,True)
        self.assertEqual(len(calls),2)
        self.assertEqual(len(result['videos']),2)
        self.assertTrue(all(video['tags_skipped'] for video in result['videos']))

    def test_no_signals_fetches_all_tags_before_completing_sample(self):
        client,calls=self.client_with([{'bvid':'BV0000000001','title':'Day one'},
                                      {'bvid':'BV0000000002','title':'Day two'}],tags=[{'tag_name':'日常'}])
        result=client.recent_video_tags('123','SESSDATA=OFFLINE_FAST_FIXTURE',threading.Event(),1.5,True)
        self.assertEqual(len(calls),4)
        self.assertTrue(result['success'])
        self.assertNotIn('skipped',result)
