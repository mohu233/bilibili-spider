"""Resumable, optionally continuous collection jobs for the local dashboard."""
import importlib.util
from concurrent.futures import FIRST_COMPLETED,ThreadPoolExecutor,wait
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .store import MAX_WORKERS, later, now, uid
from .transport import CollectionError, RequestTransport


class BiliClient(RequestTransport):
    WBI_KEY_POSITIONS=(46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
                       27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
                       37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
                       22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52)

    def __init__(self):
        super().__init__()
        self.wbi_key=''
        self.wbi_expires_at=0
        self.wbi_lock=threading.Lock()

    def validate(self,cookie):
        data=self._request('https://api.bilibili.com/x/web-interface/nav',cookie)
        if not isinstance(data,dict) or not data.get('isLogin'):
            raise CollectionError('Cookie 未登录','auth',True)
        return data.get('uname','已登录账号')

    def profile(self,user,cookie,stop,interval):
        url='https://api.bilibili.com/x/web-interface/card?'+urllib.parse.urlencode({'mid':user,'photo':0})
        data=self._request(url,cookie,stop,interval)
        if not isinstance(data,dict) or not isinstance(data.get('card'),dict):
            raise CollectionError('用户资料字段不完整','profile_format')
        card=data['card']
        try:
            if uid(card.get('mid',''))!=user:raise ValueError()
        except ValueError:raise CollectionError('用户资料 UID 不一致或无效','profile_mismatch') from None
        return {'name':str(card.get('name') or '')[:200],'intro':str(card.get('sign') or '')[:20000],
                'avatar_url':str(card.get('face') or ''),'fan_count':card.get('fans'),
                'follow_count':card.get('attention')}

    def relation(self,user,direction,page,cookie,stop,interval):
        url='https://api.bilibili.com/x/relation/'+direction+'?'+urllib.parse.urlencode(
            {'vmid':user,'pn':page,'ps':50,'order':'desc'})
        data=self._request(url,cookie,stop,interval)
        if not isinstance(data,dict) or not isinstance(data.get('list'),list):
            raise CollectionError('列表不可访问或格式不完整，未标记完成','relation_unavailable')
        items=data['list']
        try:
            values=[uid(x['mid']) for x in items]
        except (ValueError,TypeError,KeyError):raise CollectionError('关系列表含无效 UID，本页待重试','relation_format') from None
        return values,len(items)<50

    def _wbi_query(self,params,cookie,stop,interval):
        # Reuse the project's ordinary WBI signing algorithm, but keep requests
        # under this client's interval, cancellation and stop-on-risk rules.
        with self.wbi_lock:
            if not self.wbi_key or time.monotonic()>=self.wbi_expires_at:
                nav=self._request('https://api.bilibili.com/x/web-interface/nav',cookie,stop,interval)
                try:
                    images=nav['wbi_img']
                    parts=[urllib.parse.urlsplit(images[key]).path.rsplit('/',1)[-1].split('.')[0]
                           for key in ('img_url','sub_url')]
                    if any(not re.fullmatch(r'[a-fA-F0-9]{32}',part) for part in parts):raise ValueError()
                    raw=''.join(parts)
                    self.wbi_key=''.join(raw[position] for position in self.WBI_KEY_POSITIONS)[:32]
                    self.wbi_expires_at=time.monotonic()+12*3600
                except (KeyError,TypeError,ValueError,AttributeError):
                    raise CollectionError('近期视频签名信息不完整，保留待审核','video_signature') from None
        values={key:''.join(char for char in str(value) if char not in "!'()*")
                for key,value in dict(params,wts=int(time.time())).items()}
        query=urllib.parse.urlencode(sorted(values.items()),quote_via=urllib.parse.quote,safe='')
        signature=hashlib.md5((query+self.wbi_key).encode('utf-8')).hexdigest()
        return query+'&w_rid='+signature

    def recent_video_tags(self,user,cookie,stop,interval,stop_on_strong=False):
        query=self._wbi_query({'mid':user,'pn':1,'ps':3,'order':'pubdate',
                               'platform':'web','web_location':1550101},cookie,stop,interval)
        data=self._request('https://api.bilibili.com/x/space/wbi/arc/search?'+query,cookie,stop,interval)
        if not isinstance(data,dict) or not isinstance(data.get('list'),dict) or not isinstance(data['list'].get('vlist'),list):
            raise CollectionError('近期视频列表不可访问或格式不完整，保留待审核','video_unavailable')
        # The list response already contains every sampled title. Inspect them
        # together before spending any extra request on individual video tags.
        if stop_on_strong:
            titles=[]
            for item in data['list']['vlist'][:3]:
                if not isinstance(item,dict) or not isinstance(item.get('title'),str) or not re.fullmatch(r'BV[a-zA-Z0-9]{10}',str(item.get('bvid',''))):
                    raise CollectionError('近期视频标识或标题不完整，保留待审核','video_format')
                titles.append({'bvid':item['bvid'],'title':item['title'][:500],
                               'tags':[],'tags_success':False})
            if any(text_evidence(video['title'])['strong_keywords'] for video in titles):
                for video in titles:
                    video.update(tags_skipped=True,tags_skip_reason='采样标题已明确命中，快速判定省略标签请求')
                return {'videos':titles,'sample_size':len(titles),'success':False,'skipped':True,
                        'skip_reason':'视频标题已明确命中，省略标签检查'}
        videos=[]
        for item in data['list']['vlist'][:3]:
            if stop and stop.is_set():raise InterruptedError()
            if not isinstance(item,dict) or not isinstance(item.get('title'),str) or not re.fullmatch(r'BV[a-zA-Z0-9]{10}',str(item.get('bvid',''))):
                error=CollectionError('近期视频标识或标题不完整，保留待审核','video_format')
                error.video_items=videos
                raise error
            video={'bvid':item['bvid'],'title':item['title'][:500],'tags':[],'tags_success':False}
            videos.append(video)
            try:
                tags=self._request('https://api.bilibili.com/x/tag/archive/tags?'+urllib.parse.urlencode({'bvid':item['bvid']}),cookie,stop,interval)
                if not isinstance(tags,list) or any(not isinstance(tag,dict) or not isinstance(tag.get('tag_name'),str) for tag in tags):
                    raise CollectionError('近期视频标签不可访问或格式不完整，保留待审核','video_tags_unavailable')
                video.update(tags=list(dict.fromkeys(tag['tag_name'][:200] for tag in tags))[:100],tags_success=True)
                if stop_on_strong and any(text_evidence(tag,exact_tag=True)['strong_keywords'] for tag in video['tags']):
                    if len(videos)<min(3,len(data['list']['vlist'])):
                        return {'videos':videos,'sample_size':len(videos),'success':False,'skipped':True,
                                'skip_reason':'视频标签已明确命中，省略其余视频检查'}
            except CollectionError as e:
                e.video_items=videos
                raise
        return {'videos':videos,'sample_size':len(videos),'success':True}

    def download(self,url,path,stop,interval):
        parts=urllib.parse.urlsplit(url)
        host=parts.hostname
        # Some public cards still return http:// CDN URLs. Upgrade only the
        # known image hosts; the request transport itself remains HTTPS-only.
        if (parts.scheme=='http' and host in ('i0.hdslb.com','i1.hdslb.com','i2.hdslb.com')
                and parts.port in (None,80) and parts.username is None and parts.password is None):
            url=urllib.parse.urlunsplit(parts._replace(scheme='https',netloc=host))
        if host not in ('i0.hdslb.com','i1.hdslb.com','i2.hdslb.com') or not url.startswith('https://'):
            raise CollectionError('头像地址不是受支持的 B 站 HTTPS 图片地址','avatar_url')
        raw=self._request(url,stop=stop,interval=interval,binary=True)
        # Validate content before it is exposed as a local image.
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:image.verify()
        except ImportError:raise CollectionError('头像校验需要 Pillow，请使用启动脚本选择图像环境','image_dependency') from None
        except Exception:raise CollectionError('头像文件不可解码，未执行模型识别','avatar_invalid') from None
        path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.tmp')
        temp.write_bytes(raw);temp.replace(path)


