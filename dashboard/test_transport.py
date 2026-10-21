"""Offline request-boundary tests. No calls leave the process."""
import datetime
import email.utils
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from .transport import CollectionError,RequestTransport,_SafeRedirectHandler,_api_lane_key,_retry_after


API='https://api.bilibili.com/x/fixture'
IMAGE='https://i0.hdslb.com/bfs/face/fixture.jpg'


class Response:
    def __init__(self,data=b'fixture',before_read=None):
        self.data=data
        self.before_read=before_read

    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,limit):
        if self.before_read:self.before_read()
        return self.data[:limit]


class Opener:
    def __init__(self,callback):self.callback=callback
    def open(self,request,timeout):return self.callback(request)


class WaitingStop(threading.Event):
    """Prove a paced request entered its cancellable wait, without timed sleeps."""
    def __init__(self):
        super().__init__()
        self.waiting=threading.Event()
        self.two_waiting=threading.Event()
        self.waiter_ids=set()
        self.waiter_lock=threading.Lock()

    def wait(self,timeout=None):
        with self.waiter_lock:
            self.waiter_ids.add(threading.get_ident())
            self.waiting.set()
            if len(self.waiter_ids)>=2:self.two_waiting.set()
        return super().wait(timeout)


def success(before_read=None):
    return Response(json.dumps({'code':0,'data':{'fixture':True}}).encode(),before_read)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.threads=[]
        self.errors=[]
        self.release=threading.Event()
        self.stop_events=[]

    def tearDown(self):
        for stop in self.stop_events:stop.set()
        self.release.set()
        for thread in self.threads:
            thread.join(3)
            self.assertFalse(thread.is_alive(),'fixture worker did not finish')

    def launch(self,callback):
        def run():
            try:callback()
            except BaseException as error:self.errors.append(error)
        thread=threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread

    def block(self):
        if not self.release.wait(3):raise AssertionError('fixture release was not delivered')

    def stop_event(self):
        stop=WaitingStop();self.stop_events.append(stop);return stop

    def test_same_account_waits_before_first_request_and_after_previous_response(self):
        client=RequestTransport()
        first=threading.Event();second=threading.Event();starts=[];response_finished=[]
        def first_response():
            self.block()
            response_finished.append(time.perf_counter())
        def open_request(request):
            starts.append(time.perf_counter())
            (first if len(starts)==1 else second).set()
            return success(first_response if len(starts)==1 else None)
        client.opener=Opener(open_request)
        started=time.perf_counter()
        self.launch(lambda:client._request(API,'DedeUserID=123; SESSDATA=FIRST',interval=.06))
        self.assertTrue(first.wait(2))
        self.assertGreaterEqual(starts[0]-started,.05,'first API request skipped its full configured wait')
        self.launch(lambda:client._request(API+'?next=1','DedeUserID=123; SESSDATA=FIRST',interval=.06))
        self.assertFalse(second.wait(.1),'same account sent another API before the first response finished')
        self.release.set()
        self.assertTrue(second.wait(2))
        self.assertGreaterEqual(starts[1]-response_finished[0],.05,
                                'the next account request did not wait a full interval after the previous response')
        for thread in self.threads:thread.join(2)
        self.assertEqual(self.errors,[])

    def test_image_does_not_wait_for_or_consume_api_interval_and_omits_cookie(self):
        client=RequestTransport();stop=self.stop_event();requests=[]
        client.opener=Opener(lambda request:requests.append(request) or success())
        client.image_opener=Opener(lambda request:requests.append(request) or Response())
        cookie='DedeUserID=123; SESSDATA=FIXTURE'
        client._request(API,cookie,interval=0)
        self.launch(lambda:client._request(API,cookie,stop,60))
        self.assertTrue(stop.waiting.wait(2))
        self.assertEqual(client._request(IMAGE,'SESSDATA=SHOULD_NOT_LEAVE',binary=True),b'fixture')
        self.assertEqual(len(requests),2)
        self.assertFalse(any(key.lower()=='cookie' for key in requests[-1].headers))
        stop.set()
        self.threads[0].join(2)
        self.assertEqual([type(error) for error in self.errors],[InterruptedError])
        self.assertEqual(len(requests),2)

    def test_waiting_account_does_not_block_another_account(self):
        client=RequestTransport();stop=self.stop_event();requests=[];other_opened=threading.Event()
        first='DedeUserID=123; SESSDATA=FIRST';second='DedeUserID=456; SESSDATA=SECOND'
        def open_request(request):
            requests.append(request)
            if request.get_header('Cookie')==second:other_opened.set()
            return success()
        client.opener=Opener(open_request)
        client._request(API,first,interval=0)
        self.launch(lambda:client._request(API,first,stop,60))
        self.assertTrue(stop.waiting.wait(2),'first account did not enter its paced wait')
        self.launch(lambda:client._request(API,second,stop,.06))
        self.assertTrue(other_opened.wait(1),'one account pacing lock blocked another account')
        self.assertEqual(len(requests),2)
        stop.set()
        for thread in self.threads:thread.join(2)
        self.assertEqual([type(error) for error in self.errors],[InterruptedError])

    def test_changed_cookie_for_same_account_still_waits_in_original_lane(self):
        client=RequestTransport();stop=self.stop_event();requests=[]
        first_opened=threading.Event();second_opened=threading.Event()
        def open_request(request):
            requests.append(request)
            (first_opened if len(requests)==1 else second_opened).set()
            return success(self.block if len(requests)==1 else None)
        client.opener=Opener(open_request)
        self.launch(lambda:client._request(API,'DedeUserID=123; SESSDATA=OLD; buvid3=one',stop,0))
        self.assertTrue(first_opened.wait(2))
        self.launch(lambda:client._request(API,'buvid3=two; SESSDATA=NEW; DedeUserID=123',stop,0))
        self.assertFalse(second_opened.wait(.1),'new session for the same UID incorrectly bypassed its active lane')
        self.release.set();self.assertTrue(second_opened.wait(2))
        for thread in self.threads:thread.join(2)
        self.assertEqual(self.errors,[])

    def test_an_account_network_response_does_not_block_another_account(self):
        client=RequestTransport();stop=self.stop_event();first_opened=threading.Event();other_opened=threading.Event()
        def open_request(request):
            if request.get_header('Cookie')=='DedeUserID=123; SESSDATA=FIRST':
                first_opened.set();return success(self.block)
            other_opened.set();return success()
        client.opener=Opener(open_request)
        self.launch(lambda:client._request(API,'DedeUserID=123; SESSDATA=FIRST',stop,.06))
        self.assertTrue(first_opened.wait(2))
        self.launch(lambda:client._request(API,'DedeUserID=456; SESSDATA=SECOND',stop,.06))
        self.assertTrue(other_opened.wait(1),'another account could not send while the first network response remained open')
        self.assertFalse(self.release.is_set())
        self.release.set()
        for thread in self.threads:thread.join(2)
        self.assertEqual(self.errors,[])

    def test_stop_cancels_a_same_account_lock_waiter_without_sending_it(self):
        client=RequestTransport();stop=self.stop_event();opened=threading.Event();requests=[]
        second_started=threading.Event();second_finished=threading.Event()
        def open_request(request):
            requests.append(request);opened.set();return success(self.block)
        client.opener=Opener(open_request)
        cookie='DedeUserID=123; SESSDATA=FIRST'
        first=self.launch(lambda:client._request(API,cookie,stop,0))
        self.assertTrue(opened.wait(2))
        def queued():
            second_started.set()
            try:client._request(API+'?queued=1',cookie,stop,0)
            finally:second_finished.set()
        second=self.launch(queued)
        self.assertTrue(second_started.wait(1))
        self.assertFalse(second_finished.wait(.15),'second same-account task did not wait for the active response')
        stop.set();second.join(1)
        self.assertFalse(second.is_alive(),'same-account lane acquisition ignored the shared stop signal')
        self.assertEqual(len(requests),1)
        self.release.set();first.join(2)
        self.assertEqual([type(error) for error in self.errors],[InterruptedError,InterruptedError])

    def test_anonymous_api_lane_is_shared_but_independent_from_authenticated_accounts(self):
        client=RequestTransport();stop=self.stop_event();requests=[];authenticated=threading.Event()
        def open_request(request):
            requests.append(request)
            if request.get_header('Cookie'):authenticated.set()
            return success()
        client.opener=Opener(open_request)
        client._request(API,interval=0)
        self.launch(lambda:client._request(API+'?anonymous=2',stop=stop,interval=60))
        self.assertTrue(stop.waiting.wait(2),'anonymous requests must share one paced lane')
        self.launch(lambda:client._request(API,'DedeUserID=123; SESSDATA=FIRST',stop,.06))
        self.assertTrue(authenticated.wait(1),'anonymous API lane blocked an authenticated account')
        self.assertEqual(len(requests),2)
        stop.set()
        for thread in self.threads:thread.join(2)
        self.assertEqual([type(error) for error in self.errors],[InterruptedError])

    def test_image_requests_never_reserve_any_api_lane(self):
        client=RequestTransport();requests=[]
        def forbidden(*args,**kwargs):raise AssertionError('image attempted to enter API pacing')
        client._reserve_api_start=forbidden
        client.image_opener=Opener(lambda request:requests.append(request) or Response())
        for cookie in ('','DedeUserID=123; SESSDATA=FIXTURE'):
            self.assertEqual(client._request(IMAGE,cookie,interval=60,binary=True),b'fixture')
        self.assertEqual(len(requests),2)
        self.assertTrue(all(not any(k.lower()=='cookie' for k in r.headers) for r in requests))

    def test_request_carries_only_opaque_stable_lane_key(self):
        client=RequestTransport();requests=[]
        client.opener=Opener(lambda request:requests.append(request) or success())
        cookies=('DedeUserID=123; SESSDATA=FIRST','SESSDATA=SECOND; DedeUserID=123',
                 'DedeUserID=456; SESSDATA=THIRD','')
        for cookie in cookies:client._request(API,cookie,interval=0)
        keys=[request._bili_lane_key for request in requests]
        self.assertEqual(keys[0],keys[1]);self.assertNotEqual(keys[0],keys[2])
        self.assertNotIn(keys[3],keys[:3])
        for key in keys:self.assertRegex(key,r'^[a-f0-9]{64}$')
        for cookie in cookies[:-1]:self.assertNotIn(cookie,repr(client.__dict__))

    def test_redirect_hop_waits_a_full_interval_inside_the_original_account_turn(self):
        client=RequestTransport();stop=self.stop_event();starts=[];keys=[]
        handler=_SafeRedirectHandler(reserve=client._reserve_api_start)
        def open_request(request):
            starts.append(time.perf_counter())
            redirected=handler.redirect_request(request,None,302,'fixture',{},API+'?redirected=1')
            starts.append(time.perf_counter())
            keys.extend([request._bili_lane_key,redirected._bili_lane_key])
            return success()
        client.opener=Opener(open_request)
        started=time.perf_counter()
        client._request(API,'DedeUserID=123; SESSDATA=FIRST',stop,.06)
        self.assertGreaterEqual(starts[0]-started,.05)
        self.assertGreaterEqual(starts[1]-starts[0],.05,'redirect hop skipped its additional full pre-request wait')
        self.assertEqual(keys[0],keys[1])

    def test_image_lane_has_a_bound_and_waiting_download_can_be_cancelled(self):
        class TwoImages(RequestTransport):IMAGE_CONCURRENCY=2
        client=TwoImages();stop=threading.Event();two_started=threading.Event();starts=[]
        def open_image(request):
            starts.append(request)
            if len(starts)==2:two_started.set()
            return Response(before_read=self.block)
        client.image_opener=Opener(open_image)
        self.launch(lambda:client._request(IMAGE,binary=True))
        self.launch(lambda:client._request(IMAGE,binary=True))
        self.assertTrue(two_started.wait(2))
        waiting=self.launch(lambda:client._request(IMAGE,stop=stop,binary=True))
        stop.set();waiting.join(2)
        self.assertEqual(len(starts),2)
        self.assertEqual([type(error) for error in self.errors],[InterruptedError])

    def test_api_cookie_is_retained_for_authenticated_requests(self):
        client=RequestTransport();requests=[]
        client.opener=Opener(lambda request:requests.append(request) or success())
        client._request(API,'SESSDATA=FIXTURE',interval=0)
        self.assertEqual(requests[0].get_header('Cookie'),'SESSDATA=FIXTURE')

    def test_all_platform_risk_codes_stop_the_round_with_cooldown(self):
        for code in (-352,-412,127306,-799,-509):
            with self.subTest(code=code):
                client=RequestTransport();stop=threading.Event()
                client.opener=Opener(lambda request:Response(json.dumps({'code':code}).encode()))
                with self.assertRaises(CollectionError) as caught:
                    client._request(API,stop=stop,interval=0)
                self.assertEqual(caught.exception.code,str(code))
                self.assertTrue(caught.exception.stop_all)
                self.assertGreaterEqual(caught.exception.cooldown_seconds,300)
                self.assertTrue(stop.is_set())

    def test_http_risk_honors_retry_after_without_retry_or_cookie_rotation(self):
        for binary,code in ((False,412),(False,429),(True,429)):
            with self.subTest(binary=binary,code=code):
                client=RequestTransport();stop=threading.Event();requests=[]
                def restricted(request):
                    requests.append(request)
                    raise urllib.error.HTTPError(request.full_url,code,'fixture',{'Retry-After':'900'},None)
                if binary:client.image_opener=Opener(restricted)
                else:client.opener=Opener(restricted)
                with self.assertRaises(CollectionError) as caught:
                    client._request(IMAGE if binary else API,'SESSDATA=FIXTURE',stop,0,binary)
                self.assertEqual(caught.exception.cooldown_seconds,900)
                self.assertTrue(stop.is_set())
                self.assertEqual(len(requests),1)

    def test_risk_cancels_an_api_waiter_before_open(self):
        client=RequestTransport();stop=self.stop_event();requests=[]
        client.opener=Opener(lambda request:requests.append(request) or success())
        cookie='DedeUserID=123; SESSDATA=SECOND'
        client._request(API,cookie,interval=0)
        thread=self.launch(lambda:client._request(API,cookie,stop,60))
        self.assertTrue(stop.waiting.wait(2))
        stop.set();thread.join(2)
        self.assertEqual(len(requests),1)
        self.assertEqual([type(error) for error in self.errors],[InterruptedError])

    def test_first_request_wait_can_be_cancelled_and_releases_the_account_lane(self):
        client=RequestTransport();stop=self.stop_event();requests=[];opened=threading.Event()
        def open_request(request):
            requests.append(request);opened.set();return success()
        client.opener=Opener(open_request)
        cookie='DedeUserID=123; SESSDATA=FIRST'
        thread=self.launch(lambda:client._request(API,cookie,stop,60))
        self.assertTrue(stop.waiting.wait(2),'first request did not enter its configured wait')
        self.assertEqual(requests,[])
        stop.set();thread.join(2)
        self.assertEqual([type(error) for error in self.errors],[InterruptedError])
        stop.clear()
        thread=self.launch(lambda:client._request(API,cookie,stop,0))
        self.assertTrue(opened.wait(1),'cancelling the initial wait leaked the account lane lock')
        thread.join(2);self.assertEqual(len(requests),1)
        self.assertEqual([type(error) for error in self.errors],[InterruptedError])

    def test_http_error_releases_account_lane_for_another_thread(self):
        client=RequestTransport();stop=self.stop_event();opened=threading.Event()
        cookie='DedeUserID=123; SESSDATA=FIRST'
        def fail(request):
            raise urllib.error.HTTPError(request.full_url,500,'fixture',{},None)
        client.opener=Opener(fail)
        with self.assertRaises(CollectionError):client._request(API,cookie,stop,0)
        client.opener=Opener(lambda request:opened.set() or success())
        thread=self.launch(lambda:client._request(API,cookie,stop,0))
        self.assertTrue(opened.wait(1),'HTTP failure left the account lock owned by the previous thread')
        thread.join(2);self.assertEqual(self.errors,[])

    def test_risk_response_cancels_waiters_in_all_account_lanes(self):
        client=RequestTransport();stop=self.stop_event();requests=[]
        first='DedeUserID=123; SESSDATA=FIRST';second='DedeUserID=456; SESSDATA=SECOND'
        def open_request(request):
            requests.append(request)
            return Response(json.dumps({'code':-412}).encode()) if 'risk=1' in request.full_url else success()
        client.opener=Opener(open_request)
        for cookie in (first,second):client._request(API,cookie,interval=0)
        for cookie in (first,second):self.launch(lambda cookie=cookie:client._request(API,cookie,stop,60))
        self.assertTrue(stop.two_waiting.wait(2),'both account lanes did not reach paced waits')
        with self.assertRaises(CollectionError) as caught:
            client._request(API+'?risk=1','DedeUserID=789; SESSDATA=THIRD',stop,0)
        self.assertTrue(caught.exception.stop_all)
        for thread in self.threads:thread.join(2)
        self.assertEqual(len(requests),3,'queued requests sent after another account received risk response')
        self.assertEqual([type(error) for error in self.errors],[InterruptedError,InterruptedError])

    def test_oversize_and_invalid_json_remain_errors(self):
        client=RequestTransport()
        for data,code in ((b'x'*10_000_001,'response_size'),(b'bad json','response_format'),
                          (b'[]','response_format')):
            client.opener=Opener(lambda request:Response(data))
            with self.assertRaises(CollectionError) as caught:client._request(API,interval=0)
            self.assertEqual(caught.exception.code,code)

    def test_bad_or_non_https_initial_urls_never_open(self):
        client=RequestTransport();requests=[]
        client.opener=client.image_opener=Opener(lambda request:requests.append(request) or success())
        for url,binary in (('http://api.bilibili.com/x',False),('https://api.bilibili.com:8443/x',False),
                           ('https://api.bilibili.com:0/x',False),('https://evil.test/x',True),
                           ('https://user:password@i0.hdslb.com/x',True)):
            with self.subTest(url=url),self.assertRaises(CollectionError):
                client._request(url,'SESSDATA=FIXTURE',interval=0,binary=binary)
        self.assertEqual(requests,[])


