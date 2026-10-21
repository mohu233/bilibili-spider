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


MAX_WORKERS = 20

DEFAULT_CONFIG = {'max_users': 10, 'pages_per_run': 2, 'request_interval': 1.5,
                  'model': 'mobilenetv3_v0.1_dist', 'positive_threshold': .85,
                  'continuous': False, 'worker_count': 1,
                  'collection_scope': 'candidates_first', 'evidence_mode': 'fast'}

# Keep the automatic queue separate from records awaiting a human decision.
# The same predicate backs its partial indexes, counts and FIFO selection.
CANDIDATE_WHERE = """is_seed=0 AND verdict='unknown'
    AND traversal NOT IN ('complete','legacy_done')
    AND collection_status IN ('pending','fetched','failed')"""


def identity_score_parts(evidence):
    """Evidence strength out of 100, separate from graph discovery value."""
    avatar=evidence.get('avatar') or {}
    value=avatar.get('furry_score',evidence.get('furry_score'))
    valid=type(value) in (int,float) and math.isfinite(value) and 0<=value<=1
    parts={'avatar':round(float(value)*40,3) if valid and avatar.get('success') is not False else 0.,
           'intro':20. if (evidence.get('intro') or {}).get('strong_keywords') else 0.,
           'video_title':20. if (evidence.get('videos') or {}).get('strong_title_keywords') else 0.,
           'video_tag':20. if (evidence.get('videos') or {}).get('strong_tag_keywords') else 0.}
    return {'identity_score':round(sum(parts.values()),3),'identity_components':parts}


