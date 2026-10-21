"""Isolated image-batch review tests; every database and avatar is a fixture."""
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from .collector import Collector
from .image_review import ImageReviewConflict, ImageReviewService
from .store import score_parts
from .tests import FakeClassifier, FakeClient, Fixture
from .web import DashboardServer


class ImageReviewFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.service = ImageReviewService(self.store)

    def avatar(self, user, color='red', status='needs_review', name=None):
        user = str(user)
        try:
            self.store.profile(user)
        except ValueError:
            self.store.add_seeds(user)
        path = self.store.directory / 'avatars' / (user + '.img')
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (24, 24), color).save(path, format='PNG')
        self.store.update_profile(user, image_path=str(path.relative_to(self.root)),
                                  avatar_url='https://i0.hdslb.com/bfs/face/' + user + '.png',
                                  collection_status=status, name=name or 'Fixture ' + user)
        return path

    def avatar_users(self, users):
        for user in users:
            self.avatar(user)

    @staticmethod
    def ids(page):
        return [item['uid'] for item in page['items']]

    def assert_unchanged(self, before):
        for user, original in before.items():
            self.assertEqual(self.store.profile(user), original, user)


class ImageReviewServiceTests(ImageReviewFixture):
    def test_page_contains_only_images_and_stable_batch_id(self):
        self.avatar_users(('1', '2', '3'))
        page = self.service.page(limit=2)
        again = self.service.page(session_id=page['session_id'], limit=2)
        self.assertEqual(page, again)
        self.assertEqual(len(page['items']), 2)
        self.assertTrue(page['session_id'])
        self.assertTrue(page['batch_id'])
        for item in page['items']:
            self.assertTrue({'uid', 'name', 'image_url'}.issubset(item))
            self.assertTrue(item['image_url'].startswith('/avatar/'))
        self.assertFalse(page['done'])

    def test_excluded_no_unmarked_yes_deferred_untouched_then_next_page(self):
        self.avatar_users(('1', '2', '3', '4'))
        page = self.service.page(limit=3)
        yes, no, deferred = self.ids(page)
        deferred_before = self.store.profile(deferred)
        result = self.service.commit(page['batch_id'], excluded=[no], deferred=[deferred])
        self.assertEqual((result['confirmed'], result['excluded'], result['deferred'], result['processed']),
                         (1, 1, 1, 2))
        self.assertFalse(result['replayed'])
        for user, verdict, seed in ((yes, 'yes', 1), (no, 'no', 0)):
            row = self.store.profile(user)
            self.assertEqual((row['manual'], row['verdict'], row['is_seed'], row['collection_status']),
                             (verdict, verdict, seed, 'reviewed'))
        self.assertEqual(self.store.profile(deferred), deferred_before)
        next_page = self.service.page(session_id=page['session_id'], limit=3)
        self.assertEqual(set(self.ids(next_page)), {'1', '2', '3', '4'} - {yes, no, deferred})
        self.service.commit(next_page['batch_id'])
        end = self.service.page(session_id=page['session_id'], limit=3)
        self.assertTrue(end['done'])
        self.assertEqual(end['items'], [])
        self.assertEqual(self.store.profile(deferred), deferred_before)
        # A new review round is allowed to show the deferred user again.
        self.assertIn(deferred, self.ids(self.service.page()))

    def test_all_deferred_writes_nothing_and_does_not_reappear_this_round(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        before = {user: self.store.profile(user) for user in self.ids(page)}
        result = self.service.commit(page['batch_id'], deferred=self.ids(page))
        self.assertEqual((result['confirmed'], result['excluded'], result['deferred'], result['processed']),
                         (0, 0, 2, 0))
        self.assert_unchanged(before)
        self.assertTrue(self.service.page(session_id=page['session_id'])['done'])

    def test_commit_replay_is_idempotent_and_changed_payload_rejected(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        excluded = [self.ids(page)[0]]
        first = self.service.commit(page['batch_id'], excluded=excluded)
        before = {user: self.store.profile(user) for user in self.ids(page)}
        replay = self.service.commit(page['batch_id'], excluded=excluded)
        self.assertTrue(replay['replayed'])
        self.assertEqual({k: v for k, v in first.items() if k != 'replayed'},
                         {k: v for k, v in replay.items() if k != 'replayed'})
        self.assert_unchanged(before)
        with self.assertRaises(ValueError):
            self.service.commit(page['batch_id'], excluded=[])
        self.assert_unchanged(before)

    def test_snapshot_excludes_new_candidates_until_next_round(self):
        self.avatar_users(('1', '2'))
        page = self.service.page(limit=1)
        self.avatar('3')
        seen = []
        while not page['done']:
            seen += self.ids(page)
            self.service.commit(page['batch_id'], deferred=self.ids(page))
            page = self.service.page(session_id=page['session_id'], limit=1)
        self.assertEqual(set(seen), {'1', '2'})
        self.assertEqual(len(seen), 2)
        self.assertIn('3', self.ids(self.service.page()))

    def test_simultaneous_retries_apply_only_once(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        barrier = threading.Barrier(3)
        results, failures = [], []

        def commit():
            try:
                barrier.wait(timeout=5)
                results.append(self.service.commit(page['batch_id'], excluded=['1']))
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=commit) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=5)
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(sorted(result['replayed'] for result in results), [False, True])
        self.assertTrue(all(result['processed'] == 2 for result in results))
        self.assertEqual(self.store.profile('1')['manual'], 'no')
        self.assertEqual(self.store.profile('2')['manual'], 'yes')

    def test_default_only_reviews_and_all_mode_also_includes_pending(self):
        self.avatar('1')
        self.avatar('2', status='pending')
        self.avatar('3'); self.store.decision('3', 'yes')
        self.avatar('4'); self.store.decision('4', 'no')
        self.assertEqual(self.ids(self.service.page()), ['1'])
        all_page = self.service.page(scope={'view': 'candidates', 'mode': 'all'})
        self.assertEqual(set(self.ids(all_page)), {'1', '2'})

    def test_missing_corrupt_and_outside_images_are_never_confirmed(self):
        valid = self.avatar('1')
        self.avatar('2').unlink()
        self.avatar('3').write_bytes(b'not an image')
        self.avatar('4')
        self.store.update_profile('4', image_path='../../outside.png')
        self.store.add_seeds('5')
        self.store.update_profile('5', collection_status='needs_review')
        before = {user: self.store.profile(user) for user in ('2', '3', '4', '5')}
        page = self.service.page()
        self.assertEqual(self.ids(page), ['1'])
        self.assertGreaterEqual(page['missing_images'], 4)
        self.service.commit(page['batch_id'])
        self.assertEqual(self.store.profile('1')['manual'], 'yes')
        self.assert_unchanged(before)
        self.assertTrue(valid.is_file())

    def test_excessive_pixel_dimensions_are_skipped_without_confirmation(self):
        self.avatar('1')
        before = {'1': self.store.profile('1')}
        # Lower Pillow's limit rather than constructing a resource-heavy file.
        with patch.object(Image, 'MAX_IMAGE_PIXELS', 100):
            page = self.service.page()
        self.assertEqual(page['items'], [])
        self.assertTrue(page['done'])
        self.assertEqual(page['missing_images'], 1)
        self.assert_unchanged(before)

    def test_invalid_limits_and_scopes_do_not_write(self):
        self.avatar('1')
        before = {'1': self.store.profile('1')}
        for limit in (True, False, 0, -1, 97, 1.5, '32', None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.service.page(limit=limit)
        for scope in ({'view': 'no'}, {'view': 'yes', 'mode': 'all'},
                      {'view': 'candidates', 'mode': 'avatar_only'}):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                self.service.page(scope=scope)
        self.assert_unchanged(before)

    def test_invalid_uid_selections_never_partially_commit(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        user = self.ids(page)[0]
        before = {uid: self.store.profile(uid) for uid in self.ids(page)}
        selections = [([user, user], []), ([user], [user]), (['999'], []),
                      ([], ['999']), ([True], []), ('1', []), ([], None)]
        for excluded, deferred in selections:
            with self.subTest(excluded=excluded, deferred=deferred), self.assertRaises(ValueError):
                self.service.commit(page['batch_id'], excluded=excluded, deferred=deferred)
            self.assert_unchanged(before)
        # Bad selections must not consume the pending batch.
        self.assertEqual(self.service.page(session_id=page['session_id']), page)
        self.assertEqual(self.service.commit(page['batch_id'])['confirmed'], 2)

    def test_other_window_manual_decision_causes_atomic_conflict(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        unchanged, changed = self.ids(page)
        self.store.decision(changed, 'no', 'Other window')
        before = {user: self.store.profile(user) for user in self.ids(page)}
        with self.assertRaises(ImageReviewConflict):
            self.service.commit(page['batch_id'])
        self.assert_unchanged(before)
        self.assertIsNone(self.store.profile(unchanged)['manual'])

    def test_avatar_url_change_causes_atomic_conflict_even_if_file_same(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        self.store.update_profile(self.ids(page)[-1], avatar_url='https://i1.hdslb.com/new.png')
        before = {user: self.store.profile(user) for user in self.ids(page)}
        with self.assertRaises(ImageReviewConflict):
            self.service.commit(page['batch_id'])
        self.assert_unchanged(before)

    def test_avatar_bytes_change_causes_atomic_conflict(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        self.avatar(self.ids(page)[-1], color='blue')
        before = {user: self.store.profile(user) for user in self.ids(page)}
        with self.assertRaises(ImageReviewConflict):
            self.service.commit(page['batch_id'])
        self.assert_unchanged(before)

    def test_avatar_disappearing_after_display_causes_atomic_conflict(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        path = self.root / self.store.profile(self.ids(page)[-1])['image_path']
        path.unlink()
        before = {user: self.store.profile(user) for user in self.ids(page)}
        with self.assertRaises(ImageReviewConflict):
            self.service.commit(page['batch_id'])
        self.assert_unchanged(before)

    def test_manual_batch_result_overrides_late_automatic_classification(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        yes, no = self.ids(page)
        self.service.commit(page['batch_id'], excluded=[no])
        self.store.classification(yes, 'no', {'decision_sources': []})
        self.store.classification(no, 'yes', {'decision_sources': ['avatar']})
        for user, verdict in ((yes, 'yes'), (no, 'no')):
            self.store.finalize_classification(user)
            row = self.store.profile(user)
            self.assertEqual((row['manual'], row['verdict'], row['collection_status']),
                             (verdict, verdict, 'reviewed'))

    def test_automatic_classification_between_display_and_commit_yields_to_manual(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        yes, no = self.ids(page)
        self.store.classification(yes, 'no', {'decision_sources': []})
        self.store.classification(no, 'yes', {'decision_sources': ['avatar']})
        result = self.service.commit(page['batch_id'], excluded=[no])
        self.assertEqual((result['confirmed'], result['excluded']), (1, 1))
        for user, manual, automatic in ((yes, 'yes', 'no'), (no, 'no', 'yes')):
            row = self.store.profile(user)
            self.assertEqual((row['manual'], row['verdict'], row['automatic']),
                             (manual, manual, automatic))

    def test_manual_note_changed_after_display_causes_conflict(self):
        self.avatar_users(('1', '2'))
        page = self.service.page()
        # Even without a manual verdict, another window's note must not be lost.
        with self.store.db() as connection:
            connection.execute("UPDATE profiles SET manual_reason='Changed externally' WHERE uid='2'")
        before = {user: self.store.profile(user) for user in self.ids(page)}
        with self.assertRaises(ImageReviewConflict):
            self.service.commit(page['batch_id'])
        self.assert_unchanged(before)

    def test_shared_parent_scores_recalculated_once_with_unique_children(self):
        self.store.add_seeds('100 200', 10, 'yes')
        self.store.relation_page('100', 'followers', ['1', '2'], 1, False)
        self.store.relation_page('100', 'followings', ['1', '2'], 1, True)
        self.store.relation_page('200', 'followers', ['1', '2'], 1, False)
        self.avatar_users(('1', '2'))
        page = self.service.page()
        self.service.commit(page['batch_id'], excluded=['2'])
        expected = score_parts(10, 0, 1, 2)
        for user in ('100', '200'):
            parent = self.store.profile(user)
            for key in ('score', 'furry_found', 'evaluated_found', 'discovery_score', 'quality_score'):
                self.assertEqual(parent[key], expected[key], (user, key))

    def test_database_failure_rolls_back_entire_batch_and_preserves_retry(self):
        self.store.add_seeds('100', 10, 'yes')
        self.store.relation_page('100', 'followers', ['1', '2'], 1, False)
        self.avatar_users(('1', '2'))
        page = self.service.page()
        before = {user: self.store.profile(user) for user in ('1', '2', '100')}
        with self.store.db() as connection:
            connection.execute("""CREATE TRIGGER image_review_fixture_failure
                BEFORE INSERT ON events WHEN NEW.message LIKE '图片批量人工审核%'
                BEGIN SELECT RAISE(ABORT, 'Fixture atomicity failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.commit(page['batch_id'], excluded=['2'])
        self.assert_unchanged(before)
        with self.store.db() as connection:
            connection.execute('DROP TRIGGER image_review_fixture_failure')
        self.assertEqual(self.service.page(session_id=page['session_id'])['batch_id'], page['batch_id'])
        result = self.service.commit(page['batch_id'], excluded=['2'])
        self.assertFalse(result['replayed'])
        self.assertEqual(result['processed'], 2)

    def test_yes_avatar_only_scope_does_not_touch_text_or_manual_approvals(self):
        sources = {'1': ['avatar'], '2': ['avatar'], '3': ['intro'],
                   '4': ['avatar', 'video_tag'], '5': ['avatar']}
        self.avatar_users(sources)
        for user, signals in sources.items():
            self.store.classification(user, 'yes', {'decision_sources': signals})
        self.store.decision('5', 'yes')
        untouched = {user: self.store.profile(user) for user in ('3', '4', '5')}
        page = self.service.page(scope={'view': 'yes', 'mode': 'avatar_only'})
        self.assertEqual(set(self.ids(page)), {'1', '2'})
        self.service.commit(page['batch_id'], excluded=['1'])
        self.assertEqual(self.store.profile('1')['manual'], 'no')
        self.assertEqual(self.store.profile('2')['manual'], 'yes')
        self.assert_unchanged(untouched)
        self.assertTrue(self.service.page(scope={'view': 'yes', 'mode': 'avatar_only'})['done'])

    def test_query_status_and_sort_match_existing_profile_filters(self):
        for user, name, score in (('1', 'Match low', .2), ('2', 'Match high', .9),
                                  ('3', 'Elsewhere', .99), ('4', 'Match pending', .5)):
            self.avatar(user, name=name, status='pending' if user == '4' else 'needs_review')
            self.store.classification(user, 'unknown', {'avatar': {'success': True, 'furry_score': score}})
        page = self.service.page(scope={'view': 'candidates', 'mode': 'all', 'q': 'Match',
                                       'status': 'review', 'sort': 'score'})
        self.assertEqual(self.ids(page), ['2', '1'])


class ImageReviewHTTPTests(ImageReviewFixture):
    def setUp(self):
        super().setUp()
        worker = Collector(self.store, FakeClient(), FakeClassifier())
        self.server = DashboardServer(self.root, 0, self.store, worker)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        super().tearDown()

    def request(self, path, payload=None, headers=None):
        combined = {'Content-Type': 'application/json'}
        if payload is not None:
            combined.update({'Origin': self.base, 'X-Panel-Token': self.server.token})
        combined.update(headers or {})
        request = urllib.request.Request(self.base + path,
                                         data=json.dumps(payload).encode() if payload is not None else None,
                                         headers=combined)
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    def test_page_and_commit_require_same_origin_and_token(self):
        self.avatar('1')
        for route in ('page', 'commit'):
            path = '/api/image-review/' + route
            for headers in ({'X-Panel-Token': ''}, {'Origin': 'https://evil.invalid'},
                            {'Origin': ''}, {'Host': 'evil.invalid'}):
                with self.subTest(route=route, headers=headers):
                    status, body, _ = self.request(path, {}, headers)
                    self.assertEqual(status, 403)
                    self.assertNotIn(self.server.token.encode(), body)
        self.assertIsNone(self.store.profile('1')['manual'])

    def test_routes_commit_batch_and_map_validation_to_400(self):
        self.avatar_users(('1', '2'))
        status, body, _ = self.request('/api/image-review/page', {'limit': 32})
        self.assertEqual(status, 200)
        page = json.loads(body)
        status, _, _ = self.request('/api/image-review/commit',
                                    {'batch_id': page['batch_id'], 'excluded': ['999']})
        self.assertEqual(status, 400)
        self.assertIsNone(self.store.profile('1')['manual'])
        status, body, _ = self.request('/api/image-review/commit',
                                       {'batch_id': page['batch_id'], 'excluded': ['1'], 'deferred': []})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['processed'], 2)
        self.assertEqual(self.store.profile('1')['verdict'], 'no')
        self.assertEqual(self.store.profile('2')['verdict'], 'yes')
        self.assertEqual(self.request('/api/image-review/page', {'limit': True})[0], 400)

    def test_http_conflict_is_409_and_no_other_rows_written(self):
        self.avatar_users(('1', '2'))
        page = json.loads(self.request('/api/image-review/page', {})[1])
        self.store.decision('2', 'no', 'Another review window')
        status, body, _ = self.request('/api/image-review/commit', {'batch_id': page['batch_id']})
        self.assertEqual(status, 409)
        self.assertTrue(json.loads(body).get('error'))
        self.assertIsNone(self.store.profile('1')['manual'])
        self.assertEqual(self.store.profile('2')['manual_reason'], 'Another review window')

    def test_static_assets_are_same_origin_javascript_and_stylesheet(self):
        for path, content_type in (('/image-review.js', 'text/javascript'), ('/image-review.css', 'text/css')):
            with self.subTest(path=path):
                status, body, headers = self.request(path)
                self.assertEqual(status, 200)
                self.assertTrue(headers['Content-Type'].startswith(content_type))
                self.assertIn("script-src 'self'", headers['Content-Security-Policy'])
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertTrue(body)


if __name__ == '__main__':
    import unittest
    unittest.main()