class LaneKeyTests(unittest.TestCase):
    def test_account_uid_dominates_cookie_order_and_session_changes(self):
        cookies=('DedeUserID=123; SESSDATA=FIRST; buvid3=ONE',
                 'buvid3=TWO; SESSDATA=SECOND; DedeUserID=123',
                 ' DedeUserID = 123 ; sid=THIRD ')
        keys=[_api_lane_key(cookie) for cookie in cookies]
        self.assertEqual(len(set(keys)),1)
        self.assertNotEqual(keys[0],_api_lane_key('DedeUserID=456; SESSDATA=FIRST'))

    def test_session_fallback_ignores_non_identity_cookie_fields(self):
        first=_api_lane_key('SESSDATA=SESSION%2CIDENTIFIER; buvid3=ONE')
        second=_api_lane_key('sid=CHANGED; buvid3=TWO; SESSDATA=SESSION%2CIDENTIFIER')
        self.assertEqual(first,second)
        self.assertNotEqual(first,_api_lane_key('SESSDATA=OTHER%2CSESSION; buvid3=ONE'))
        self.assertEqual(first,_api_lane_key('DedeUserID=invalid; SESSDATA=SESSION%2CIDENTIFIER'))

    def test_fallback_and_anonymous_keys_are_stable_opaque_and_distinct(self):
        values=('', 'buvid3=FIRST; sid=ONE', 'buvid3=SECOND; sid=TWO',
                'DedeUserID=123; SESSDATA=PRIVATE', 'SESSDATA=123')
        keys=[_api_lane_key(value) for value in values]
        self.assertEqual(len(set(keys)),len(values))
        for value,key in zip(values,keys):
            self.assertEqual(key,_api_lane_key(value))
            self.assertRegex(key,r'^[a-f0-9]{64}$')
            if value:self.assertNotIn(value,key)


