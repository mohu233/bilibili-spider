"""Loopback-only HTML/API server; cookie values are write-only at the HTTP boundary."""
import argparse
import hmac
import json
import os
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .collector import Collector
from .store import Store, now, uid


class DashboardServer(ThreadingHTTPServer):
    daemon_threads=True

    def __init__(self,root,port=8767,store=None,collector=None):
        self.project_root=Path(root).resolve()
        self.assets=Path(__file__).parent/'static'
        self.token=secrets.token_urlsafe(32)
        self.store=store or Store(root)
        self.collector=collector or Collector(self.store)
        super().__init__(('127.0.0.1',port),Handler)
        self.allowed_hosts={f'127.0.0.1:{self.server_port}',f'localhost:{self.server_port}'}


class Handler(BaseHTTPRequestHandler):
    server_version='BilibiliPanel/1.0'

    def log_message(self,*args):
        pass

    def send_data(self,status,data,content_type='application/json; charset=utf-8',extra=None):
        if not isinstance(data,bytes):data=json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key,value in (extra or {}).items():self.send_header(key,value)
        self.end_headers()
        try:self.wfile.write(data)
        except (BrokenPipeError,ConnectionResetError):pass

    def allowed(self,mutation=False):
        if self.headers.get('Host') not in self.server.allowed_hosts:
            self.send_data(403,{'error':'仅支持本机地址访问'});return False
        origin=self.headers.get('Origin')
        allowed_origins={'http://'+h for h in self.server.allowed_hosts}
        if origin and origin not in allowed_origins:
            self.send_data(403,{'error':'跨来源访问已拒绝'});return False
        if mutation:
            if origin not in allowed_origins or not hmac.compare_digest(self.headers.get('X-Panel-Token',''),self.server.token):
                self.send_data(403,{'error':'请从本机面板页面执行操作'});return False
        return True

    def do_GET(self):
        if not self.allowed():return
        parts=urllib.parse.urlsplit(self.path)
        query=urllib.parse.parse_qs(parts.query)
        arg=lambda k,default='':query.get(k,[default])[0]
        try:
            if parts.path in ('/','/app.js','/style.css','/favicon.svg'):
                filename={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/favicon.svg':'favicon.svg'}[parts.path]
                content_type={'index.html':'text/html; charset=utf-8','app.js':'text/javascript; charset=utf-8',
                              'style.css':'text/css; charset=utf-8','favicon.svg':'image/svg+xml'}[filename]
                self.send_data(200,(self.server.assets/filename).read_bytes(),content_type);return
            if parts.path=='/api/bootstrap':
                self.send_data(200,{'token':self.server.token,'project_root':str(self.server.project_root),
                                   'models':self.server.collector.classifier.availability()});return
            if parts.path=='/api/state':
                state=self.server.store.state();state['worker_active']=self.server.collector.active()
                self.send_data(200,state);return
            if parts.path=='/api/profiles':
                self.send_data(200,self.server.store.list_profiles(arg('view','seeds'),arg('page','1'),
                    arg('q'),arg('status'),arg('sort','score')));return
            if parts.path=='/api/profile':
                self.send_data(200,self.server.store.profile(arg('uid')));return
            if parts.path=='/api/references':
                self.send_data(200,self.server.store.references(arg('q'),int(arg('page','1'))));return
            if parts.path.startswith('/avatar/'):
                user=uid(parts.path.removeprefix('/avatar/'))
                row=self.server.store.profile(user)
                path=(self.server.project_root/row['image_path']).resolve()
                allowed_dirs=[self.server.project_root/n for n in ('data','hit','miss','avatars','_cache')]
                if not row['image_path'] or not any(path.is_relative_to(p) for p in allowed_dirs) or path.suffix.lower() not in ('.jpg','.jpeg','.png','.webp','.gif','.img'):
                    raise ValueError('没有本地头像')
                if not path.is_file() or path.stat().st_size>10_000_000:raise ValueError('没有可用头像')
                data=path.read_bytes()
                kind='image/png' if data.startswith(b'\x89PNG') else 'image/jpeg' if data.startswith(b'\xff\xd8') else 'image/gif' if data.startswith(b'GIF8') else 'image/webp' if data.startswith(b'RIFF') and data[8:12]==b'WEBP' else None
                if not kind:raise ValueError('头像格式不受支持')
                self.send_data(200,data,kind);return
            if parts.path=='/api/export':
                view=arg('view','seeds');page=1;records=[]
                while True:
                    data=self.server.store.list_profiles(view,page,limit=100)
                    records+=data['results']
                    if len(records)>=data['total']:break
                    page+=1
                exported=[]
                for r in records:
                    with self.server.store.db() as c:
                        found=[int(x['child_uid']) for x in c.execute('''SELECT DISTINCT e.child_uid FROM edges e
                        JOIN profiles p ON p.uid=e.child_uid WHERE e.parent_uid=? AND p.verdict='yes' ''',(r['uid'],))]
                    exported.append({**r,'uid':int(r['uid']),'seed_weight':r['score'],
                                     'parent_seed':int(r['parent_uid']) if r['parent_uid'] else None,
                                     'discovered_uids':found,
                                     'hit':'furry-manual' if r['manual']=='yes' else 'furry-model' if r['verdict']=='yes' else 'none' if r['verdict']=='no' else 'unknown'})
                payload={'meta':{'source':'local_panel','exported_at':now(),'total_count':len(exported)},'uids':exported}
                self.send_data(200,payload,extra={'Content-Disposition':f'attachment; filename="furry_{view}.json"'});return
            self.send_data(404,{'error':'页面不存在'})
        except (ValueError,TypeError):self.send_data(400,{'error':'请求参数无效或记录不可用'})
        except Exception:self.send_data(500,{'error':'本地数据读取失败，请检查数据文件或运行环境'})

    def do_POST(self):
        if not self.allowed(mutation=True):return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=512000:raise ValueError('请求内容为空或过大')
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('仅接受 JSON 请求')
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict):raise ValueError('请求内容应为对象')
            path=urllib.parse.urlsplit(self.path).path
            store=self.server.store;worker=self.server.collector
            if path=='/api/seeds':
                result=store.add_seeds(payload.get('uids',''),payload.get('base_score',10),payload.get('verdict','unknown'))
            elif path=='/api/decision':
                store.decision(payload['uid'],payload['decision'],payload.get('reason',''));result={'ok':True}
            elif path=='/api/base-score':
                store.set_base(payload['uid'],payload['base_score']);result={'ok':True}
            elif path=='/api/requeue':
                if worker.active():raise ValueError('请先暂停采集，再重新安排遍历')
                store.requeue(payload['uid']);result={'ok':True}
            elif path=='/api/import':
                if worker.active():raise ValueError('请先暂停采集，再导入数据')
                result=store.import_legacy(payload.get('benchmark') is True)
            elif path=='/api/settings':
                if worker.active():raise ValueError('请先暂停采集，再修改运行设置')
                result=store.set_config(payload)
            elif path=='/api/cookies':
                result={'id':store.account_add(payload.get('label',''),payload.get('cookie',''))}
            elif path=='/api/cookies/import':
                result=store.import_cookies()
            elif path=='/api/cookies/toggle':
                store.account_enabled(payload['id'],payload['enabled']);result={'ok':True}
            elif path=='/api/cookies/validate':
                result=worker.validate_account(payload['id'])
            elif path=='/api/jobs/start':
                result={'job_id':worker.start()}
            elif path=='/api/jobs/stop':
                worker.stop();result={'ok':True}
            else:self.send_data(404,{'error':'操作不存在'});return
            self.send_data(200,result)
        except KeyError:self.send_data(400,{'error':'缺少必要参数'})
        except (ValueError,TypeError) as e:self.send_data(400,{'error':str(e) if isinstance(e,ValueError) else '参数类型无效'})
        except Exception:self.send_data(500,{'error':'操作未完成，请检查本地数据或运行环境'})


def main(default_root=None):
    parser=argparse.ArgumentParser(description='Bilibili Spider 本地采集面板')
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('--project-root',type=Path,default=default_root or Path(__file__).parents[1])
    args=parser.parse_args()
    if not 1024<=args.port<=65535:parser.error('port 必须为 1024–65535')
    root=args.project_root.resolve()
    directory=root/'data'/'panel';directory.mkdir(parents=True,exist_ok=True)
    lock=(directory/'server.lock').open('a+b')
    if lock.seek(0,os.SEEK_END)==0:lock.write(b'0');lock.flush()
    lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        print('此项目已有面板进程运行，请打开原面板。',flush=True)
        lock.close();return 1
    server=None
    try:
        store=Store(root);store.recover_jobs()
        server=DashboardServer(root,args.port,store=store)
        print(f'Local: http://127.0.0.1:{server.server_port}',flush=True)
        print('仅本机访问；添加种子与 Cookie 后，由页面手动启动采集。',flush=True)
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:pass
    finally:
        if server:
            server.collector.stop()
            if server.collector.thread:server.collector.thread.join(timeout=25)
            server.server_close()
        lock.close()
    return 0
