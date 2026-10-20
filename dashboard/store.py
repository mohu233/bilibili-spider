"""SQLite state and bounded seed scoring. Legacy JSON files stay untouched."""
import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def later(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def uid(value):
    value = str(value).strip()
    if not re.fullmatch(r'[1-9][0-9]{0,18}', value) or int(value) > 9223372036854775807:
        raise ValueError('UID 必须为有效的正整数')
    return str(int(value))


def score_parts(base, parent_snapshot, matches, evaluated):
    base = min(20., max(0., float(base)))
    inherited = min(10., max(0., float(parent_snapshot)) * .1)
    matches = max(0, int(matches))
    evaluated = max(matches, int(evaluated))
    discovery = 50 * (1 - math.exp(-matches / 20))
    quality = 20 * matches / (evaluated + 10)
    return {'base_score': round(base, 3), 'inherited_score': round(inherited, 3),
            'discovery_score': round(discovery, 3), 'quality_score': round(quality, 3),
            'score': round(min(100., base + inherited + discovery + quality), 3),
            'furry_found': matches, 'evaluated_found': evaluated}


DEFAULT_CONFIG = {'max_users': 10, 'pages_per_run': 2, 'request_interval': 1.5,
                  'model': 'mobilenetv3_v0.1_dist', 'positive_threshold': .85}


class Store:
    def __init__(self, project_root, database=None):
        self.root = Path(project_root).resolve()
        self.directory = self.root / 'data' / 'panel'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = Path(database) if database else self.directory / 'panel.sqlite3'
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS profiles(
              uid TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', intro TEXT NOT NULL DEFAULT '',
              avatar_url TEXT NOT NULL DEFAULT '', image_path TEXT NOT NULL DEFAULT '',
              fan_count INTEGER, follow_count INTEGER,
              verdict TEXT NOT NULL DEFAULT 'unknown', automatic TEXT NOT NULL DEFAULT 'unknown',
              manual TEXT, manual_reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '{}',
              is_seed INTEGER NOT NULL DEFAULT 1, source TEXT NOT NULL DEFAULT 'manual',
              parent_uid TEXT, parent_score_snapshot REAL NOT NULL DEFAULT 0,
              base_score REAL NOT NULL DEFAULT 10, inherited_score REAL NOT NULL DEFAULT 0,
              discovery_score REAL NOT NULL DEFAULT 0, quality_score REAL NOT NULL DEFAULT 0,
              score REAL NOT NULL DEFAULT 10, furry_found INTEGER NOT NULL DEFAULT 0,
              evaluated_found INTEGER NOT NULL DEFAULT 0,
              traversal TEXT NOT NULL DEFAULT 'pending', collection_status TEXT NOT NULL DEFAULT 'pending',
              followers_state TEXT NOT NULL DEFAULT 'pending', followings_state TEXT NOT NULL DEFAULT 'pending',
              followers_page INTEGER NOT NULL DEFAULT 1, followings_page INTEGER NOT NULL DEFAULT 1,
              last_error TEXT NOT NULL DEFAULT '', available_at TEXT NOT NULL DEFAULT '',
              collected_at TEXT, completed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              legacy TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS edges(
              parent_uid TEXT NOT NULL, child_uid TEXT NOT NULL, direction TEXT NOT NULL,
              parent_score_snapshot REAL NOT NULL, discovered_at TEXT NOT NULL,
              PRIMARY KEY(parent_uid,child_uid,direction));
            CREATE INDEX IF NOT EXISTS edges_child ON edges(child_uid);
            CREATE INDEX IF NOT EXISTS profiles_queue ON profiles(verdict,traversal,available_at,score);
            CREATE TABLE IF NOT EXISTS accounts(
              id TEXT PRIMARY KEY, label TEXT NOT NULL, secret TEXT NOT NULL,
              fingerprint TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL DEFAULT 'unverified', account_name TEXT NOT NULL DEFAULT '',
              checked_at TEXT, used_at TEXT, request_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, state TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
              processed INTEGER NOT NULL DEFAULT 0, current_uid TEXT, phase TEXT NOT NULL DEFAULT '',
              message TEXT NOT NULL DEFAULT '', options TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, level TEXT NOT NULL,
              message TEXT NOT NULL, uid TEXT);
            CREATE TABLE IF NOT EXISTS reference_profiles(
              uid TEXT NOT NULL,source TEXT NOT NULL,name TEXT NOT NULL DEFAULT '',
              previous_hit TEXT NOT NULL DEFAULT '',data TEXT NOT NULL,imported_at TEXT NOT NULL,
              PRIMARY KEY(uid,source));
            ''')
            for key, value in DEFAULT_CONFIG.items():
                c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key,json.dumps(value)))

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA busy_timeout=15000')
        try:
            with c:
                yield c
        finally:
            c.close()

    def event(self, message, level='info', user=None):
        with self.db() as c:
            c.execute('INSERT INTO events(time,level,message,uid) VALUES(?,?,?,?)',
                      (now(),level,message,user))

    def get_config(self):
        with self.db() as c:
            return {r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}

    def set_config(self, value):
        result = {k:value.get(k, v) for k,v in DEFAULT_CONFIG.items()}
        for k, low, high in [('max_users',1,500),('pages_per_run',1,20)]:
            if type(result[k]) is not int or not low <= result[k] <= high:
                raise ValueError(f'{k} 必须在 {low}–{high} 之间')
        for k,low,high in [('request_interval',.5,30),('positive_threshold',.5,.99)]:
            if type(result[k]) not in (int,float) or not math.isfinite(result[k]) or not low<=result[k]<=high:
                raise ValueError(f'{k} 超出允许范围')
        if result['model'] not in ('mobilenetv3_v0.1_dist','caformer_s36_v0.1','yolo'):
            raise ValueError('未知模型')
        with self.db() as c:
            for k,v in result.items():
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,json.dumps(v)))
        return result

    @staticmethod
    def _recalculate(c):
        aggregates = {r['parent_uid']:(r['matches'],r['evaluated']) for r in c.execute('''
          SELECT e.parent_uid,
          COUNT(DISTINCT CASE WHEN p.verdict='yes' THEN e.child_uid END) AS matches,
          COUNT(DISTINCT CASE WHEN p.verdict IN ('yes','no') THEN e.child_uid END) AS evaluated
          FROM edges e JOIN profiles p ON e.child_uid=p.uid
          WHERE e.parent_uid != e.child_uid GROUP BY e.parent_uid''')}
        updates=[]
        for r in c.execute('SELECT uid,base_score,parent_score_snapshot FROM profiles'):
            parts=score_parts(r['base_score'],r['parent_score_snapshot'],*aggregates.get(r['uid'],(0,0)))
            updates.append((parts['inherited_score'],parts['discovery_score'],parts['quality_score'],
                            parts['score'],parts['furry_found'],parts['evaluated_found'],r['uid']))
        c.executemany('''UPDATE profiles SET inherited_score=?,discovery_score=?,quality_score=?,
                        score=?,furry_found=?,evaluated_found=? WHERE uid=?''',updates)

    def add_seeds(self, text, base=10, verdict='unknown'):
        if not isinstance(text,str) or len(text)>100000:
            raise ValueError('请填写 UID，每行一个')
        inputs=[uid(x) for x in re.split(r'[\s,，;；]+',text.strip()) if x]
        tokens=list(dict.fromkeys(inputs))
        if not 1<=len(tokens)<=5000:
            raise ValueError('每次添加 1–5000 个 UID')
        if type(base) not in (int,float) or not math.isfinite(base) or not 0<=base<=20:
            raise ValueError('基础分应在 0–20 之间')
        if verdict not in ('unknown','yes'):
            raise ValueError('请选择待识别或确认 Furry')
        added=0
        with self.db() as c:
            for user in tokens:
                added+=c.execute('''INSERT OR IGNORE INTO profiles
                (uid,base_score,score,verdict,manual,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)''',(user,base,base,verdict,'yes' if verdict=='yes' else None,now(),now())).rowcount
        self.event(f'添加种子 {added} 个，重复 {len(inputs)-added} 个')
        return {'added':added,'duplicates':len(inputs)-added}

    def profile(self, user):
        with self.db() as c:
            row=c.execute('SELECT * FROM profiles WHERE uid=?',(uid(user),)).fetchone()
            if row is None:
                raise ValueError('UID 不在面板数据库中')
            record=dict(row)
            record['evidence']=json.loads(record['evidence'])
            record.pop('legacy',None)
            record['children']=[dict(r) for r in c.execute('''
              SELECT p.uid,p.name,p.verdict,e.direction FROM edges e JOIN profiles p ON p.uid=e.child_uid
              WHERE e.parent_uid=? ORDER BY e.discovered_at DESC LIMIT 100''',(user,))]
            record['relations_count']=c.execute('SELECT COUNT(DISTINCT child_uid) FROM edges WHERE parent_uid=?',(user,)).fetchone()[0]
            record['references']=[dict(r) for r in c.execute('SELECT source,name,previous_hit FROM reference_profiles WHERE uid=?',(user,))]
            return record

    def list_profiles(self, view='seeds', page=1, search='', status='', sort='score', limit=30):
        where,params=[],[]
        if view=='seeds':where.append("is_seed=1 AND verdict!='no' AND traversal NOT IN ('complete','legacy_done')")
        elif view=='yes':where.append("verdict='yes'")
        elif view=='no':where.append("verdict='no'")
        elif view!='all':raise ValueError('未知数据面板')
        if search:
            where.append('(uid LIKE ? OR name LIKE ?)');params.extend(['%'+search[:100]+'%']*2)
        if status in ('pending','partial','complete','failed','legacy_done'):
            where.append('traversal=?');params.append(status)
        elif status=='review':where.append("collection_status='needs_review'")
        clause=' WHERE '+' AND '.join(where) if where else ''
        order={'score':'score DESC,created_at ASC,uid ASC','recent':'updated_at DESC,uid ASC',
               'discoveries':'furry_found DESC,score DESC,uid ASC'}.get(sort,'score DESC,uid ASC')
        page=max(1,int(page));limit=max(1,min(100,int(limit)))
        with self.db() as c:
            total=c.execute('SELECT COUNT(*) FROM profiles'+clause,params).fetchone()[0]
            rows=[dict(r) for r in c.execute('SELECT * FROM profiles'+clause+' ORDER BY '+order+' LIMIT ? OFFSET ?',
                                            params+[limit,(page-1)*limit])]
        for r in rows:
            r.pop('legacy',None);r['evidence']=json.loads(r['evidence'])
        return {'results':rows,'total':total,'page':page,'page_size':limit}

    def decision(self,user,decision,reason=''):
        user=uid(user)
        if decision not in ('yes','no','clear'):raise ValueError('未知人工判定')
        with self.db() as c:
            row=c.execute('SELECT automatic FROM profiles WHERE uid=?',(user,)).fetchone()
            if row is None:raise ValueError('UID 不存在')
            manual=None if decision=='clear' else decision
            effective=manual or row['automatic']
            status='needs_review' if effective=='unknown' else 'reviewed' if manual else 'classified'
            c.execute('''UPDATE profiles SET manual=?,verdict=?,manual_reason=?,updated_at=?,available_at='',collection_status=?
                         WHERE uid=?''',(manual,effective,str(reason)[:500],now(),status,user))
            self._recalculate(c)
        self.event('人工判定：'+{'yes':'Furry Yes','no':'Furry No','clear':'恢复自动结果'}[decision],user=user)

    def requeue(self,user):
        with self.db() as c:
            changed=c.execute('''UPDATE profiles SET traversal='pending',followers_state='pending',
              followings_state='pending',followers_page=1,followings_page=1,available_at='',last_error='',
              completed_at=NULL,updated_at=? WHERE uid=?''',(now(),uid(user))).rowcount
            if not changed:raise ValueError('UID 不存在')
        self.event('重新安排关系遍历（已有关系保留并去重）',user=uid(user))

    def set_base(self,user,base):
        if type(base) not in (float,int) or not math.isfinite(base) or not 0<=base<=20:
            raise ValueError('基础分应在 0–20 之间')
        with self.db() as c:
            if not c.execute('UPDATE profiles SET base_score=?,updated_at=? WHERE uid=?',(base,now(),uid(user))).rowcount:
                raise ValueError('UID 不存在')
            self._recalculate(c)

    def update_profile(self,user,**fields):
        allowed={'name','intro','avatar_url','image_path','fan_count','follow_count','collection_status',
                 'last_error','available_at','collected_at','traversal','completed_at'}
        if not fields or set(fields)-allowed:raise ValueError('未知用户字段')
        fields['updated_at']=now()
        with self.db() as c:
            c.execute('UPDATE profiles SET '+','.join(k+'=?' for k in fields)+' WHERE uid=?',list(fields.values())+[uid(user)])

    def classification(self,user,automatic,evidence):
        if automatic not in ('yes','no','unknown'):raise ValueError('未知模型结果')
        with self.db() as c:
            c.execute('''UPDATE profiles SET automatic=?, verdict=COALESCE(manual,?),evidence=?,updated_at=?
                         WHERE uid=?''',(automatic,automatic,json.dumps(evidence,ensure_ascii=False),now(),uid(user)))
            self._recalculate(c)

    def relation_page(self,parent,direction,children,page,finished):
        parent=uid(parent)
        if direction not in ('followers','followings'):raise ValueError('未知关系方向')
        children=list(dict.fromkeys(uid(x) for x in children if str(x)!=parent))
        with self.db() as c:
            seed=c.execute('SELECT score FROM profiles WHERE uid=?',(parent,)).fetchone()
            if seed is None:raise ValueError('父级 UID 不存在')
            snapshot=seed['score']
            for child in children:
                parts=score_parts(10,snapshot,0,0)
                c.execute('''INSERT OR IGNORE INTO profiles(uid,source,parent_uid,parent_score_snapshot,
                inherited_score,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)''',
                          (child,'discovery',parent,snapshot,parts['inherited_score'],parts['score'],now(),now()))
                c.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)',(parent,child,direction,snapshot,now()))
            c.execute(f'''UPDATE profiles SET {direction}_page=?,{direction}_state=?,traversal='partial',updated_at=? WHERE uid=?''',
                      (page+1,'complete' if finished else 'partial',now(),parent))
            self._recalculate(c)

    def finish_traversal(self,user):
        row=self.profile(user)
        complete=all(row[k+'_state']=='complete' for k in ('followers','followings'))
        self.update_profile(user,traversal='complete' if complete else 'partial',
                            completed_at=now() if complete else None,last_error='')
        return complete

    def relation_failure(self,user,direction,message):
        if direction not in ('followers','followings'):raise ValueError('未知方向')
        with self.db() as c:
            c.execute(f'''UPDATE profiles SET {direction}_state='failed',traversal='partial',
                         last_error=?,updated_at=? WHERE uid=?''',(message,now(),uid(user)))

    def next_seed(self,exclude):
        with self.db() as c:
            rows=c.execute('''SELECT uid FROM profiles WHERE is_seed=1 AND verdict!='no'
            AND traversal NOT IN ('complete','legacy_done') AND (available_at='' OR available_at<=?)
            ORDER BY score DESC,created_at ASC,uid ASC''',(now(),))
            return next((r['uid'] for r in rows if r['uid'] not in exclude),None)

    def account_add(self,label,secret):
        if not isinstance(secret,str) or len(secret)>20000 or any(ord(x)<32 or ord(x)>255 for x in secret):
            raise ValueError('Cookie 格式无效，请粘贴完整 Cookie')
        if not any(k.strip()=='SESSDATA' and sep and v.strip() for k,sep,v in (s.partition('=') for s in secret.split(';'))):
            raise ValueError('Cookie 必须包含有效的 SESSDATA 字段')
        if not isinstance(label,str) or not label.strip() or len(label)>60:raise ValueError('填写 1–60 字的账号备注')
        ident=uuid.uuid4().hex
        fingerprint=hashlib.sha256(secret.strip().encode()).hexdigest()
        with self.db() as c:
            if c.execute('SELECT 1 FROM accounts WHERE fingerprint=?',(fingerprint,)).fetchone():
                raise ValueError('这个 Cookie 已在池中')
            c.execute('INSERT INTO accounts(id,label,secret,fingerprint,created_at) VALUES(?,?,?,?,?)',
                      (ident,label.strip(),secret.strip(),fingerprint,now()))
        self.event('新增 Cookie 账号（凭据不回显）')
        return ident

    def accounts(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('''SELECT id,label,enabled,status,account_name,checked_at,
                       used_at,request_count,created_at FROM accounts ORDER BY created_at''')]

    def account_enabled(self,ident,enabled):
        if type(enabled) is not bool:raise ValueError('启用状态无效')
        with self.db() as c:
            if not c.execute('UPDATE accounts SET enabled=? WHERE id=?',(int(enabled),ident)).rowcount:
                raise ValueError('账号不存在')

    def account_secret(self,ident):
        with self.db() as c:
            row=c.execute('SELECT secret FROM accounts WHERE id=?',(ident,)).fetchone()
            if row is None:raise ValueError('账号不存在')
            return row['secret']

    def account_result(self,ident,status,name=''):
        with self.db() as c:
            c.execute('UPDATE accounts SET status=?,account_name=?,checked_at=? WHERE id=?',(status,name[:100],now(),ident))

    def next_account(self):
        with self.db() as c:
            r=c.execute('''SELECT id FROM accounts WHERE enabled=1 AND status!='expired'
                           ORDER BY COALESCE(used_at,''),created_at LIMIT 1''').fetchone()
            if r:
                c.execute('UPDATE accounts SET used_at=?,request_count=request_count+1 WHERE id=?',(now(),r['id']))
            return r['id'] if r else None

    def cooldown(self,seconds):
        with self.db() as c:
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('cooldown_until',json.dumps(later(seconds))))

    def job_create(self,options):
        ident=uuid.uuid4().hex
        with self.db() as c:
            c.execute('INSERT INTO jobs(id,state,started_at,options) VALUES(?,?,?,?)',
                      (ident,'running',now(),json.dumps(options)))
        return ident

    def job_update(self,ident,**fields):
        allowed={'state','ended_at','processed','current_uid','phase','message'}
        if not fields or set(fields)-allowed:raise ValueError('未知任务字段')
        with self.db() as c:
            c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',list(fields.values())+[ident])

    def recover_jobs(self):
        with self.db() as c:
            c.execute("UPDATE jobs SET state='interrupted',ended_at=?,message='上次进程中断，可从已保存分页继续' WHERE state IN ('running','stopping')",(now(),))

    def state(self):
        with self.db() as c:
            counts={r['verdict']:r['n'] for r in c.execute('SELECT verdict,COUNT(*) n FROM profiles GROUP BY verdict')}
            counts['seeds']=c.execute("SELECT COUNT(*) FROM profiles WHERE is_seed=1 AND verdict!='no' AND traversal NOT IN ('complete','legacy_done')").fetchone()[0]
            counts['complete']=c.execute("SELECT COUNT(*) FROM profiles WHERE traversal IN ('complete','legacy_done')").fetchone()[0]
            counts['review']=c.execute("SELECT COUNT(*) FROM profiles WHERE collection_status='needs_review'").fetchone()[0]
            counts['total']=sum(counts.get(k,0) for k in ('yes','no','unknown'))
            job=c.execute('SELECT * FROM jobs ORDER BY started_at DESC LIMIT 1').fetchone()
            events=[dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 25')]
            leaders=[dict(r) for r in c.execute("SELECT uid,name,score,furry_found FROM profiles WHERE verdict='yes' ORDER BY score DESC LIMIT 5")]
            histogram=[0]*5
            for r in c.execute("SELECT score FROM profiles WHERE verdict!='no'"):
                histogram[min(4,int(r['score']//20))]+=1
            relation_count=c.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
            reference_count=c.execute('SELECT COUNT(DISTINCT uid) FROM reference_profiles').fetchone()[0]
        return {'counts':counts,'job':dict(job) if job else None,'events':events,'leaders':leaders,
                'histogram':histogram,'relation_count':relation_count,'reference_count':reference_count,'accounts':self.accounts(),
                'config':self.get_config(),'time':now()}

    def import_legacy(self,include_benchmark=False):
        records=[];files=[];benchmark_uids=[]
        for filename in ('matched_pending.json','matched_done.json','not_matched.json','error.json'):
            path=self.root/'data'/filename
            if not path.exists():continue
            try:
                doc=json.loads(path.read_text(encoding='utf-8-sig'))
                values=doc['uids'] if isinstance(doc,dict) else doc
                if not isinstance(values,list):raise ValueError()
                for value in values:
                    if not isinstance(value,dict):raise ValueError()
                    user=uid(value['uid'])
                    records.append((user,filename,value))
                files.append(filename)
            except (ValueError,KeyError,TypeError):raise ValueError(f'{filename} 格式错误，未导入任何数据') from None
        if include_benchmark:
            manifests=sorted((self.root/'data').glob('avatar_benchmark_*/manifest.json'))
            if not manifests:raise ValueError('未找到头像实验清单')
            path=manifests[-1]
            doc=json.loads(path.read_text(encoding='utf-8'))
            for row in doc['records']:
                value=dict(row);user=uid(value['uid'])
                value['avatar_url']=value.get('current_avatar_url',value.get('avatar_url',''))
                value['name']=value.get('current_name',value.get('name',''))
                records.append((user,str(path.relative_to(self.root)),value))
                benchmark_uids.append(user)
            files.append(str(path.relative_to(self.root)))
        added=0
        with self.db() as c:
            for user,source,value in records:
                added+=c.execute('INSERT OR IGNORE INTO reference_profiles VALUES(?,?,?,?,?,?)',
                (user,source,str(value.get('name') or '')[:200],str(value.get('hit') or '')[:500],
                 json.dumps(value,ensure_ascii=False),now())).rowcount
        if benchmark_uids:
            seeds=self.add_seeds(' '.join(dict.fromkeys(benchmark_uids)),10,'unknown')
            self.event('仅从头像实验提取 UID，旧头像、分类、分数和遍历状态未带入')
            return {**seeds,'reference_rows':added,'mode':'fresh_seeds','files':files}
        self.event(f'载入历史参考 {added} 条，未写入新采集队列或分类')
        return {'added':added,'mode':'reference_only','files':files}

    def references(self,search='',page=1):
        with self.db() as c:
            pattern='%'+search[:100]+'%'
            total=c.execute('SELECT COUNT(*) FROM reference_profiles WHERE uid LIKE ? OR name LIKE ?',
                            (pattern,pattern)).fetchone()[0]
            rows=[dict(r) for r in c.execute('''SELECT uid,source,name,previous_hit,imported_at FROM reference_profiles
                       WHERE uid LIKE ? OR name LIKE ? ORDER BY imported_at DESC,uid LIMIT 50 OFFSET ?''',
                       (pattern,pattern,(max(1,int(page))-1)*50))]
            return {'results':rows,'total':total,'page':page}

    def import_cookies(self):
        path=self.root/'cookies.json'
        if not path.exists():raise ValueError('项目根目录没有 cookies.json')
        try:
            values=json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(values,list):raise ValueError()
        except (ValueError,OSError):raise ValueError('cookies.json 无法读取为 Cookie 数组') from None
        added=0;skipped=0
        for i,value in enumerate(values):
            try:self.account_add(f'导入账号 {i+1}',value);added+=1
            except ValueError:skipped+=1
        return {'added':added,'skipped':skipped}
