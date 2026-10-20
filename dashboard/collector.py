"""Bounded, resumable collection jobs for the local dashboard."""
import importlib.util
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .store import later, now, uid


class CollectionError(Exception):
    def __init__(self,message,code='network',stop_all=False):
        super().__init__(message)
        self.code=code
        self.stop_all=stop_all


class BiliClient:
    def __init__(self):
        self.lock=threading.Lock()
        self.last_request=0
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self,url,cookie='',stop=None,interval=1.5,binary=False):
        with self.lock:
            wait=max(0,interval-(time.monotonic()-self.last_request))
            if stop and stop.wait(wait):raise InterruptedError()
            if not stop and wait:time.sleep(wait)
            if stop and stop.is_set():raise InterruptedError()
            headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/',
                     'Accept':'application/json' if not binary else 'image/*','Cache-Control':'no-cache'}
            if urllib.parse.urlsplit(url).hostname=='api.bilibili.com' and cookie:
                headers['Cookie']=cookie
            self.last_request=time.monotonic()
            try:
                with self.opener.open(urllib.request.Request(url,headers=headers),timeout=20) as response:
                    raw=response.read(10_000_001)
                if len(raw)>10_000_000:raise CollectionError('响应超过大小限制','response_size')
                if binary:return raw
                data=json.loads(raw)
            except urllib.error.HTTPError as e:
                if e.code in (412,429):raise CollectionError('平台访问受限，已暂停整轮采集',str(e.code),True) from None
                raise CollectionError(f'HTTP 请求失败（{e.code}）','http') from None
            except (urllib.error.URLError,TimeoutError,OSError):
                raise CollectionError('网络连接失败或超时，进度已保留','network') from None
            except (ValueError,UnicodeError):raise CollectionError('平台响应格式异常','response_format') from None
            if not isinstance(data,dict):raise CollectionError('平台响应格式异常','response_format')
            code=data.get('code')
            if code in (-352,-412,127306):
                raise CollectionError('平台风控提示，已暂停整轮采集并冷却 5 分钟',str(code),True)
            if code==-101:raise CollectionError('Cookie 已失效，请更新账号凭据','auth',True)
            if code!=0:raise CollectionError(f'平台拒绝本次请求（code={code}）',str(code))
            return data.get('data')

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

    def download(self,url,path,stop,interval):
        host=urllib.parse.urlsplit(url).hostname
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


