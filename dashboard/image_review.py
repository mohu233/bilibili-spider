"""Stable image review rounds and atomic, replay-safe manual decisions."""
import hashlib
import io
import threading
import time
import uuid
from pathlib import Path

from .store import Store, now, uid

_UNSET = object()


class ImageReviewConflict(ValueError):
    code = 'review_conflict'


class ImageReviewService:
    MAX_SESSIONS = 16
    SESSION_SECONDS = 4 * 3600
    MAX_BATCHES = 256

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.sessions = {}
        self.batches = {}

    @staticmethod
    def _scope(scope):
        if scope is None:
            scope = {}
        if not isinstance(scope, dict):
            raise ValueError('审核范围应为对象')
        view = scope.get('view', 'candidates')
        mode = scope.get('mode', 'avatar_only' if view == 'yes' else 'review')
        if view not in ('candidates', 'yes') or mode not in (
                ('avatar_only',) if view == 'yes' else ('review', 'all')):
            raise ValueError('未知图片审核范围')
        search, status, sort = (scope.get(key, default) for key, default in
                                (('q', ''), ('status', ''), ('sort', 'score')))
        if not all(isinstance(value, str) for value in (search, status, sort)):
            raise ValueError('审核筛选参数无效')
        if status not in ('', 'pending', 'partial', 'complete', 'failed', 'legacy_done', 'review'):
            raise ValueError('未知审核状态')
        if sort not in ('score', 'recent', 'discoveries'):
            raise ValueError('未知审核排序')
        return {'view': view, 'mode': mode, 'q': search[:100], 'status': status, 'sort': sort}

    @staticmethod
    def _query(scope):
        clauses = ['manual IS NULL']
        params = []
        if scope['view'] == 'candidates':
            clauses.append("is_seed=0 AND verdict='unknown'")
            if scope['mode'] == 'review':
                clauses.append("collection_status='needs_review'")
        else:
            evidence = "CASE WHEN json_valid(evidence) THEN evidence ELSE '{}' END"
            clauses.extend(["verdict='yes' AND automatic='yes'",
                            f"json_type({evidence},'$.decision_sources')='array'",
                            f"json_array_length({evidence},'$.decision_sources')=1",
                            f"json_extract({evidence},'$.decision_sources[0]')='avatar'"])
        if scope['q']:
            clauses.append('(uid LIKE ? OR name LIKE ?)')
            params += ['%' + scope['q'] + '%'] * 2
        if scope['status'] == 'review':
            clauses.append("collection_status='needs_review'")
        elif scope['status']:
            clauses.append('traversal=?')
            params.append(scope['status'])
        order = {'score': 'identity_score DESC,score DESC,created_at ASC,uid ASC',
                 'recent': 'updated_at DESC,uid ASC',
                 'discoveries': 'furry_found DESC,score DESC,uid ASC'}[scope['sort']]
        return ' AND '.join(clauses), params, order

    def _image_hash(self, row):
        path = (self.store.root / row['image_path']).resolve()
        allowed = [self.store.root / name for name in ('data', 'hit', 'miss', 'avatars', '_cache')]
        if (not any(path.is_relative_to(directory) for directory in allowed)
                or path.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.img')
                or not path.is_file() or path.stat().st_size > 10_000_000):
            raise ValueError('没有可用头像')
        data = path.read_bytes()
        if len(data) > 10_000_000:
            raise ValueError('头像过大')
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 20_000_000:
                    raise ValueError('头像像素过大')
                image.verify()
        except Image.DecompressionBombError:
            raise ValueError('头像像素过大') from None
        return hashlib.sha256(data).hexdigest()

    def _cleanup(self):
        current = time.monotonic()
        expired = [key for key, value in self.sessions.items()
                   if current - value['touched'] > self.SESSION_SECONDS]
        for key in expired:
            self.sessions.pop(key, None)
        for key in list(self.batches):
            if self.batches[key]['session_id'] not in self.sessions:
                self.batches.pop(key, None)
        while len(self.batches) > self.MAX_BATCHES:
            completed = next((key for key, value in self.batches.items() if value['result'] is not None), None)
            if completed is None:
                break
            self.batches.pop(completed)

    @staticmethod
    def _response(session_id, session, batch=None):
        return {'session_id': session_id, 'batch_id': batch['id'] if batch else None,
                'items': [dict(item) for item in batch['items']] if batch else [],
                'total': session['total'],
                'remaining': max(0, session['total'] - (batch['next_position'] if batch else session['position'])),
                'missing_images': session['missing_images'],
                'done': batch is None}

    def page(self, session_id=None, scope=None, limit=32):
        if type(limit) is not int or not 1 <= limit <= 96:
            raise ValueError('每页图片数量必须为 1–96 的整数')
        normalized = self._scope(scope) if scope is not None or session_id is None else None
        with self.lock:
            self._cleanup()
            if session_id is None:
                if len(self.sessions) >= self.MAX_SESSIONS:
                    oldest = min(self.sessions, key=lambda key: self.sessions[key]['touched'])
                    self.sessions.pop(oldest)
                    self._cleanup()
                where, params, order = self._query(normalized)
                with self.store.db() as connection:
                    users = [row['uid'] for row in connection.execute(
                        'SELECT uid FROM profiles WHERE ' + where + " AND image_path!='' ORDER BY " + order, params)]
                    without_image = connection.execute('SELECT COUNT(*) FROM profiles WHERE ' + where +
                                                       " AND image_path=''", params).fetchone()[0]
                session_id = uuid.uuid4().hex
                session = {'scope': normalized, 'uids': users, 'total': len(users), 'position': 0,
                           'pending': None, 'missing_images': without_image, 'touched': time.monotonic()}
                self.sessions[session_id] = session
            else:
                if not isinstance(session_id, str) or session_id not in self.sessions:
                    raise ValueError('本轮图片审核已过期，请重新开始')
                session = self.sessions[session_id]
                if normalized is not None and normalized != session['scope']:
                    raise ValueError('审核范围已变化，请重新开始这一轮')
                session['touched'] = time.monotonic()
            if session['pending']:
                batch = self.batches[session['pending']]
                if batch.get('conflicted'):
                    raise ImageReviewConflict('本页数据已变化，请重新开始这一轮并复核图片')
                return self._response(session_id, session, batch)
            where, params, _ = self._query(session['scope'])
            position = session['position']
            snapshots, items = {}, []
            with self.store.db() as connection:
                while position < session['total'] and len(items) < limit:
                    user = session['uids'][position]
                    position += 1
                    row = connection.execute(
                        'SELECT uid,name,image_path,avatar_url,manual,manual_reason FROM profiles WHERE uid=? AND ' + where,
                        [user] + params).fetchone()
                    if row is None:
                        continue
                    row = dict(row)
                    try:
                        digest = self._image_hash(row)
                    except (OSError, ValueError, SyntaxError):
                        session['missing_images'] += 1
                        continue
                    snapshots[user] = {key: row[key] for key in ('manual', 'manual_reason', 'image_path', 'avatar_url')}
                    snapshots[user]['image_hash'] = digest
                    items.append({'uid': user, 'name': row['name'], 'image_url': '/avatar/' + user + '?v=' + digest})
            if not items:
                session['position'] = position
                return self._response(session_id, session)
            batch_id = uuid.uuid4().hex
            batch = {'id': batch_id, 'session_id': session_id, 'items': items,
                     'snapshots': snapshots, 'next_position': position, 'result': None}
            self.batches[batch_id] = batch
            session['pending'] = batch_id
            return self._response(session_id, session, batch)

    @staticmethod
    def _ids(values, allowed):
        if not isinstance(values, list) or len(values) > 96:
            raise ValueError('图片选择应为 UID 数组')
        result = [uid(value) for value in values]
        if len(set(result)) != len(result) or not set(result).issubset(allowed):
            raise ValueError('存在重复或不属于本页的 UID')
        return set(result)

    def commit(self, batch_id, excluded=_UNSET, deferred=_UNSET):
        with self.lock:
            self._cleanup()
            if not isinstance(batch_id, str) or batch_id not in self.batches:
                raise ValueError('图片审核页已过期，请重新开始')
            batch = self.batches[batch_id]
            session = self.sessions[batch['session_id']]
            allowed = set(batch['snapshots'])
            excluded = self._ids([] if excluded is _UNSET else excluded, allowed)
            deferred = self._ids([] if deferred is _UNSET else deferred, allowed)
            if excluded & deferred:
                raise ValueError('同一图片不能同时排除和暂缓')
            choice = (frozenset(excluded), frozenset(deferred))
            if batch['result'] is not None:
                if choice != batch['choice']:
                    raise ValueError('本页已提交，不能更改已提交的选择')
                return dict(batch['result'], replayed=True)
            if batch.get('conflicted'):
                raise ImageReviewConflict('本页数据已变化，请重新开始这一轮并复核图片')
            confirmed = allowed - excluded - deferred
            decisions = {user: 'no' if user in excluded else 'yes' for user in allowed - deferred}
            stamp = now()
            with self.store.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                previous = {}
                for user in decisions:
                    row = connection.execute(
                        'SELECT uid,manual,manual_reason,image_path,avatar_url,verdict FROM profiles WHERE uid=?', (user,)).fetchone()
                    expected = batch['snapshots'][user]
                    conflict = row is None or any(row[key] != expected[key] for key in
                                                 ('manual', 'manual_reason', 'image_path', 'avatar_url'))
                    if not conflict:
                        try:
                            conflict = self._image_hash(row) != expected['image_hash']
                        except (OSError, ValueError, SyntaxError):
                            conflict = True
                    if conflict:
                        batch['conflicted'] = True
                        raise ImageReviewConflict('本页图片或人工判定已变化，整页尚未提交；请重新开始这一轮')
                    previous[user] = row['verdict']
                changed = []
                for user, decision in decisions.items():
                    reason = '图片批量审核：人工' + ('确认 Furry' if decision == 'yes' else '排除')
                    connection.execute("""UPDATE profiles SET manual=?,verdict=?,
                        is_seed=CASE WHEN ?='yes' AND traversal NOT IN ('complete','legacy_done') THEN 1 ELSE 0 END,
                        manual_reason=?,updated_at=?,available_at='',collection_status='reviewed' WHERE uid=?""",
                        (decision, decision, decision, reason, stamp, user))
                    if previous[user] != decision:
                        changed.append(user)
                if changed:
                    parents = [row['parent_uid'] for row in connection.execute(
                        'SELECT DISTINCT parent_uid FROM edges WHERE parent_uid!=child_uid AND child_uid IN (' +
                        ','.join('?' for _ in changed) + ')', changed)]
                    Store._recalculate(connection, parents)
                if decisions:
                    connection.execute('INSERT INTO events(time,level,message) VALUES(?,?,?)',
                        (stamp, 'info', f'图片批量人工审核：确认 {len(confirmed)} 个，排除 {len(excluded)} 个，暂缓 {len(deferred)} 个'))
            session['position'] = batch['next_position']
            session['pending'] = None
            session['touched'] = time.monotonic()
            result = {'confirmed': len(confirmed), 'excluded': len(excluded), 'deferred': len(deferred),
                      'processed': len(decisions), 'remaining': max(0, session['total'] - session['position']),
                      'replayed': False}
            batch['result'], batch['choice'] = result, choice
            return dict(result)