class RedirectTests(unittest.TestCase):
    def redirect(self,handler,request,target):
        return handler.redirect_request(request,None,302,'fixture',{},target)

    def test_api_redirect_only_allows_same_https_origin_and_keeps_pacing(self):
        reservations=[];stop=threading.Event()
        handler=_SafeRedirectHandler(reserve=lambda event,interval,lane_key=None:reservations.append((event,interval,lane_key)))
        request=urllib.request.Request(API,headers={'Cookie':'SESSDATA=FIXTURE'})
        key=_api_lane_key('SESSDATA=FIXTURE')
        request._bili_stop=stop;request._bili_interval=1.75;request._bili_lane_key=key
        redirected=self.redirect(handler,request,API+'?redirected=1')
        self.assertEqual(redirected.get_header('Cookie'),'SESSDATA=FIXTURE')
        self.assertEqual(reservations,[(stop,1.75,key)])
        self.assertIs(redirected._bili_stop,stop)
        self.assertEqual(redirected._bili_lane_key,key)
        second_hop=self.redirect(handler,redirected,API+'?redirected=2')
        self.assertEqual(second_hop._bili_lane_key,key)
        self.assertEqual(reservations,[(stop,1.75,key)]*2)
        for target in ('https://i0.hdslb.com/x','https://evil.test/x','http://api.bilibili.com/x',
                       'https://api.bilibili.com:8443/x','https://api.bilibili.com.evil.test/x'):
            with self.subTest(target=target),self.assertRaises(CollectionError):
                self.redirect(handler,request,target)
        self.assertEqual(len(reservations),2)

    def test_api_redirect_without_attached_lane_derives_it_from_cookie(self):
        reservations=[]
        handler=_SafeRedirectHandler(reserve=lambda stop,interval,lane_key=None:reservations.append(lane_key))
        request=urllib.request.Request(API,headers={'Cookie':'DedeUserID=123; SESSDATA=FIXTURE'})
        redirected=self.redirect(handler,request,API+'?redirected=1')
        expected=_api_lane_key('DedeUserID=123; SESSDATA=CHANGED')
        self.assertEqual(reservations,[expected])
        self.assertEqual(redirected._bili_lane_key,expected)
        self.assertNotIn('FIXTURE',redirected._bili_lane_key)

    def test_image_redirect_strips_credentials_and_restricts_all_hops(self):
        def forbidden(*args):raise AssertionError('CDN redirect attempted to reserve an API lane')
        handler=_SafeRedirectHandler(binary=True,reserve=forbidden)
        request=urllib.request.Request(IMAGE,headers={'Cookie':'SESSDATA=FIXTURE','Cookie2':'FIXTURE',
                        'Authorization':'fixture','Proxy-Authorization':'fixture','Accept':'image/*'})
        redirected=self.redirect(handler,request,'https://i1.hdslb.com/bfs/fixture.jpg')
        self.assertEqual(redirected.get_header('Accept'),'image/*')
        self.assertFalse(any(key.lower() in ('cookie','cookie2','authorization','proxy-authorization')
                             for key in redirected.headers))
        for target in ('https://api.bilibili.com/x','https://evil.test/x','http://i1.hdslb.com/x',
                       'https://i1.hdslb.com:8443/x','https://i3.hdslb.com/x'):
            with self.subTest(target=target),self.assertRaises(CollectionError):
                self.redirect(handler,redirected,target)

    def test_retry_after_dates_and_invalid_values(self):
        future=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=901)
        date=email.utils.format_datetime(future,usegmt=True)
        self.assertGreaterEqual(_retry_after({'Retry-After':date}),900)
        for value in ('0','-1','nonsense','nan','inf'):
            with self.subTest(value=value):self.assertEqual(_retry_after({'Retry-After':value}),300)


if __name__=='__main__':unittest.main()