class Collector:
    def __init__(self,store,client=None,classifier=None):
        self.store=store
        self.client=client or BiliClient()
        self.classifier=classifier or Classifier(store.root)
        self.stop_event=threading.Event()
        self.lock=threading.Lock()
        self.thread=None
        self.job_id=None

    def active(self):
        return bool(self.thread and self.thread.is_alive())

    def validate_account(self,ident):
        try:
            name=self.client.validate(self.store.account_secret(ident))
            self.store.account_result(ident,'valid',name)
            return {'valid':True,'message':'账号验证成功'}
        except CollectionError as e:
            self.store.account_result(ident,'expired' if e.code=='auth' else 'error')
            if e.stop_all and e.code!='auth':self.store.cooldown(300)
            return {'valid':False,'message':str(e)}

    def start(self):
        with self.lock:
            if self.active():raise ValueError('已有采集任务运行中')
            config=self.store.get_config()
            if config.get('cooldown_until','')>now():raise ValueError('平台冷却中，请等待冷却结束后再启动')
            if not any(a['enabled'] and a['status']!='expired' for a in self.store.accounts()):
                raise ValueError('请先在 Cookie 池添加并启用有效账号')
            if not self.store.next_seed(set()):raise ValueError('没有可采集种子；待审核或延后记录需要处理后再启动')
            availability=self.classifier.availability().get(config['model'],{})
            if not availability.get('weights') or not availability.get('runtime'):
                raise ValueError('所选模型权重或运行依赖不可用，请检查采集设置')
            self.stop_event.clear()
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
        cookie=self.store.account_secret(account)
        self.store.job_update(job,current_uid=user,phase='获取用户资料')
        profile=self.client.profile(user,cookie,self.stop_event,config['request_interval'])
        self.store.update_profile(user,**profile,collected_at=now(),collection_status='fetched',last_error='')
        evidence={'classified_at':now(),'threshold':config['positive_threshold'],
                  'keywords':[s for s in ('furry','fursuit','福瑞','兽设','兽装') if s in profile['intro'].lower()]}
        automatic='unknown'
        try:
            self.store.job_update(job,phase='下载头像并识别')
            path=self.store.directory/'avatars'/f'{user}.img'
            self.client.download(profile['avatar_url'],path,self.stop_event,config['request_interval'])
            self.store.update_profile(user,image_path=str(path.relative_to(self.store.root)))
            evidence.update(self.classifier.predict(path,config['model']))
            score=evidence['furry_score']
            automatic='yes' if score>=config['positive_threshold'] else 'no' if score<=1-config['positive_threshold'] else 'unknown'
        except CollectionError as e:
            evidence['error']=str(e)
            self.store.event(str(e),'warning',user)
            if e.stop_all:raise
        self.store.classification(user,automatic,evidence)
        row=self.store.profile(user)
        if row['verdict']=='unknown':
            self.store.update_profile(user,collection_status='needs_review',available_at=later(3600))
            self.store.event('模型结果不确定，等待人工审核后扩散','warning',user)
            return
        self.store.update_profile(user,collection_status='classified',available_at='')
        if row['verdict']=='no':
            self.store.event('归入 Furry No，本轮不扩散',user=user)
            return
        errors=False
        for direction in ('followings','followers'):
            if self.stop_event.is_set():raise InterruptedError()
            row=self.store.profile(user)
            if row[direction+'_state']=='complete':continue
            start=row[direction+'_page']
            for page in range(start,start+config['pages_per_run']):
                if self.stop_event.is_set():raise InterruptedError()
                self.store.job_update(job,phase=('关注' if direction=='followings' else '粉丝')+f'列表 · 第 {page} 页')
                try:
                    children,finished=self.client.relation(user,direction,page,cookie,self.stop_event,config['request_interval'])
                    self.store.relation_page(user,direction,children,page,finished)
                    self.store.event(('关注' if direction=='followings' else '粉丝')+f'第 {page} 页保存 {len(children)} 个 UID',user=user)
                    if finished:break
                except CollectionError as e:
                    self.store.relation_failure(user,direction,str(e))
                    errors=True
                    if e.stop_all:raise
                    self.store.event(str(e),'warning',user)
                    break
        complete=self.store.finish_traversal(user)
        if errors:self.store.update_profile(user,available_at=later(60))
        self.store.event('粉丝与关注遍历完成，种子得分已重算' if complete else '本轮分页已保存，仍为部分完成',user=user)

    def _run(self,job,config):
        processed=0;seen=set();state='completed';message='本轮采集完成'
        try:
            self.store.event('开始采集：按种子得分排序，单轮最多 '+str(config['max_users'])+' 个 UID')
            while processed<config['max_users']:
                if self.stop_event.is_set():raise InterruptedError()
                user=self.store.next_seed(seen)
                if user is None:
                    message='本轮可用种子已处理完；待审核和未完成分页仍保留'
                    break
                account=self.store.next_account()
                if account is None:raise CollectionError('没有可用 Cookie，采集已停止','auth',True)
                seen.add(user)
                try:
                    self._process(user,account,config,job)
                except CollectionError as e:
                    self.store.update_profile(user,last_error=str(e),collection_status='failed',available_at=later(60))
                    self.store.event(str(e),'error',user)
                    if e.stop_all:
                        if e.code=='auth':self.store.account_result(account,'expired')
                        else:self.store.cooldown(300)
                        raise
                processed+=1
                self.store.job_update(job,processed=processed)
        except InterruptedError:
            state='paused';message='已暂停，已保存分页可继续'
        except CollectionError as e:
            state='blocked';message=str(e)
        except Exception:
            state='failed';message='采集内部错误，已保留数据；请检查依赖或运行环境'
            self.store.event(message,'error')
        finally:
            self.store.job_update(job,state=state,message=message,processed=processed,current_uid=None,phase='',ended_at=now())
            self.store.event(message,'info' if state in ('completed','paused') else 'warning')