def normalize_cookie(secret):
    """Accept a Cookie value or copied request headers without changing token values."""
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError('请粘贴完整 Cookie，必须包含 SESSDATA= 字段')
    if len(secret) > 20000:
        raise ValueError('Cookie 内容过长，请只复制 Cookie 请求头')
    secret = secret.strip(' \t\r\n')
    lines = re.split(r'\r\n|\r|\n', secret)
    headers = [(i, re.fullmatch(r'[ \t]*cookie[ \t]*:[ \t]*(.*)', line, re.I))
               for i, line in enumerate(lines)]
    headers = [(i, match.group(1)) for i, match in headers if match]
    if len(headers) > 1:
        raise ValueError('检测到多个 Cookie 请求头，请只复制一条请求的 Cookie')
    if headers:
        index, secret = headers[0]
        if index + 1 < len(lines) and lines[index + 1].strip() and not re.match(
                r"[ \t]*[!#$%&'*+.^_`|~0-9A-Za-z-]+[ \t]*:", lines[index + 1]):
            raise ValueError('Cookie 请求头被换行拆开，请重新复制完整的 Cookie 值')
    elif len(lines) > 1:
        raise ValueError('多行内容中未找到 Cookie: 请求头，请复制完整 Cookie 值或请求头')
    if any(x in secret for x in ('\u2026', '\u22ef')):
        raise ValueError('Cookie 含有省略号，内容已被截断；请重新复制未省略的原始值')
    if any(ord(x) < 32 or ord(x) == 127 or ord(x) > 255 for x in secret):
        raise ValueError('Cookie 含有换行、中文或特殊字符，请复制原始 Cookie 请求头')
    pairs = []
    sessions = []
    for part in secret.split(';'):
        if not part.strip():
            continue
        key, separator, value = part.strip().partition('=')
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise ValueError('Cookie 应为 name=value 格式；请保留 SESSDATA=，不要只粘贴字段值')
        pairs.append(f'{key}={value}')
        if key == 'SESSDATA':
            sessions.append(value)
    if not sessions or sessions == [''] or sessions == ['""']:
        raise ValueError('未找到非空的 SESSDATA= 字段，请复制登录后的完整 Cookie')
    if len(sessions) > 1:
        raise ValueError('检测到重复的 SESSDATA 字段，请只复制一份 Cookie')
    if '...' in sessions[0]:
        raise ValueError('SESSDATA 含有省略号，登录凭据已被截断；请重新复制未省略的原始值')
    return '; '.join(pairs)


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
              is_seed INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'manual',
              parent_uid TEXT, parent_score_snapshot REAL NOT NULL DEFAULT 0,
              base_score REAL NOT NULL DEFAULT 10, inherited_score REAL NOT NULL DEFAULT 0,
              discovery_score REAL NOT NULL DEFAULT 0, quality_score REAL NOT NULL DEFAULT 0,
              score REAL NOT NULL DEFAULT 10, identity_score REAL NOT NULL DEFAULT 0,
              furry_found INTEGER NOT NULL DEFAULT 0,
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
            if 'identity_score' not in {row['name'] for row in c.execute('PRAGMA table_info(profiles)')}:
                c.execute('ALTER TABLE profiles ADD COLUMN identity_score REAL NOT NULL DEFAULT 0')
            self._migrate_candidate_pool(c)
            c.execute('CREATE INDEX IF NOT EXISTS profiles_identity_queue ON profiles(is_seed,verdict,traversal,identity_score)')
            c.execute('CREATE INDEX IF NOT EXISTS profiles_candidate_fifo ON profiles(created_at,uid,available_at) WHERE '+CANDIDATE_WHERE)
            c.execute('CREATE INDEX IF NOT EXISTS profiles_candidate_retry ON profiles(available_at) WHERE '+CANDIDATE_WHERE)
            for row in c.execute("SELECT uid,evidence,identity_score FROM profiles WHERE evidence!='{}'").fetchall():
                evidence=json.loads(row['evidence'])
                scoring=identity_score_parts(evidence)
                if any(evidence.get(k)!=v for k,v in scoring.items()) or row['identity_score']!=scoring['identity_score']:
                    evidence.update(scoring)
                    c.execute('UPDATE profiles SET identity_score=?,evidence=? WHERE uid=?',
                              (scoring['identity_score'],json.dumps(evidence,ensure_ascii=False),row['uid']))
            c.execute("""UPDATE profiles SET is_seed=CASE WHEN verdict='yes' AND traversal NOT IN ('complete','legacy_done') THEN 1 ELSE 0 END
                WHERE is_seed!=CASE WHEN verdict='yes' AND traversal NOT IN ('complete','legacy_done') THEN 1 ELSE 0 END""")

    def _migrate_candidate_pool(self,c):
        if c.execute("SELECT 1 FROM settings WHERE key='candidate_pool_version'").fetchone():
            return
        # Recheck old automatic avatar-only assessments with the new evidence policy.
        # Manual decisions and all downloaded profiles/relations remain intact.
        for row in c.execute('SELECT uid,automatic,verdict,collection_status,traversal,completed_at,evidence FROM profiles WHERE manual IS NULL').fetchall():
            evidence=json.loads(row['evidence'])
            if evidence.get('decision_policy')=='any_strong_v1':
                continue
            if row['automatic']!='unknown' or row['collection_status']=='needs_review':
                evidence['previous_assessment']={k:row[k] for k in ('automatic','verdict','collection_status','traversal','completed_at')}
                c.execute("""UPDATE profiles SET automatic='unknown',verdict='unknown',is_seed=0,
                    collection_status='pending',traversal='pending',completed_at=NULL,available_at='',evidence=?,updated_at=? WHERE uid=?""",
                    (json.dumps(evidence,ensure_ascii=False),now(),row['uid']))
        c.execute("UPDATE profiles SET is_seed=CASE WHEN verdict='yes' THEN 1 ELSE 0 END")
        self._recalculate(c)
        c.execute("INSERT INTO settings(key,value) VALUES('candidate_pool_version','1')")

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
        for k, low, high in [('max_users',1,500),('pages_per_run',1,20),('worker_count',0,MAX_WORKERS)]:
            if type(result[k]) is not int or not low <= result[k] <= high:
                raise ValueError(f'{k} 必须在 {low}–{high} 之间')
        for k,low,high in [('request_interval',.5,30),('positive_threshold',.5,.99)]:
            if type(result[k]) not in (int,float) or not math.isfinite(result[k]) or not low<=result[k]<=high:
                raise ValueError(f'{k} 超出允许范围')
        if result['model'] not in ('mobilenetv3_v0.1_dist','caformer_s36_v0.1','yolo'):
            raise ValueError('未知模型')
        if type(result['continuous']) is not bool:
            raise ValueError('连续采集开关必须为 true 或 false')
        if result['collection_scope'] not in ('candidates_first','seed_cycle'):
            raise ValueError('未知采集范围')
        if result['evidence_mode'] not in ('fast','full'):
            raise ValueError('未知判定模式')
        with self.db() as c:
            for k,v in result.items():
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,json.dumps(v)))
        return result

    @staticmethod
    def _recalculate(c,users=None):
        if users is not None:
            users=tuple(dict.fromkeys(users))
            if not users:return
            if len(users)>500:
                for offset in range(0,len(users),500):
                    Store._recalculate(c,users[offset:offset+500])
                return
        placeholders=','.join('?' for _ in users) if users is not None else ''
        parent_filter=' AND e.parent_uid IN ('+placeholders+')' if users is not None else ''
        profile_filter=' WHERE uid IN ('+placeholders+')' if users is not None else ''
        params=users if users is not None else ()
        aggregates = {r['parent_uid']:(r['matches'],r['evaluated']) for r in c.execute('''
          SELECT e.parent_uid,
          COUNT(DISTINCT CASE WHEN p.verdict='yes' THEN e.child_uid END) AS matches,
          COUNT(DISTINCT CASE WHEN p.verdict IN ('yes','no') THEN e.child_uid END) AS evaluated
          FROM edges e JOIN profiles p ON e.child_uid=p.uid
          WHERE e.parent_uid != e.child_uid'''+parent_filter+' GROUP BY e.parent_uid',params)}
        updates=[]
        for r in c.execute('SELECT uid,base_score,parent_score_snapshot FROM profiles'+profile_filter,params):
            parts=score_parts(r['base_score'],r['parent_score_snapshot'],*aggregates.get(r['uid'],(0,0)))
            updates.append((parts['inherited_score'],parts['discovery_score'],parts['quality_score'],
                            parts['score'],parts['furry_found'],parts['evaluated_found'],r['uid']))
        c.executemany('''UPDATE profiles SET inherited_score=?,discovery_score=?,quality_score=?,
                        score=?,furry_found=?,evaluated_found=? WHERE uid=?''',updates)

    @staticmethod
    def _recalculate_parents(c,user):
        # Inheritance is a discovery-time snapshot. Only direct parents depend
        # on a user's current verdict; unrelated profiles never need rewriting.
        parents=[row['parent_uid'] for row in c.execute(
            'SELECT DISTINCT parent_uid FROM edges WHERE child_uid=? AND parent_uid!=child_uid',(user,))]
        Store._recalculate(c,parents)

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
                (uid,base_score,score,verdict,manual,is_seed,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)''',(user,base,base,verdict,'yes' if verdict=='yes' else None,int(verdict=='yes'),now(),now())).rowcount
        self.event(f'添加'+('确认种子' if verdict=='yes' else '待判定候选')+f' {added} 个，重复 {len(inputs)-added} 个')
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

    def list_profiles(self, view='seeds', page=1, search='', status='', sort='score', limit=30, basis=''):
        if basis not in ('', 'avatar_only'):
            raise ValueError('未知确认依据筛选')
        if basis and view != 'yes':
            raise ValueError('确认依据筛选仅适用于 Furry Yes')
        where,params=[],[]
        if view=='seeds':where.append("is_seed=1 AND verdict='yes' AND traversal NOT IN ('complete','legacy_done')")
        elif view=='candidates':where.append("is_seed=0 AND verdict='unknown'")
        elif view=='yes':where.append("verdict='yes'")
        elif view=='no':where.append("verdict='no'")
        elif view!='all':raise ValueError('未知数据面板')
        if basis == 'avatar_only':
            # A manual approval is already reviewed. Match the saved decision
            # sources exactly instead of guessing from a high avatar score.
            safe_evidence="CASE WHEN json_valid(evidence) THEN evidence ELSE '{}' END"
            where.extend(["automatic='yes'", "manual IS NULL",
                          f"json_type({safe_evidence}, '$.decision_sources')='array'",
                          f"json_array_length({safe_evidence}, '$.decision_sources')=1",
                          f"json_extract({safe_evidence}, '$.decision_sources[0]')='avatar'"])
        if search:
            where.append('(uid LIKE ? OR name LIKE ?)');params.extend(['%'+search[:100]+'%']*2)
        if status in ('pending','partial','complete','failed','legacy_done'):
            where.append('traversal=?');params.append(status)
        elif status=='review':where.append("collection_status='needs_review'")
        clause=' WHERE '+' AND '.join(where) if where else ''
        order={'score':'identity_score DESC,score DESC,created_at ASC,uid ASC','recent':'updated_at DESC,uid ASC',
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
            row=c.execute('SELECT automatic,verdict FROM profiles WHERE uid=?',(user,)).fetchone()
            if row is None:raise ValueError('UID 不存在')
            manual=None if decision=='clear' else decision
            effective=manual or row['automatic']
            status='needs_review' if effective=='unknown' else 'reviewed' if manual else 'classified'
            c.execute('''UPDATE profiles SET manual=?,verdict=?,
                         is_seed=CASE WHEN ?='yes' AND traversal NOT IN ('complete','legacy_done') THEN 1 ELSE 0 END,
                         manual_reason=?,updated_at=?,available_at='',collection_status=?
                         WHERE uid=?''',(manual,effective,effective,str(reason)[:500],now(),status,user))
            if row['verdict']!=effective:self._recalculate_parents(c,user)
        self.event('人工判定：'+{'yes':'Furry Yes','no':'Furry No','clear':'恢复自动结果'}[decision],user=user)

    def requeue(self,user):
        with self.db() as c:
            changed=c.execute('''UPDATE profiles SET traversal='pending',followers_state='pending',
              followings_state='pending',followers_page=1,followings_page=1,available_at='',last_error='',
              completed_at=NULL,is_seed=CASE WHEN verdict='yes' THEN 1 ELSE 0 END,
              updated_at=? WHERE uid=?''',(now(),uid(user))).rowcount
            if not changed:raise ValueError('UID 不存在')
        self.event('重新安排关系遍历（已有关系保留并去重）',user=uid(user))

    def set_base(self,user,base):
        if type(base) not in (float,int) or not math.isfinite(base) or not 0<=base<=20:
            raise ValueError('基础分应在 0–20 之间')
        with self.db() as c:
            if not c.execute('UPDATE profiles SET base_score=?,updated_at=? WHERE uid=?',(base,now(),uid(user))).rowcount:
                raise ValueError('UID 不存在')
            self._recalculate(c,(uid(user),))

    def update_profile(self,user,**fields):
        allowed={'name','intro','avatar_url','image_path','fan_count','follow_count','collection_status',
                 'last_error','available_at','collected_at','traversal','completed_at'}
        if not fields or set(fields)-allowed:raise ValueError('未知用户字段')
        fields['updated_at']=now()
        with self.db() as c:
            c.execute('UPDATE profiles SET '+','.join(k+'=?' for k in fields)+' WHERE uid=?',list(fields.values())+[uid(user)])

    def classification(self,user,automatic,evidence):
        if automatic not in ('yes','no','unknown'):raise ValueError('未知模型结果')
        evidence={**evidence,'decision_policy':'any_strong_v1'}
        evidence.update(identity_score_parts(evidence))
        user=uid(user)
        with self.db() as c:
            before=c.execute('SELECT verdict FROM profiles WHERE uid=?',(user,)).fetchone()
            c.execute('''UPDATE profiles SET automatic=?, verdict=COALESCE(manual,?),
                         is_seed=CASE WHEN COALESCE(manual,?)='yes' AND traversal NOT IN ('complete','legacy_done') THEN 1 ELSE 0 END,
                         identity_score=?,evidence=?,updated_at=?
                         WHERE uid=?''',(automatic,automatic,automatic,evidence['identity_score'],json.dumps(evidence,ensure_ascii=False),now(),user))
            after=c.execute('SELECT verdict FROM profiles WHERE uid=?',(user,)).fetchone()
            if before and after and before['verdict']!=after['verdict']:
                self._recalculate_parents(c,user)

    def finalize_classification(self,user):
        """Commit queue status from the current verdict without overwriting review actions."""
        user=uid(user)
        with self.db() as c:
            c.execute("""UPDATE profiles SET
                collection_status=CASE WHEN manual IS NOT NULL THEN 'reviewed'
                    WHEN verdict='unknown' THEN 'needs_review' ELSE 'classified' END,
                available_at=CASE WHEN verdict='unknown' THEN ? ELSE '' END,
                updated_at=? WHERE uid=?""",(later(3600),now(),user))
        return self.profile(user)

    def relation_page(self,parent,direction,children,page,finished):
        parent=uid(parent)
        if direction not in ('followers','followings'):raise ValueError('未知关系方向')
        children=list(dict.fromkeys(uid(x) for x in children if str(x)!=parent))
        with self.db() as c:
            seed=c.execute('SELECT score,is_seed,verdict FROM profiles WHERE uid=?',(parent,)).fetchone()
            if seed is None:raise ValueError('父级 UID 不存在')
            if not seed['is_seed'] or seed['verdict']!='yes':
                return False
            snapshot=seed['score']
            for child in children:
                parts=score_parts(10,snapshot,0,0)
                c.execute('''INSERT OR IGNORE INTO profiles(uid,is_seed,source,parent_uid,parent_score_snapshot,
                inherited_score,score,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                          (child,0,'discovery',parent,snapshot,parts['inherited_score'],parts['score'],now(),now()))
                c.execute('INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)',(parent,child,direction,snapshot,now()))
            c.execute(f'''UPDATE profiles SET {direction}_page=?,{direction}_state=?,traversal='partial',updated_at=? WHERE uid=?''',
                      (page+1,'complete' if finished else 'partial',now(),parent))
            self._recalculate(c,(parent,))
        return True

    def finish_traversal(self,user):
        row=self.profile(user)
        complete=all(row[k+'_state'] in ('complete','unavailable') for k in ('followers','followings'))
        restricted=[name for key,name in (('followers','粉丝'),('followings','关注')) if row[key+'_state']=='unavailable']
        error=row['last_error']
        if complete:
            error=('、'.join(restricted)+'列表因用户隐私不可获取；仅处理已公开获取的关联用户') if restricted else ''
        with self.db() as c:
            c.execute("""UPDATE profiles SET traversal=?,completed_at=?,last_error=?,updated_at=?,
                         is_seed=CASE WHEN ? OR verdict!='yes' THEN 0 ELSE 1 END WHERE uid=?""",
                      ('complete' if complete else 'partial',now() if complete else None,error,now(),complete,uid(user)))
            if complete:
                # Relations are complete, but this seed's discovered users must
                # finish automatic assessment before another seed is selected.
                if c.execute("SELECT 1 FROM settings WHERE key='selected_seed_uid' AND value=?",(json.dumps(uid(user)),)).fetchone():
                    c.execute("INSERT OR REPLACE INTO settings VALUES('selected_seed_phase',?)",(json.dumps('classify_candidates'),))
        return complete

    def relation_failure(self,user,direction,message):
        if direction not in ('followers','followings'):raise ValueError('未知方向')
        with self.db() as c:
            c.execute(f'''UPDATE profiles SET {direction}_state='failed',traversal='partial',
                         last_error=?,updated_at=? WHERE uid=?''',(message,now(),uid(user)))

    def relation_unavailable(self,user,direction,message):
        if direction not in ('followers','followings'):raise ValueError('未知方向')
        with self.db() as c:
            c.execute(f'''UPDATE profiles SET {direction}_state='unavailable',traversal='partial',
                         last_error=?,updated_at=? WHERE uid=?''',(message,now(),uid(user)))

    @staticmethod
    def _pending_candidate_count(c,runnable=False):
        if runnable:
            return c.execute('SELECT COUNT(*) FROM profiles INDEXED BY profiles_candidate_retry WHERE '+
                             CANDIDATE_WHERE+' AND available_at<=?',(now(),)).fetchone()[0]
        return c.execute('SELECT COUNT(*) FROM profiles INDEXED BY profiles_candidate_retry WHERE '+CANDIDATE_WHERE).fetchone()[0]

    @staticmethod
    def _candidate_pending(c):
        return c.execute('SELECT 1 FROM profiles INDEXED BY profiles_candidate_retry WHERE '+
                         CANDIDATE_WHERE+' LIMIT 1').fetchone() is not None

    @staticmethod
    def _next_candidate(c,exclude):
        excluded=tuple(set(exclude))
        condition=' AND uid NOT IN ('+','.join('?' for _ in excluded)+')' if excluded else ''
        row=c.execute('SELECT uid FROM profiles INDEXED BY profiles_candidate_fifo WHERE '+
                      CANDIDATE_WHERE+' AND available_at<=?'+condition+
                      ' ORDER BY created_at ASC,uid ASC LIMIT 1',(now(),*excluded)).fetchone()
        return row['uid'] if row else None

    @staticmethod
    def _collection_scope(c):
        setting=c.execute("SELECT value FROM settings WHERE key='collection_scope'").fetchone()
        return json.loads(setting['value']) if setting else DEFAULT_CONFIG['collection_scope']

    def pending_candidates(self):
        with self.db() as c:return self._pending_candidate_count(c)

    def expansion_allowed(self):
        with self.db() as c:
            if self._collection_scope(c)=='candidates_first' and self._candidate_pending(c):return False
            selected=self._selected_cycle(c)
            if selected:
                return self._cycle_phase(selected)=='expand_seed' and (not selected['available_at'] or selected['available_at']<=now())
            return c.execute("""SELECT 1 FROM profiles WHERE is_seed=1 AND verdict='yes'
                AND traversal NOT IN ('complete','legacy_done') AND collection_status!='needs_review'
                AND (available_at='' OR available_at<=?) LIMIT 1""",(now(),)).fetchone() is not None

    @staticmethod
    def _selected_cycle(c):
        selected=c.execute("SELECT value FROM settings WHERE key='selected_seed_uid'").fetchone()
        if not selected:return None
        selected_uid=json.loads(selected['value'])
        # Completed relation traversal remains a cycle until its children finish.
        return c.execute("""SELECT * FROM profiles WHERE uid=? AND verdict='yes'
            AND (is_seed=1 OR (followers_state IN ('complete','unavailable')
                AND followings_state IN ('complete','unavailable')))""",(selected_uid,)).fetchone()

    @staticmethod
    def _cycle_phase(row):
        return 'classify_candidates' if all(row[k+'_state'] in ('complete','unavailable') for k in ('followers','followings')) else 'expand_seed'

    @staticmethod
    def _cycle_pending(c,parent,runnable=False):
        where="""e.parent_uid=? AND e.child_uid!=e.parent_uid AND p.is_seed=0 AND p.verdict='unknown'
            AND p.traversal NOT IN ('complete','legacy_done') AND p.collection_status!='needs_review'"""
        params=[parent]
        if runnable:
            where+=" AND (p.available_at='' OR p.available_at<=?)";params.append(now())
        return c.execute('SELECT DISTINCT p.uid,p.identity_score,p.score,p.created_at FROM edges e JOIN profiles p ON p.uid=e.child_uid WHERE '+where+
                         ' ORDER BY p.identity_score DESC,p.score DESC,p.created_at ASC,p.uid ASC',params).fetchall()

    @staticmethod
    def _cycle_pending_count(c,parent,runnable=False):
        where="""e.parent_uid=? AND e.child_uid!=e.parent_uid AND p.is_seed=0 AND p.verdict='unknown'
            AND p.traversal NOT IN ('complete','legacy_done') AND p.collection_status!='needs_review'"""
        params=[parent]
        if runnable:
            where+=' AND p.available_at<=?';params.append(now())
        return c.execute('SELECT COUNT(DISTINCT p.uid) FROM edges e JOIN profiles p ON p.uid=e.child_uid WHERE '+where,
                         params).fetchone()[0]

    @staticmethod
    def _clear_cycle(c):
        c.execute("DELETE FROM settings WHERE key IN ('selected_seed_uid','selected_seed_phase')")

    def next_seed(self,exclude,candidates_only=False):
        """Select one task, preserving seed cycles while the global backlog drains."""
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if self._collection_scope(c)=='candidates_first':
                candidate=self._next_candidate(c,exclude)
                if candidate is not None:return candidate
                # Delayed retries retain priority, and a candidate whose verdict
                # was just committed may still be running in another worker.
                if candidates_only or self._candidate_pending(c):return None
            selected=self._selected_cycle(c)
            if selected:
                phase=self._cycle_phase(selected)
                c.execute("INSERT OR REPLACE INTO settings VALUES('selected_seed_phase',?)",(json.dumps(phase),))
                if phase=='expand_seed':
                    if candidates_only or selected['available_at'] and selected['available_at']>now():return None
                    return selected['uid'] if selected['uid'] not in exclude else None
                pending=self._cycle_pending(c,selected['uid'])
                if pending:
                    candidates=self._cycle_pending(c,selected['uid'],True)
                    return next((r['uid'] for r in candidates if r['uid'] not in exclude),None)
                # In-flight children may have committed a verdict but still be
                # running. Only an idle coordinator may close or start a cycle.
                if candidates_only:return None
                self._clear_cycle(c)
                c.execute('INSERT INTO events(time,level,message,uid) VALUES(?,?,?,?)',
                          (now(),'info','本种子的粉丝、关注及关联用户自动判定已完成，准备选择下一最高分种子',selected['uid']))
            else:
                self._clear_cycle(c)
            row=c.execute("""SELECT uid FROM profiles WHERE is_seed=1 AND verdict='yes'
                AND traversal NOT IN ('complete','legacy_done') AND collection_status!='needs_review'
                AND (available_at='' OR available_at<=?)
                ORDER BY identity_score DESC,score DESC,created_at ASC,uid ASC LIMIT 1""",(now(),)).fetchone()
            if row:
                if candidates_only:return None
                c.execute("INSERT OR REPLACE INTO settings VALUES('selected_seed_uid',?)",(json.dumps(row['uid']),))
                c.execute("INSERT OR REPLACE INTO settings VALUES('selected_seed_phase',?)",(json.dumps('expand_seed'),))
                return row['uid'] if row['uid'] not in exclude else None
            # Bootstrap only when there is no executable confirmed seed. A large
            # unrelated candidate backlog must never starve the seed cycle.
            candidates=c.execute("""SELECT uid FROM profiles WHERE is_seed=0 AND verdict='unknown'
                AND traversal NOT IN ('complete','legacy_done') AND collection_status!='needs_review'
                AND (available_at='' OR available_at<=?) ORDER BY created_at ASC,uid ASC""",(now(),)).fetchall()
            return next((r['uid'] for r in candidates if r['uid'] not in exclude),None)

    def account_add(self,label,secret):
        secret=normalize_cookie(secret)
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

    def account_delete(self,ident):
        if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident):
            raise ValueError('账号标识无效')
        with self.db() as c:
            if not c.execute('DELETE FROM accounts WHERE id=?',(ident,)).rowcount:
                raise ValueError('账号不存在或已删除')
            c.execute('INSERT INTO events(time,level,message) VALUES(?,?,?)',
                      (now(),'info','删除 Cookie 账号（已采集数据保留）'))

    def account_secret(self,ident):
        with self.db() as c:
            row=c.execute('SELECT secret FROM accounts WHERE id=?',(ident,)).fetchone()
            if row is None:raise ValueError('账号不存在')
            return row['secret']

    def account_result(self,ident,status,name=''):
        with self.db() as c:
            c.execute('UPDATE accounts SET status=?,account_name=?,checked_at=? WHERE id=?',(status,name[:100],now(),ident))

    def next_account(self,exclude=()):
        excluded=tuple(set(exclude))
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            condition=(' AND id NOT IN ('+','.join('?' for _ in excluded)+')') if excluded else ''
            r=c.execute("SELECT id FROM accounts WHERE enabled=1 AND status!='expired'"+condition+
                        " ORDER BY COALESCE(used_at,''),created_at,id LIMIT 1",excluded).fetchone()
            if r:
                c.execute('UPDATE accounts SET used_at=?,request_count=request_count+1 WHERE id=?',(now(),r['id']))
            return r['id'] if r else None

    def cooldown(self,seconds):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            until=later(seconds)
            previous=c.execute("SELECT value FROM settings WHERE key='cooldown_until'").fetchone()
            if previous:
                previous=json.loads(previous['value'])
                if isinstance(previous,str) and previous>until:until=previous
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('cooldown_until',json.dumps(until)))

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
            c.execute('BEGIN')
            counts={r['verdict']:r['n'] for r in c.execute('SELECT verdict,COUNT(*) n FROM profiles GROUP BY verdict')}
            counts['seeds']=c.execute("SELECT COUNT(*) FROM profiles WHERE is_seed=1 AND verdict='yes' AND traversal NOT IN ('complete','legacy_done')").fetchone()[0]
            counts['candidates']=c.execute("SELECT COUNT(*) FROM profiles WHERE is_seed=0 AND verdict='unknown'").fetchone()[0]
            counts['complete']=c.execute("SELECT COUNT(*) FROM profiles WHERE traversal IN ('complete','legacy_done')").fetchone()[0]
            counts['restricted']=c.execute("""SELECT COUNT(*) FROM profiles WHERE traversal IN ('complete','legacy_done')
                AND (followers_state='unavailable' OR followings_state='unavailable')""").fetchone()[0]
            counts['review']=c.execute("SELECT COUNT(*) FROM profiles WHERE collection_status='needs_review'").fetchone()[0]
            counts['pending_candidates']=self._pending_candidate_count(c)
            counts['total']=sum(counts.get(k,0) for k in ('yes','no','unknown'))
            job=c.execute('SELECT * FROM jobs ORDER BY started_at DESC LIMIT 1').fetchone()
            events=[dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 25')]
            leaders=[dict(r) for r in c.execute("SELECT uid,name,score,furry_found FROM profiles WHERE verdict='yes' ORDER BY score DESC LIMIT 5")]
            histogram=[0]*5
            for r in c.execute("SELECT MIN(4,CAST(score/20 AS INTEGER)) bucket,COUNT(*) n FROM profiles WHERE verdict!='no' GROUP BY bucket"):
                histogram[r['bucket']]=r['n']
            relation_count=c.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
            reference_count=c.execute('SELECT COUNT(DISTINCT uid) FROM reference_profiles').fetchone()[0]
            collection_scope=self._collection_scope(c)
            runnable=self._pending_candidate_count(c,True)
            selected=self._selected_cycle(c)
            selected_uid=selected['uid'] if selected else None
            seed_ready=c.execute("""SELECT 1 FROM profiles WHERE is_seed=1 AND verdict='yes'
                AND traversal NOT IN ('complete','legacy_done') AND collection_status!='needs_review'
                AND (available_at='' OR available_at<=?) LIMIT 1""",(now(),)).fetchone()
            phase=self._cycle_phase(selected) if selected else None
            cycle_total=cycle_pending=cycle_runnable=cycle_review=0
            wait_until=''
            if selected:
                cycle_total=c.execute('SELECT COUNT(DISTINCT child_uid) FROM edges WHERE parent_uid=? AND child_uid!=parent_uid',(selected_uid,)).fetchone()[0]
                cycle_pending=self._cycle_pending_count(c,selected_uid)
                cycle_runnable=self._cycle_pending_count(c,selected_uid,True)
                cycle_review=c.execute("""SELECT COUNT(DISTINCT p.uid) FROM edges e JOIN profiles p ON p.uid=e.child_uid
                    WHERE e.parent_uid=? AND e.child_uid!=e.parent_uid AND p.verdict='unknown' AND p.collection_status='needs_review'""",(selected_uid,)).fetchone()[0]
                if phase=='expand_seed':
                    wait_until=selected['available_at']
                elif cycle_pending and not cycle_runnable:
                    delayed=c.execute("""SELECT MIN(p.available_at) FROM edges e JOIN profiles p ON p.uid=e.child_uid
                        WHERE e.parent_uid=? AND p.is_seed=0 AND p.verdict='unknown'
                        AND p.traversal NOT IN ('complete','legacy_done') AND p.collection_status!='needs_review'
                        AND p.available_at>?""",(selected_uid,now())).fetchone()[0]
                    wait_until=delayed or ''
                mode=('expand_seed' if not wait_until or wait_until<=now() else 'waiting') if phase=='expand_seed' else ('classify_candidates' if cycle_runnable or not cycle_pending else 'waiting')
            else:
                mode='expand_seed' if seed_ready else 'bootstrap_candidates' if runnable else 'waiting'
            candidate_wait_until=''
            if collection_scope=='candidates_first' and counts['pending_candidates']:
                mode='candidate_backlog' if runnable else 'waiting'
                if not runnable:
                    candidate_wait_until=c.execute('SELECT MIN(available_at) FROM profiles INDEXED BY profiles_candidate_retry WHERE '+
                                                   CANDIDATE_WHERE).fetchone()[0] or ''
            queue={'pending_candidates':counts['pending_candidates'],'runnable_candidates':runnable,
                   'collection_scope':collection_scope,'candidate_wait_until':candidate_wait_until,
                   'selected_seed_uid':selected_uid,'mode':mode,'cycle_phase':phase,
                   'cycle_total_candidates':cycle_total,'cycle_pending_candidates':cycle_pending,
                   'cycle_runnable_candidates':cycle_runnable,'cycle_review_candidates':cycle_review,
                   'cycle_checked_candidates':cycle_total-cycle_pending,
                   'cycle_restricted_directions':[key for key in ('followers','followings') if selected and selected[key+'_state']=='unavailable'],
                   'selected_seed_wait_until':wait_until}
        return {'counts':counts,'job':dict(job) if job else None,'events':events,'leaders':leaders,
                'histogram':histogram,'relation_count':relation_count,'reference_count':reference_count,'queue':queue,'accounts':self.accounts(),
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
        except (ValueError,OSError):raise ValueError('cookies.json 不是可读取的 UTF-8 JSON 文件，请检查文件格式') from None
        # Some Cookie exports group cookie-name/value maps by account.
        # Keep the existing string-array import and accept a single field map too.
        if isinstance(values,dict):
            values=[values] if 'SESSDATA' in values else list(values.values())
        if not isinstance(values,list):
            raise ValueError('cookies.json 须为 Cookie 字符串数组、Cookie 字段对象或按账号分组的对象')
        added=0;skipped=0
        for i,value in enumerate(values):
            try:
                if isinstance(value,dict):
                    if not all(isinstance(k,str) and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",k)
                               and isinstance(v,str) and ';' not in v for k,v in value.items()):
                        raise ValueError('Cookie 字段对象必须使用有效字段名与文本值')
                    value='; '.join(f'{k}={v}' for k,v in value.items())
                self.account_add(f'导入账号 {i+1}',value);added+=1
            except ValueError:skipped+=1
        return {'added':added,'skipped':skipped}