class Classifier:
    def __init__(self,root):
        self.root=Path(root)
        self.sessions={}

    def model_paths(self):
        paths={}
        for name in ('mobilenetv3_v0.1_dist','caformer_s36_v0.1'):
            candidates=[self.root/'models'/name]
            candidates+=sorted((self.root/'data').glob('avatar_benchmark_*/models/'+name),reverse=True)
            path=next((p for p in candidates if (p/'model.onnx').is_file() and (p/'meta.json').is_file()),None)
            paths[name]=path
        paths['yolo']=self.root/'models'/'furry1500x200.pt' if (self.root/'models'/'furry1500x200.pt').is_file() else None
        return paths

    def availability(self):
        paths=self.model_paths()
        return {name:{'weights':bool(path),'runtime':bool(importlib.util.find_spec('ultralytics') if name=='yolo'
                                 else importlib.util.find_spec('onnxruntime'))} for name,path in paths.items()}

    def predict(self,path,model_name):
        model_path=self.model_paths().get(model_name)
        if model_path is None:raise CollectionError('所选模型权重不存在，请选择已下载模型','model_missing')
        if model_name=='yolo':
            try:
                from ultralytics import YOLO
                if model_name not in self.sessions:self.sessions[model_name]=YOLO(str(model_path))
                results=self.sessions[model_name].predict(str(path),verbose=False)
                boxes=results[0].boxes
                if boxes is None:raise ValueError()
                scores=[float(conf) for cls,conf in zip(boxes.cls.tolist(),boxes.conf.tolist()) if int(cls) in (0,1)]
                return {'furry_score':max(scores,default=0.),'model':'yolo','model_path':str(model_path.relative_to(self.root))}
            except ImportError:raise CollectionError('YOLO 运行依赖未安装，请选择 DeepGHS 模型或安装项目依赖','model_dependency') from None
            except Exception:raise CollectionError('YOLO 模型推理失败，未作为非 Furry 处理','model_inference') from None
        try:
            import numpy as np
            import onnxruntime as ort
            from PIL import Image,ImageOps
        except ImportError:raise CollectionError('模型运行依赖未安装，请按 PANEL.md 配置环境','model_dependency') from None
        try:
            if model_name not in self.sessions:
                options=ort.SessionOptions();options.intra_op_num_threads=4
                self.sessions[model_name]=ort.InferenceSession(str(model_path/'model.onnx'),options,providers=['CPUExecutionProvider'])
            session=self.sessions[model_name]
            _,_,height,width=session.get_inputs()[0].shape
            height=height if isinstance(height,int) else 384
            width=width if isinstance(width,int) else 384
            with Image.open(path) as image:
                image=ImageOps.exif_transpose(image).convert('RGBA')
                background=Image.new('RGBA',image.size,'white');background.alpha_composite(image)
                image=background.convert('RGB').resize((width,height),Image.Resampling.BILINEAR)
                data=((np.asarray(image,dtype=np.float32)/255.-.5)/.5).transpose(2,0,1)[None]
            outputs=[x.name for x in session.get_outputs()]
            values=session.run(outputs,{session.get_inputs()[0].name:data})[outputs.index('output')][0]
            labels=json.loads((model_path/'meta.json').read_text(encoding='utf-8'))['labels']
            score=float(values[labels.index('furry')])
            if not math_is_probability(score):raise ValueError()
            return {'furry_score':score,'model':model_name,'model_path':str(model_path.relative_to(self.root))}
        except Exception:raise CollectionError('分类模型推理失败，未作为非 Furry 处理','model_inference') from None


