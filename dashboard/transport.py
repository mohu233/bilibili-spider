"""Paced Bilibili API traffic and an independent, credential-free image lane."""
import datetime
from contextlib import contextmanager
import email.utils
import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


IMAGE_HOSTS=frozenset(('i0.hdslb.com','i1.hdslb.com','i2.hdslb.com'))
RISK_CODES=(-352,-412,127306,-799,-509)
DEFAULT_RISK_COOLDOWN=300
MAX_RESPONSE_BYTES=10_000_000


def _api_lane_key(cookie):
    """Keep one opaque pacing identity per account, without retaining credentials."""
    values={}
    for part in str(cookie or '').split(';'):
        name,separator,value=part.strip().partition('=')
        if separator:values[name.strip()]=value.strip()
    account=values.get('DedeUserID','')
    if 0<len(account)<=19 and account.isascii() and account.isdigit() and int(account)>0:
        identity='account:'+str(int(account))
    elif values.get('SESSDATA'):
        identity='session:'+values['SESSDATA']
    elif cookie:
        identity='cookie:'+str(cookie)
    else:
        identity='anonymous-api'
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


class CollectionError(Exception):
    def __init__(self,message,code='network',stop_all=False,cooldown_seconds=None):
        super().__init__(message)
        self.code=code
        self.stop_all=stop_all
        self.cooldown_seconds=(DEFAULT_RISK_COOLDOWN if stop_all and code!='auth' else 0
                               ) if cooldown_seconds is None else cooldown_seconds


def _origin(url):
    try:
        parts=urllib.parse.urlsplit(url)
        if parts.username is not None or parts.password is not None:return None
        return parts.scheme.lower(),parts.hostname,parts.port if parts.port is not None else 443
    except (ValueError,TypeError,AttributeError):return None


def _valid_url(url,binary):
    origin=_origin(url)
    return bool(origin and origin[0]=='https' and origin[2]==443 and
                origin[1] in (IMAGE_HOSTS if binary else ('api.bilibili.com',)))


def _retry_after(headers):
    """Read either RFC Retry-After form without shortening the risk cooldown."""
    value=headers.get('Retry-After') if headers else None
    if not value:return DEFAULT_RISK_COOLDOWN
    try:
        seconds=float(value)
        if not math.isfinite(seconds):raise ValueError()
    except (ValueError,TypeError):
        try:
            target=email.utils.parsedate_to_datetime(value)
            if target.tzinfo is None:target=target.replace(tzinfo=datetime.timezone.utc)
            seconds=(target-datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        except (ValueError,TypeError,OverflowError):return DEFAULT_RISK_COOLDOWN
    return max(DEFAULT_RISK_COOLDOWN,math.ceil(seconds))


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self,binary=False,reserve=None):
        self.binary=binary
        self.reserve=reserve

    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if (not _valid_url(newurl,self.binary) or
                (not self.binary and _origin(req.full_url)!=_origin(newurl))):
            raise CollectionError('平台重定向地址不受支持，已停止本次请求','redirect_url')
        redirected=super().redirect_request(req,fp,code,msg,headers,newurl)
        if redirected is None:return None
        if self.binary:
            # Never forward credentials to a CDN, including cross-CDN redirects.
            for key in list(redirected.headers)+list(redirected.unredirected_hdrs):
                if key.lower() in ('cookie','cookie2','authorization','proxy-authorization'):
                    redirected.remove_header(key)
        else:
            redirected._bili_stop=getattr(req,'_bili_stop',None)
            redirected._bili_interval=getattr(req,'_bili_interval',1.5)
            redirected._bili_lane_key=getattr(req,'_bili_lane_key',
                                             _api_lane_key(req.get_header('Cookie','')))
            if self.reserve:self.reserve(redirected._bili_stop,redirected._bili_interval,
                                         redirected._bili_lane_key)
        return redirected


