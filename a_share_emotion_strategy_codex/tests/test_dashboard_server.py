"""Real HTTP on a disposable loopback port, with synthetic public/private data."""
import datetime as dt
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_server
import monitor_feed

TZ = dashboard_server.TZ
NOW = dt.datetime(2026, 9, 14, 10, 0, 30, tzinfo=TZ)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.public = self.base / 'public'
        self.monitor = self.base / 'monitor'
        self.public.mkdir()
        self.monitor.mkdir()
        (self.public / 'latest.html').write_text('<!doctype html><h1>虚构观察页面</h1>', encoding='utf-8')
        (self.public / 'app.js').write_text('window.syntheticPage = true;', encoding='utf-8')
        (self.base / 'config.json').write_text('{"ntfy_token":"PRIVATE_SYNTHETIC_TOKEN"}')
        (self.public / 'unlisted.json').write_text('{"private":"UNLISTED_SYNTHETIC_VALUE"}')
        (self.public / 'outside.json').symlink_to(self.base / 'config.json')
        (self.public / 'escape').symlink_to(self.base, target_is_directory=True)
        (self.public / 'version.json').write_text(json.dumps(dict(
            revision='synthetic-r1', updated_at=NOW.isoformat(),
            files=['latest.html', 'app.js', 'outside.json', 'escape/config.json', '../config.json'],
            private_path='/Users/<synthetic>/private', ntfy_token='VERSION_SYNTHETIC_SECRET')))
        (self.monitor / 'status.json').write_text(json.dumps(dict(
            heartbeat=NOW.isoformat(), enabled=True, scope='all_sectors', membership_date='2026-09-14',
            quality=dict(status='healthy', window_coverage=1), ntfy_token='STATUS_SYNTHETIC_SECRET')))
        (self.monitor / 'events.json').write_text(json.dumps([dict(
            time=NOW.timestamp(), message='PRIVATE_MESSAGE', ntfy_topic='TOPIC_SYNTHETIC_SECRET',
            signals=[dict(type='dragon_fund_outflow', scope='dragon_watchlist', key='synthetic:1',
                          code='600001', name='虚构观察甲', source_asof=(NOW - dt.timedelta(seconds=30)).isoformat(),
                          main_net_cny=-1000, ntfy_token='EVENT_SYNTHETIC_SECRET')])]))
        self.patches = [
            patch('dashboard_server.private_path', side_effect=lambda name='': self.public),
            patch('monitor_feed.monitor_root', return_value=self.monitor),
            patch('market_calendar.is_trading_day', return_value=(True, '测试交易日')),
            patch('dashboard_server.read_status', side_effect=lambda: monitor_feed.read_status(now=NOW, directory=self.monitor)),
            patch('dashboard_server.read_events', side_effect=lambda day=None: monitor_feed.read_events(day=day, cutoff=NOW, directory=self.monitor)),
        ]
        for item in self.patches:
            item.start()
        self.server = dashboard_server.make_server(port=0, root=self.public)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()
        self.assertFalse(self.thread.is_alive())

    def request(self, path='/', method='GET', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_random_loopback_bind_and_normal_page_head_and_javascript(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertNotEqual(self.port, 8767)
        code, headers, body = self.request('/')
        self.assertEqual(code, 200)
        self.assertIn('虚构观察页面'.encode(), body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
        code, headers, body = self.request('/latest.html', method='HEAD')
        self.assertEqual(code, 200)
        self.assertEqual(body, b'')
        self.assertGreater(int(headers['Content-Length']), 0)
        self.assertEqual(self.request('/app.js')[0], 200)

    def test_status_events_and_version_endpoints(self):
        code, _, body = self.request('/api/monitor-status')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['state'], 'healthy')
        code, _, body = self.request('/api/monitor-events?date=2026-09-14')
        self.assertEqual(code, 200)
        event = json.loads(body)['events'][0]
        self.assertEqual(event['type'], 'dragon_fund_outflow')
        self.assertEqual(event['quote_time'], (NOW - dt.timedelta(seconds=30)).isoformat())
        code, _, body = self.request('/api/dashboard-version')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {'revision': 'synthetic-r1', 'updated_at': NOW.isoformat()})

    def test_api_responses_do_not_disclose_private_fields(self):
        for path in ('/api/monitor-status', '/api/monitor-events', '/api/dashboard-version'):
            with self.subTest(path=path):
                code, _, body = self.request(path)
                self.assertEqual(code, 200)
                text = body.decode()
                for secret in ('PRIVATE_SYNTHETIC_TOKEN', 'STATUS_SYNTHETIC_SECRET', 'EVENT_SYNTHETIC_SECRET',
                               'TOPIC_SYNTHETIC_SECRET', 'VERSION_SYNTHETIC_SECRET', 'PRIVATE_MESSAGE', '/Users/'):
                    self.assertNotIn(secret, text)

    def test_wrong_host_origin_or_cross_site_fetch_are_rejected(self):
        for headers in ({'Host': 'evil.invalid'}, {'Host': '127.0.0.1:1'},
                        {'Origin': 'https://evil.invalid'}, {'Origin': 'null'},
                        {'Origin': 'http://localhost:1'}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(headers=headers):
                code, _, body = self.request('/api/monitor-status', headers=headers)
                self.assertEqual(code, 403)
                self.assertEqual(json.loads(body)['error'], 'local_only')

    def test_same_loopback_host_and_origin_are_accepted(self):
        for host in (f'127.0.0.1:{self.port}', f'localhost:{self.port}'):
            self.assertEqual(self.request('/api/monitor-status', headers={
                'Host': host, 'Origin': 'http://' + host, 'Sec-Fetch-Site': 'same-origin'})[0], 200)

    def test_mutating_methods_are_rejected_without_changing_files(self):
        before = {p: p.read_bytes() for p in self.monitor.iterdir()}
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            code, _, body = self.request('/api/monitor-events', method=method, body=b'{"mutation":true}')
            self.assertEqual(code, 405)
            self.assertEqual(json.loads(body)['error'], 'read_only')
        self.assertEqual(before, {p: p.read_bytes() for p in self.monitor.iterdir()})

    def test_traversal_unlisted_private_paths_and_symlinks_are_rejected(self):
        for path in ('/../config.json', '/%2e%2e/config.json', '/%2e%2e%2fconfig.json',
                     '/..%5cconfig.json', '/config.json', '/unlisted.json', '/outside.json',
                     '/escape/config.json', '/api/unknown', '/version.json'):
            with self.subTest(path=path):
                code, _, body = self.request(path)
                self.assertIn(code, (400, 404))
                self.assertNotIn(b'PRIVATE_SYNTHETIC_TOKEN', body)

    def test_query_validation_rejects_empty_duplicate_unknown_and_future_dates(self):
        for path in ('/api/monitor-status?x=1', '/api/dashboard-version?x=1', '/latest.html?x=1',
                     '/api/monitor-events?unknown=x', '/api/monitor-events?date=bad-date',
                     '/api/monitor-events?date=2099-01-01', '/api/monitor-events?date',
                     '/api/monitor-events?date=', '/api/monitor-events?date=&date=2026-09-14',
                     '/api/monitor-events?date=2026-09-14&date=2026-09-13'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)

    def test_missing_static_file_and_backend_failure_are_controlled(self):
        (self.public / 'app.js').unlink()
        self.assertEqual(self.request('/app.js')[0], 404)
        with patch('dashboard_server.read_status', side_effect=OSError('SYNTHETIC_PRIVATE_EXCEPTION')):
            code, _, body = self.request('/api/monitor-status')
            self.assertEqual(code, 503)
            self.assertEqual(json.loads(body), {'error': 'data_unavailable'})
            self.assertNotIn(b'SYNTHETIC_PRIVATE_EXCEPTION', body)


if __name__ == '__main__':
    unittest.main()