def math_is_probability(value):
    return 0<=value<=1


def text_evidence(text,exact_tag=False):
    """Keep explicit topic evidence separate from ambiguous and negated mentions."""
    text=str(text).casefold()[:20000]
    patterns={'furry':r'(?<![a-z0-9_])furr(?:y|ies)(?![a-z0-9_])',
              'fursuit':r'(?<![a-z0-9_])fursuit(?:s|er|ers|ing)?(?![a-z0-9_])',
              '兽装':'兽装','兽设':'兽设','福瑞':'福瑞','兽人':'兽人'}
    tags={'furry':'furry','furries':'furry','furry fandom':'furry','fursuit':'fursuit',
          'fursuiting':'fursuit','兽装':'兽装','兽设':'兽设','福瑞':'福瑞','兽人':'兽人'}
    exact=tags.get(text.strip(' #＃\t\r\n')) if exact_tag else None
    strong=[];weak=[];negated=[]
    for keyword,pattern in patterns.items():
        for match in re.finditer(pattern,text):
            prefix=re.split(r'[，,。.!！?？;；\n]|但是|不过|然而|而是|但',text[:match.start()])[-1][-24:]
            suffix=text[match.end():match.end()+10]
            denial=bool(re.search(r'(?:不是|并非|并不是|非|不算|不喜欢|不爱|不画|不搞|不做|不玩|不接|不属于|不涉及|拒绝|反对|讨厌|反感|远离|排斥|反).{0,6}$',prefix)
                        or re.search(r'\b(?:not(?:\s+(?:a|an|into|really|interested|in|fan|of))*|anti|no)[\s-]*$',prefix)
                        or re.match(r'\s*(?:不是|并非|不算|无关|勿扰|退散|禁止)',suffix))
            target=negated if denial else strong if not exact_tag or exact==keyword else weak
            if keyword not in target:target.append(keyword)
    for keyword in ('毛茸茸','毛毛','兽圈','anthro'):
        if keyword in text and keyword not in weak:weak.append(keyword)
    return {'strong_keywords':strong,'weak_keywords':weak,'negated_keywords':negated}