class RequestTransport:
    """Wait before each API call; different accounts use independent serial lanes.

    Images share only cancellation and risk handling with API traffic. Their
    semaphore bounds sockets; they never use API credentials or its limiter.
    """
    IMAGE_CONCURRENCY=4

    def __init__(self):
        self.api_lanes_lock=threading.Lock()
        self.api_lanes={}
        self.image_slots=threading.BoundedSemaphore(self.IMAGE_CONCURRENCY)
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    _SafeRedirectHandler(reserve=self._reserve_api_start))
        self.image_opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    _SafeRedirectHandler(binary=True))

    @staticmethod
    def _check_stop(stop):
        if stop and stop.is_set():raise InterruptedError()

    @contextmanager
    def _api_turn(self,stop,lane_key=None):
        lane_key=lane_key or _api_lane_key('')
        self._check_stop(stop)
        with self.api_lanes_lock:
            lane=self.api_lanes.setdefault(lane_key,threading.RLock())
        while not lane.acquire(timeout=.1):self._check_stop(stop)
        try:
            self._check_stop(stop)
            yield
        finally:lane.release()

    def _reserve_api_start(self,stop,interval,lane_key=None):
        # A complete delay precedes every request, including the first call and
        # API redirect hops. HTTP response time does not consume this delay.
        with self._api_turn(stop,lane_key):
            if stop:
                if stop.wait(interval):raise InterruptedError()
            else:time.sleep(interval)
            self._check_stop(stop)

    @contextmanager
    def _request_slot(self,stop,interval,binary,lane_key=None):
        if binary:
            self._acquire_image_slot(stop)
            try:yield
            finally:self.image_slots.release()
        else:
            # Keep this account's lane until its response is handled. Other
            # accounts remain independent; redirect reservations can re-enter.
            with self._api_turn(stop,lane_key):
                self._reserve_api_start(stop,interval,lane_key)
                yield

    def _acquire_image_slot(self,stop):
        self._check_stop(stop)
        while not self.image_slots.acquire(timeout=.1):self._check_stop(stop)
        try:self._check_stop(stop)
        except BaseException:
            self.image_slots.release()
            raise

    def _request(self,url,cookie='',stop=None,interval=1.5,binary=False):
        if not _valid_url(url,binary):
            raise CollectionError('请求地址不是受支持的 B 站 HTTPS 地址','request_url')
        try:
            interval=float(interval)
            if not math.isfinite(interval) or interval<0:raise ValueError()
        except (ValueError,TypeError):
            raise CollectionError('请求间隔无效','request_interval') from None
        headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/',
                 'Accept':'image/*' if binary else 'application/json','Cache-Control':'no-cache'}
        if cookie and not binary:headers['Cookie']=cookie
        request=urllib.request.Request(url,headers=headers)
        request._bili_stop=stop
        request._bili_interval=interval
        if not binary:request._bili_lane_key=_api_lane_key(cookie)
        with self._request_slot(stop,interval,binary,getattr(request,'_bili_lane_key',None)):
            self._check_stop(stop)
            opener=self.image_opener if binary else self.opener
            try:
                with opener.open(request,timeout=20) as response:
                    # Redirect handlers enforce this before opening; recheck the
                    # final response as a boundary guard for custom openers.
                    final_url=response.geturl() if hasattr(response,'geturl') else url
                    if (not _valid_url(final_url,binary) or
                            (not binary and _origin(final_url)!=_origin(url))):
                        raise CollectionError('平台响应地址不受支持','redirect_url')
                    raw=response.read(MAX_RESPONSE_BYTES+1)
                if len(raw)>MAX_RESPONSE_BYTES:
                    raise CollectionError('响应超过大小限制','response_size')
                if binary:
                    self._check_stop(stop)
                    return raw
                data=json.loads(raw)
            except urllib.error.HTTPError as error:
                cooldown=_retry_after(error.headers)
                error.close()
                if error.code in (412,429):
                    if stop:stop.set()
                    raise CollectionError('平台访问受限，已暂停整轮采集',str(error.code),True,
                                          cooldown) from None
                raise CollectionError(f'HTTP 请求失败（{error.code}）','http') from None
            except InterruptedError:raise
            except (urllib.error.URLError,TimeoutError,OSError):
                raise CollectionError('网络连接失败或超时，进度已保留','network') from None
            except (ValueError,UnicodeError):
                raise CollectionError('平台响应格式异常','response_format') from None
            if not isinstance(data,dict):
                raise CollectionError('平台响应格式异常','response_format')
            code=data.get('code')
            if code in RISK_CODES:
                if stop:stop.set()
                raise CollectionError('平台风控提示，已暂停整轮采集并冷却至少 5 分钟',str(code),True)
            if code==-101:
                if stop:stop.set()
                raise CollectionError('Cookie 已失效，请更新账号凭据','auth',True)
            if code!=0:raise CollectionError(f'平台拒绝本次请求（code={code}）',str(code))
            self._check_stop(stop)
            return data.get('data')