class Collector:
    IDLE_WAIT_SECONDS=5
    ROUND_WAIT_SECONDS=1

    def __init__(self,store,client=None,classifier=None):
        self.store=store
        self.client=client or BiliClient()
        self.classifier=classifier or Classifier(store.root)
        self.stop_event=threading.Event()
        self.lock=threading.Lock()
        self.account_lock=threading.RLock()
        self.reserved_accounts=set()
        self.workers_lock=threading.Lock()
        self.inference_lock=threading.Lock()
        self.worker_context=threading.local()
        self.worker_items={}
        self.requested_workers=1
        self.thread=None
        self.job_id=None

    def active(self):
        return bool(self.thread and self.thread.is_alive())

    def busy_account_ids(self):
        with self.account_lock:
            return set(self.reserved_accounts)

    def _claim_account(self,exclude=()):
        # Selection and reservation are one operation: deletion must not slip
        # between the coordinator selecting an account and its worker starting.
        with self.account_lock:
            ident=self.store.next_account(exclude=set(exclude)|self.reserved_accounts)
            if ident is not None:self.reserved_accounts.add(ident)
            return ident

    def _release_account(self,ident):
        with self.account_lock:
            self.reserved_accounts.discard(ident)

    def delete_account(self,ident):
        if not isinstance(ident,str):raise ValueError('账号不存在')
        with self.account_lock:
            if ident in self.reserved_accounts:
                raise ValueError('账号正在采集或验证，请先停用并等待当前任务结束后再删除')
            self.store.account_delete(ident)

    def workers_snapshot(self):
        configured=self.requested_workers if self.active() else self.store.get_config().get('worker_count',1)
        available=sum(bool(account['enabled'] and account['status']!='expired') for account in self.store.accounts())
        requested=min(MAX_WORKERS,available) if configured==0 else configured
        with self.workers_lock:
            items=[dict(item) for _,item in sorted(self.worker_items.items())]
        return {'requested':requested,'effective':min(requested,available),'active':len(items),'items':items,
                'mode':'auto' if configured==0 else 'manual','configured':configured,
                'available_accounts':available,'limit':MAX_WORKERS}

    def _worker_limit(self):
        # Refresh account availability while the job is running. Extra accounts
        # can fill new slots; disabling an account never interrupts its task.
        available=sum(bool(account['enabled'] and account['status']!='expired') for account in self.store.accounts())
        requested=MAX_WORKERS if self.requested_workers==0 else self.requested_workers
        return min(requested,available)

    def _phase(self,job,user,phase,**fields):
        slot=getattr(self.worker_context,'slot',None)
        with self.workers_lock:
            if slot in self.worker_items:self.worker_items[slot]['phase']=phase
        self.store.job_update(job,current_uid=user,phase=phase,**fields)

    def _execute_task(self,slot,user,account,config,job,kind):
        try:
            label=next((item['label'] for item in self.store.accounts() if item['id']==account),'')
            self.worker_context.slot=slot
            self.worker_context.kind=kind
            with self.workers_lock:
                self.worker_items[slot]={'slot':slot,'uid':user,'account_id':account,
                                         'account_label':label,'phase':'准备判定' if kind=='candidate' else '准备遍历',
                                         'kind':kind}
            if self.stop_event.is_set():raise InterruptedError()
            self._process(user,account,config,job)
        except CollectionError as error:
            if error.stop_all:self.stop_event.set()
            raise
        except InterruptedError:
            raise
        except Exception:
            self.stop_event.set()
            raise
        finally:
            with self.workers_lock:self.worker_items.pop(slot,None)
            if hasattr(self.worker_context,'slot'):del self.worker_context.slot
            if hasattr(self.worker_context,'kind'):del self.worker_context.kind
            self._release_account(account)

    def validate_account(self,ident):
        if not isinstance(ident,str):raise ValueError('账号不存在')
        with self.account_lock:
            if ident in self.reserved_accounts:
                raise ValueError('账号正在采集或验证，请先停用并等待当前任务结束后再验证')
            secret=self.store.account_secret(ident)
            self.reserved_accounts.add(ident)
        try:
            name=self.client.validate(secret)
            self.store.account_result(ident,'valid',name)
            return {'valid':True,'message':'账号验证成功'}
        except CollectionError as e:
            self.store.account_result(ident,'expired' if e.code=='auth' else 'error')
            if e.stop_all and e.code!='auth':
                self.stop_event.set()
                self.store.cooldown(getattr(e,'cooldown_seconds',300))
            return {'valid':False,'message':str(e)}
        finally:
            self._release_account(ident)

    def start(self):
        with self.lock:
            if self.active():raise ValueError('已有采集任务运行中')
            config=self.store.get_config()
            if config.get('cooldown_until','')>now():raise ValueError('平台冷却中，请等待冷却结束后再启动')
            if not any(a['enabled'] and a['status']!='expired' for a in self.store.accounts()):
                raise ValueError('请先在 Cookie 池添加并启用有效账号')
            if not config.get('continuous',False) and not self.store.next_seed(set()):
                raise ValueError('没有可执行的候选或种子；待审核或延后记录需要处理后再启动')
            availability=self.classifier.availability().get(config['model'],{})
            if not availability.get('weights') or not availability.get('runtime'):
                raise ValueError('所选模型权重或运行依赖不可用，请检查采集设置')
            self.stop_event.clear()
            self.requested_workers=config.get('worker_count',1)
            self.job_id=self.store.job_create(config)
            self.thread=threading.Thread(target=self._run,args=(self.job_id,config),daemon=True,name='panel-collector')
            self.thread.start()
            return self.job_id

    def stop(self):
        with self.lock:
            if not self.active():return
            # Commit the request before waking the worker; never overwrite a terminal job.
            with self.store.db() as c:
                c.execute("UPDATE jobs SET state='stopping',message=? WHERE id=? AND state='running'",
                          ('将在当前请求结束后暂停，分页进度保留',self.job_id))
            self.stop_event.set()

    def _process(self,user,account,config,job):
        row=self.store.profile(user)
        # A candidate claimed by the scheduler must never turn into an expansion
        # task when a manual decision is made while its evidence is being fetched.
        started_as_seed=bool(row['is_seed'] and row['verdict']=='yes'
                             and getattr(self.worker_context,'kind',None)!='candidate')
        cookie=self.store.account_secret(account)
        evidence=row['evidence']
        checked=(evidence.get('decision_policy')=='any_strong_v1'
                 and bool(evidence.get('automatic_reason'))
                 and all(isinstance(evidence.get(part),dict) for part in ('intro','videos','avatar')))
        if started_as_seed and checked:
            self._phase(job,user,'继续已确认种子的关系遍历')
            self._expand_seed(user,cookie,config,job)
            return
        self._phase(job,user,'获取用户资料')
        profile=self.client.profile(user,cookie,self.stop_event,config['request_interval'])
        updates=dict(profile,collected_at=now(),collection_status='fetched',last_error='')
        # The cached image belongs to the URL it was downloaded from. Clear the
        # binding before attempting a changed URL so a failed download cannot
        # make a later retry classify the previous avatar as the new one.
        if row['avatar_url']!=profile['avatar_url']:updates['image_path']=''
        self.store.update_profile(user,**updates)
        intro={'success':True,**text_evidence(profile['intro'])}
        evidence={'classified_at':now(),'threshold':config['positive_threshold'],
                  'rule':'any_strong_signal','decision_policy':'any_strong_v1',
                  'intro':intro,'decision_sources':[]}
        fast=config.get('evidence_mode','fast')=='fast'
        evidence['evidence_mode']='fast' if fast else 'full'
        if fast:
            avatar=self._collect_avatar(user,profile,row,config,job,evidence)
            if intro['strong_keywords'] or (avatar['success'] and avatar.get('furry_score',0)>=config['positive_threshold']):
                videos={'success':False,'items':[],'sample_size':0,'strong_title_keywords':[],
                        'strong_tag_keywords':[],'weak_keywords':[],'negated_keywords':[],
                        'skipped':True,'skip_reason':'简介或头像已明确命中，快速判定省略视频请求'}
                evidence['videos']=videos
            else:videos=self._collect_videos(user,cookie,config,job,evidence,True)
        else:
            videos=self._collect_videos(user,cookie,config,job,evidence,False)
            avatar=self._collect_avatar(user,profile,row,config,job,evidence)
        sources=evidence['decision_sources']
        if intro['strong_keywords']:sources.append('intro')
        if videos['strong_title_keywords']:sources.append('video_title')
        if videos['strong_tag_keywords']:sources.append('video_tag')
        score=avatar.get('furry_score')
        if avatar['success'] and score>=config['positive_threshold']:sources.append('avatar')
        evidence['keywords']=list(dict.fromkeys(intro['strong_keywords']+videos['strong_title_keywords']+videos['strong_tag_keywords']))
        ambiguous=any(part[key] for part in (intro,videos) for key in ('weak_keywords','negated_keywords'))
        if sources:
            automatic='yes'
            evidence['automatic_reason']='任一证据明确命中：'+ '、'.join({'intro':'简介','video_title':'视频标题','video_tag':'视频标签','avatar':'头像'}[source] for source in sources)
        elif avatar['success'] and score<=1-config['positive_threshold'] and videos['success'] and not ambiguous:
            automatic='no';evidence['automatic_reason']='简介、近期视频采样完成且无相关文字信号，头像分数低于排除阈值'
        else:
            automatic='unknown';evidence['automatic_reason']='缺少明确证据、存在弱相关文字或有证据未能获取，等待人工审核'
        self.store.classification(user,automatic,evidence)
        row=self.store.finalize_classification(user)
        if row['verdict']=='unknown':
            self.store.event('三项证据不足，进入候选审核；确认后才加入种子库并扩散','warning',user)
            return
        if row['verdict']=='no':
            self.store.event('归入 Furry No，本轮不扩散',user=user)
            return
        if not started_as_seed:
            self.store.event('候选已确认 Furry 并加入种子库，等待当前调度队列完成后再遍历关系',user=user)
            return
        self._expand_seed(user,cookie,config,job)

    def _collect_videos(self,user,cookie,config,job,evidence,fast):
        videos={'success':False,'items':[],'sample_size':0,'strong_title_keywords':[],
                'strong_tag_keywords':[],'weak_keywords':[],'negated_keywords':[]}
        try:
            self._phase(job,user,'检查近期视频标题与标签')
            sampled=self.client.recent_video_tags(user,cookie,self.stop_event,config['request_interval'],fast)
            if not isinstance(sampled,dict) or not (sampled.get('success') is True or (fast and sampled.get('skipped') is True)) or not isinstance(sampled.get('videos'),list):
                raise CollectionError('近期视频信息不完整，保留待审核','video_format')
            videos.update(success=sampled.get('success') is True,items=sampled['videos'][:3],sample_size=len(sampled['videos'][:3]))
            if sampled.get('skipped'):
                videos.update(skipped=True,skip_reason=str(sampled.get('skip_reason','快速判定已命中')))
            if any(not isinstance(item,dict) or not isinstance(item.get('title'),str) or not isinstance(item.get('tags'),list) or any(not isinstance(tag,str) for tag in item['tags']) for item in videos['items']):
                raise CollectionError('近期视频标题或标签不完整，保留待审核','video_format')
        except CollectionError as e:
            partial=getattr(e,'video_items',None)
            if partial is not None:videos.update(items=partial[:3],sample_size=len(partial[:3]))
            videos.update(success=False,error=str(e),error_code=e.code)
            self.store.event('视频证据获取失败：'+str(e),'warning',user)
            if e.stop_all:
                evidence['videos']=videos
                self.store.classification(user,'unknown',evidence)
                raise
        for item in videos['items']:
            if not isinstance(item,dict) or not isinstance(item.get('title'),str) or not isinstance(item.get('tags'),list):continue
            for source,values in (('title',[item['title']]),('tag',item['tags'])):
                for value in values:
                    if not isinstance(value,str):continue
                    signals=text_evidence(value,exact_tag=source=='tag')
                    for key,dest in (('strong_keywords','strong_'+source+'_keywords'),
                                     ('weak_keywords','weak_keywords'),('negated_keywords','negated_keywords')):
                        for keyword in signals[key]:
                            if keyword not in videos[dest]:videos[dest].append(keyword)
        evidence['videos']=videos
        return videos

    def _collect_avatar(self,user,profile,row,config,job,evidence):
        avatar={'success':False}
        try:
            self._phase(job,user,'下载头像并识别')
            path=self.store.directory/'avatars'/f'{user}.img'
            cached=False
            if row.get('avatar_url')==profile['avatar_url'] and row.get('image_path')==str(path.relative_to(self.store.root)) and path.is_file():
                try:
                    from PIL import Image
                    if path.stat().st_size<=10_000_000:
                        with Image.open(path) as image:image.verify()
                        cached=True
                except (ImportError,OSError,ValueError,SyntaxError):pass
            if not cached:self.client.download(profile['avatar_url'],path,self.stop_event,config['request_interval'])
            avatar['cache_hit']=cached
            self.store.update_profile(user,image_path=str(path.relative_to(self.store.root)))
            self._phase(job,user,'等待模型识别')
            # Protect both lazy model/session construction and inference. Network
            # waits may overlap, but shared ONNX/YOLO state is never used in parallel.
            with self.inference_lock:
                if self.stop_event.is_set():raise InterruptedError()
                self._phase(job,user,'识别头像')
                prediction=self.classifier.predict(path,config['model'])
            evidence.update(prediction)
            avatar.update(prediction,success=True)
        except CollectionError as e:
            evidence['error']=str(e)
            avatar.update(error=str(e),error_code=e.code)
            self.store.event(str(e),'warning',user)
            if e.stop_all:
                evidence['avatar']=avatar
                self.store.classification(user,'unknown',evidence)
                raise
        evidence['avatar']=avatar
        return avatar

    def _expand_seed(self,user,cookie,config,job):
        errors=False
        for direction in ('followings','followers'):
            if self.stop_event.is_set():raise InterruptedError()
            row=self.store.profile(user)
            if not row['is_seed'] or row['verdict']!='yes':
                self.store.event('账号不再是已确认种子，停止本次关系扩散',user=user)
                return
            if row[direction+'_state'] in ('complete','unavailable'):continue
            start=row[direction+'_page']
            for page in range(start,start+config['pages_per_run']):
                if self.stop_event.is_set():raise InterruptedError()
                # The current seed owns the cycle until both directions and its
                # discovered candidates have been checked. Other pending users
                # must not interrupt this traversal. Recheck manual exclusion at
                # each page boundary before making another relation request.
                row=self.store.profile(user)
                if not row['is_seed'] or row['verdict']!='yes':
                    self.store.event('账号不再是已确认种子，停止本次关系扩散',user=user)
                    return
                self._phase(job,user,('关注' if direction=='followings' else '粉丝')+f'列表 · 第 {page} 页')
                try:
                    children,finished=self.client.relation(user,direction,page,cookie,self.stop_event,config['request_interval'])
                    if self.store.relation_page(user,direction,children,page,finished) is False:
                        self.store.event('账号不再是已确认种子，停止本次关系扩散',user=user)
                        return
                    self.store.event(('关注' if direction=='followings' else '粉丝')+f'第 {page} 页保存 {len(children)} 个 UID',user=user)
                    if finished:break
                except CollectionError as e:
                    if not e.stop_all and str(e.code) in ('22115','22118'):
                        message=('关注' if direction=='followings' else '粉丝')+'列表因平台隐私设置不可获取，保留已获取用户，不再请求此方向'
                        self.store.relation_unavailable(user,direction,message)
                        self.store.event(message,'warning',user)
                        break
                    self.store.relation_failure(user,direction,str(e))
                    errors=True
                    if e.stop_all:raise
                    self.store.event(str(e),'warning',user)
                    break
        complete=self.store.finish_traversal(user)
        if errors:self.store.update_profile(user,available_at=later(60))
        if complete:
            row=self.store.profile(user)
            message=('公开关系获取结束，部分方向因隐私不可获取，开始判定已获取关联用户'
                     if any(row[direction+'_state']=='unavailable' for direction in ('followings','followers')) else
                     '粉丝与关注获取完成，开始判定当前种子的关联用户')
        else:message='本批分页已保存，继续获取同一种子的粉丝与关注'
        self.store.event(message,user=user)

    def _run(self,job,config):
        self.requested_workers=config.get('worker_count',1)
        if self.requested_workers!=1:
            return self._run_parallel(job,config)
        return self._run_serial(job,config)

    def _run_serial(self,job,config):
        processed=0;seen=set();state='completed';message='本轮采集完成'
        continuous=config.get('continuous',False)
        batch_processed=0;waiting=False
        try:
            self.store.event(('开始持续采集' if continuous else '开始采集')+
                             ('：优先处理全局候选积压；每批最多 ' if config.get('collection_scope')=='candidates_first' else '：选择匹配分最高种子，获取粉丝与关注，再判定本批关联用户；每批最多 ')+
                             str(config['max_users'])+' 个 UID')
            while continuous or processed<config['max_users']:
                if self.stop_event.is_set():raise InterruptedError()
                if continuous and batch_processed>=config['max_users']:
                    seen.clear();batch_processed=0
                    self.store.job_update(job,current_uid=None,phase='准备下一批',
                                          message=f'持续采集中，已累计处理 {processed} 个 UID')
                    if self.stop_event.wait(self.ROUND_WAIT_SECONDS):raise InterruptedError()
                user=self.store.next_seed(seen)
                if user is None:
                    if not continuous:
                        message='本轮可执行步骤已处理完；当前种子、分页与关联用户判定进度仍保留'
                        break
                    # A partial traversal can be revisited in the next batch. Clear
                    # exclusions before deciding that there is no executable work.
                    if seen:
                        seen.clear();batch_processed=0
                        if self.store.next_seed(seen) is not None:
                            self.store.job_update(job,current_uid=None,phase='准备下一批',
                                                  message=f'持续采集中，已累计处理 {processed} 个 UID')
                            if self.stop_event.wait(self.ROUND_WAIT_SECONDS):raise InterruptedError()
                            continue
                    if not waiting:
                        self.store.job_update(job,current_uid=None,phase='等待新任务',
                                              message='当前轮暂无可执行步骤；延后记录到期或出现新种子后自动继续')
                        self.store.event('持续采集等待当前轮可执行步骤，当前种子与关联用户进度保留')
                        waiting=True
                    if self.stop_event.wait(self.IDLE_WAIT_SECONDS):raise InterruptedError()
                    continue
                if waiting:
                    waiting=False
                    self.store.job_update(job,message=f'持续采集中，已累计处理 {processed} 个 UID')
                    self.store.event('发现可执行步骤，按当前种子的采集与判定流程继续')
                if self.stop_event.is_set():raise InterruptedError()
                account=self._claim_account()
                if account is None:
                    if self.busy_account_ids():
                        if self.stop_event.wait(.1):raise InterruptedError()
                        continue
                    raise CollectionError('没有可用 Cookie，采集已停止','auth',True)
                seen.add(user)
                handed_off=False
                try:
                    row=self.store.profile(user)
                    kind='seed' if row['is_seed'] and row['verdict']=='yes' else 'candidate'
                    handed_off=True
                    self._execute_task(1,user,account,config,job,kind)
                except CollectionError as e:
                    self.store.update_profile(user,last_error=str(e),collection_status='failed',available_at=later(60))
                    self.store.event(str(e),'error',user)
                    if e.stop_all:
                        if e.code=='auth':self.store.account_result(account,'expired')
                        else:self.store.cooldown(getattr(e,'cooldown_seconds',300))
                        raise
                finally:
                    # Also covers failures before _execute_task can take over.
                    if not handed_off:self._release_account(account)
                processed+=1
                batch_processed+=1
                self.store.job_update(job,processed=processed)
        except InterruptedError:
            state='paused';message='已暂停，已保存分页可继续'
        except CollectionError as e:
            state='blocked';message=str(e)
        except Exception:
            state='failed';message='采集内部错误，已保留数据；请检查依赖或运行环境'
            self.store.event(message,'error')
        finally:
            # Serialize the terminal write with stop(), so a committed pause
            # request cannot race the final completed-state check.
            with self.lock:
                if state=='completed' and self.stop_event.is_set():
                    state='paused';message='已暂停，已保存分页可继续'
                self.store.job_update(job,state=state,message=message,processed=processed,current_uid=None,phase='',ended_at=now())
            self.store.event(message,'info' if state in ('completed','paused') else 'warning')

    def _run_parallel(self,job,config):
        """Claim work in one coordinator; only candidate evidence work may overlap."""
        processed=0;batch_processed=0;seen=set();waiting=False
        state='completed';message='本轮采集完成'
        continuous=config.get('continuous',False)
        in_flight={}
        pool=ThreadPoolExecutor(max_workers=MAX_WORKERS if self.requested_workers==0 else self.requested_workers,
                                thread_name_prefix='panel-candidate')

        def collect_finished(future):
            # This function only runs on the coordinator, including during the
            # final join. No child changes job counters or account reservations.
            nonlocal processed,batch_processed,state,message
            task=in_flight.pop(future)
            user=task['uid'];account=task['account_id']
            count=False
            try:
                future.result()
                count=True
            except InterruptedError:
                if state not in ('blocked','failed'):
                    state='paused';message='已暂停，已保存分页可继续'
            except CollectionError as error:
                self.store.update_profile(user,last_error=str(error),collection_status='failed',available_at=later(60))
                self.store.event(str(error),'error',user)
                if error.stop_all:
                    self.stop_event.set()
                    if error.code=='auth':self.store.account_result(account,'expired')
                    else:self.store.cooldown(getattr(error,'cooldown_seconds',300))
                    state='blocked';message=str(error)
                else:count=True
            except Exception:
                self.stop_event.set()
                state='failed';message='采集内部错误，已保留数据；请检查依赖或运行环境'
                self.store.event(message,'error')
            if count:
                processed+=1;batch_processed+=1
                self.store.job_update(job,processed=processed)

        try:
            self.store.event(('开始持续采集' if continuous else '开始采集')+
                             ('：优先处理全局候选积压，' if config.get('collection_scope')=='candidates_first' else '：先获取单个最高分种子的粉丝与关注，再')+
                             ('按可用账号数自动分配判定线程（最多 '+str(MAX_WORKERS)+' 个）'
                              if self.requested_workers==0 else '用最多 '+str(self.requested_workers)+' 个账号判定本批关联用户')+
                             '；每批最多 '+
                             str(config['max_users'])+' 个 UID')
            while True:
                for future in tuple(in_flight):
                    if future.done():collect_finished(future)
                if self.stop_event.is_set():raise InterruptedError()
                if not continuous and processed>=config['max_users'] and not in_flight:break
                if continuous and batch_processed>=config['max_users'] and not in_flight:
                    seen.clear();batch_processed=0
                    self.store.job_update(job,current_uid=None,phase='准备下一批',
                                          message=f'持续采集中，已累计处理 {processed} 个 UID')
                    if self.stop_event.wait(self.ROUND_WAIT_SECONDS):raise InterruptedError()

                if not in_flight and self._worker_limit()==0:
                    raise CollectionError('没有可用 Cookie，采集已停止','auth',True)
                while len(in_flight)<self._worker_limit() and batch_processed+len(in_flight)<config['max_users']:
                    if self.stop_event.is_set():raise InterruptedError()
                    # No other task may overlap a relation traversal. Conversely,
                    # never select/lock a seed while candidate work is in flight.
                    if any(task['kind']=='seed' for task in in_flight.values()):break
                    # A candidate may already have committed its verdict while
                    # its future is still exiting. In that interval the store
                    # must not finish this cycle or lock the next root.
                    user=self.store.next_seed(seen,candidates_only=bool(in_flight))
                    if user is None:break
                    row=self.store.profile(user)
                    kind='seed' if row['is_seed'] and row['verdict']=='yes' else 'candidate'
                    if kind=='seed' and in_flight:break
                    busy_accounts={task['account_id'] for task in in_flight.values()}
                    account=self._claim_account(exclude=busy_accounts)
                    if account is None:
                        if in_flight or self.busy_account_ids():break
                        raise CollectionError('没有可用 Cookie，采集已停止','auth',True)
                    try:
                        if waiting:
                            waiting=False
                            self.store.job_update(job,message=f'持续采集中，已累计处理 {processed} 个 UID')
                            self.store.event('发现可执行步骤，按当前种子的采集与判定流程继续')
                        seen.add(user)
                        busy_slots={task['slot'] for task in in_flight.values()}
                        slot=next(value for value in range(1,MAX_WORKERS+1) if value not in busy_slots)
                        future=pool.submit(self._execute_task,slot,user,account,config,job,kind)
                    except Exception:
                        self._release_account(account)
                        raise
                    in_flight[future]={'slot':slot,'uid':user,'account_id':account,'kind':kind}
                    if kind=='seed':break

                if in_flight:
                    # The coordinator remains alive during cancellation and waits
                    # for every child, including one inside a bounded HTTP call.
                    wait(tuple(in_flight),timeout=.1,return_when=FIRST_COMPLETED)
                    continue
                if self.busy_account_ids():
                    # A user-triggered validation owns the remaining account.
                    # It is temporary unavailability, not an authentication failure.
                    if self.stop_event.wait(.1):raise InterruptedError()
                    continue
                if not continuous:
                    message='本轮可执行步骤已处理完；当前种子、分页与关联用户判定进度仍保留'
                    break
                if seen:
                    seen.clear();batch_processed=0
                    if self.store.next_seed(seen) is not None:
                        self.store.job_update(job,current_uid=None,phase='准备下一批',
                                              message=f'持续采集中，已累计处理 {processed} 个 UID')
                        if self.stop_event.wait(self.ROUND_WAIT_SECONDS):raise InterruptedError()
                        continue
                if not waiting:
                    self.store.job_update(job,current_uid=None,phase='等待新任务',
                                          message='当前轮暂无可执行步骤；延后记录到期或出现新种子后自动继续')
                    self.store.event('持续采集等待当前轮可执行步骤，当前种子与关联用户进度保留')
                    waiting=True
                if self.stop_event.wait(self.IDLE_WAIT_SECONDS):raise InterruptedError()
        except InterruptedError:
            if state not in ('blocked','failed'):
                state='paused';message='已暂停，已保存分页可继续'
        except CollectionError as error:
            state='blocked';message=str(error)
            self.stop_event.set()
        except Exception:
            state='failed';message='采集内部错误，已保留数据；请检查依赖或运行环境'
            self.stop_event.set()
            self.store.event(message,'error')
        finally:
            if in_flight:self.stop_event.set()
            pool.shutdown(wait=True)
            for future in tuple(in_flight):collect_finished(future)
            with self.lock:
                if state=='completed' and self.stop_event.is_set():
                    state='paused';message='已暂停，已保存分页可继续'
                self.store.job_update(job,state=state,message=message,processed=processed,
                                      current_uid=None,phase='',ended_at=now())
            self.store.event(message,'info' if state in ('completed','paused') else 'warning')
