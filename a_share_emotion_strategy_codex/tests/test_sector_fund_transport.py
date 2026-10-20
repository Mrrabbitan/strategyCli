"""Offline public-THS transport checks; every response is synthetic.

These tests do not contact providers, write real research, or relax the current
THS-only policy.  A successful public response remains time-unknown reference.
"""
import datetime as dt
import unittest
from unittest.mock import patch

import requests

import sector_fund_alternative as alternative
from test_sector_fund_alternative import page, publisher_chart


NOW = dt.datetime(2026, 10, 20, 11, 0, tzinfo=alternative.TZ)


def valid_html():
    return page(chart=publisher_chart())


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.text = valid_html() if body is None else body
        self.status_code = status
        self.headers = dict(headers or {})
        self.url = None
        self.encoding = None
        self.iteration_count = 0
        self.closed = False

    def iter_content(self, chunk_size=65536, **kwargs):
        self.iteration_count += 1
        body = self.text.encode('gbk')
        for start in range(0, len(body), chunk_size):
            yield body[start:start+chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('synthetic HTTP '+str(self.status_code), response=self)

    def close(self):
        self.closed = True


class TextOnlyResponse:
    """Preserve compatibility with the older minimal offline response fakes."""
    status_code = 200
    headers = {'Date': 'Tue, 20 Oct 2026 03:00:00 GMT'}

    def __init__(self):
        self.text = valid_html()

    def raise_for_status(self):
        return None


class Session:
    def __init__(self, *events):
        self.events = list(events)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.events:
            raise AssertionError('unplanned extra public request')
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        if hasattr(event, 'url'):
            event.url = url
        return event


class ThsTransportTests(unittest.TestCase):
    def collect(self, session, raw=None, **kwargs):
        raw = [] if raw is None else raw
        return alternative.collect_ths_published_top10(
            session=session, now=NOW, raw_sink=raw.append, **kwargs)

    def assert_collection_error(self, session, raw=None, **kwargs):
        with self.assertRaises(alternative.ThsCollectionError) as caught:
            self.collect(session, raw, **kwargs)
        self.assertTrue(caught.exception.error_kind)
        return caught.exception

    def test_malformed_conditional_declarations_do_not_destroy_native_table(self):
        prefix = ('<!DOCTYPE html><html><head><!–[if lte IE 8]>'
                  '<meta http-equiv="X-UA-Compatible" content="IE=EmulateIE7"/>'
                  '<![endif]–></head><body>')
        parsed = alternative.parse_ths_page(prefix+valid_html()+'</body></html>')
        self.assertEqual(parsed['rows'][0]['code'], '881901')
        self.assertEqual(parsed['rows'][0]['net_cny'], 30000000)
        self.assertEqual(len(parsed['chart']), 20)

    def test_timeout_then_independent_same_provider_success(self):
        raw = []
        session = Session(requests.ReadTimeout('synthetic temporary read timeout'), Response())
        snapshot = self.collect(session, raw)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual([call[0] for call in session.calls], list(alternative.THS_PUBLIC_URLS))
        self.assertEqual(len(raw), 2)
        self.assertTrue(any(raw[0].get(key) for key in ('error', 'error_kind', 'exception')))
        self.assertEqual(snapshot['source_urls'], [session.calls[-1][0]])
        self.assertEqual(snapshot['received_count'], 10)
        self.assertFalse(snapshot['universe_complete'])
        self.assertIsNone(snapshot['fund_source_time'])

    def test_two_transport_timeouts_keep_both_attempts_and_stop(self):
        raw = []
        session = Session(requests.ConnectTimeout('synthetic connection at 0xAAAA'),
                          requests.ReadTimeout('synthetic read at 0xBBBB'))
        self.assert_collection_error(session, raw)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(raw), 2)
        for record in raw:
            self.assertTrue(record.get('url') or record.get('source_url'))
            self.assertTrue(record.get('retrieved_at') or record.get('started_at'))
            self.assertTrue(any(record.get(key) for key in ('error', 'error_kind', 'exception')))

    def test_connection_error_can_use_only_one_independent_second_response(self):
        raw = []
        session = Session(requests.ConnectionError('synthetic connection reset'), Response())
        snapshot = self.collect(session, raw)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(snapshot['source_urls'], [session.calls[1][0]])
        self.assertEqual(snapshot['rows'][0]['net_cny'], 30000000)

    def test_forbidden_rate_limit_and_unauthorized_do_not_retry(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                raw = []
                session = Session(Response(status=status), Response())
                error = self.assert_collection_error(session, raw)
                self.assertEqual(error.http_status, status)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(len(raw), 1)
                self.assertEqual(raw[0].get('status'), status)

    def test_redirect_does_not_follow_or_try_another_entrance(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                session = Session(Response(status=status,
                    headers={'Location': 'https://example.invalid/challenge'}), Response())
                error = self.assert_collection_error(session)
                self.assertEqual(error.http_status, status)
                self.assertEqual(len(session.calls), 1)
                self.assertIs(session.calls[0][1].get('allow_redirects'), False)

    def test_http_200_challenge_is_not_a_blank_pool_or_retry_permission(self):
        body = '<html><title>安全验证</title><p>访问过于频繁，请输入验证码</p></html>'
        raw = []
        session = Session(Response(body), Response())
        self.assert_collection_error(session, raw)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(raw), 1)
        self.assertIn('安全验证', raw[0].get('body', ''))

    def test_server_error_can_retry_but_does_not_contribute_rows(self):
        raw = []
        session = Session(Response('synthetic server unavailable', status=503), Response())
        snapshot = self.collect(session, raw)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(raw), 2)
        self.assertEqual(raw[0].get('status'), 503)
        self.assertEqual(snapshot['source_urls'], [session.calls[1][0]])
        self.assertEqual(snapshot['received_count'], 10)

    def test_server_challenge_or_data_embedded_in_challenge_stops_immediately(self):
        challenge = '<title>安全验证</title><p>请输入验证码</p>'
        for status, body in ((503, challenge), (200, valid_html()+challenge)):
            with self.subTest(status=status):
                session, raw = Session(Response(body, status=status), Response()), []
                error = self.assert_collection_error(session, raw)
                self.assertEqual(error.error_kind, 'access_challenge')
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(len(raw), 1)

    def test_native_schema_or_same_response_conflict_stops_without_retry(self):
        bad_unit = valid_html().replace('流入资金(亿)', '流入资金(万)')
        conflict = publisher_chart()
        conflict[0]['amount'] = .5
        for body in (bad_unit, page(chart=conflict)):
            with self.subTest(body_kind='unit' if body is bad_unit else 'conflict'):
                raw = []
                session = Session(Response(body), Response())
                error = self.assert_collection_error(session, raw)
                self.assertTrue(error.validation_code or error.error_kind)
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(len(raw), 1)

    def test_success_streams_with_no_redirect_and_retains_unknown_market_time(self):
        response = Response(headers={'Date': 'Tue, 20 Oct 2026 03:00:00 GMT'})
        raw = []
        session = Session(response)
        snapshot = self.collect(session, raw)
        self.assertEqual(len(session.calls), 1)
        self.assertIs(session.calls[0][1].get('stream'), True)
        self.assertIs(session.calls[0][1].get('allow_redirects'), False)
        self.assertGreater(response.iteration_count, 0)
        self.assertEqual(snapshot['native_unit'], '亿元')
        self.assertEqual(snapshot['normalized_unit'], '元')
        self.assertEqual(snapshot['rows'][0]['net_cny'], 30000000)
        self.assertEqual(snapshot['coverage_kind'], 'publisher_top10')
        self.assertFalse(snapshot['universe_complete'])
        self.assertFalse(snapshot['half_hour_available'])
        self.assertIsNone(snapshot['fund_source_time'])
        self.assertEqual(snapshot['time_quality'], 'unknown_reference')
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]['headers']['Date'], response.headers['Date'])

    def test_streamed_response_larger_than_two_megabytes_is_rejected(self):
        raw = []
        session = Session(Response('x'*(2*1024*1024+1)), Response())
        self.assert_collection_error(session, raw)
        self.assertEqual(len(session.calls), 1)
        self.assertGreaterEqual(len(raw), 1)

    def test_text_only_old_fake_remains_supported(self):
        snapshot = self.collect(Session(TextOnlyResponse()))
        self.assertEqual(snapshot['received_count'], 10)
        self.assertIsNone(snapshot['fund_source_time'])

    def test_explicit_single_attempt_never_tries_second_url(self):
        session = Session(requests.ReadTimeout('synthetic timeout'), Response())
        self.assert_collection_error(session, max_attempts=1)
        self.assertEqual(len(session.calls), 1)

    def test_exhausted_total_budget_does_not_start_second_request(self):
        raw = []
        session = Session(Response(), Response())
        ticks = iter((0, 0, 13))
        with patch.object(alternative.time, 'monotonic', side_effect=lambda: next(ticks, 13)):
            error = self.assert_collection_error(session, raw, budget_seconds=12)
        self.assertEqual(error.error_kind, 'timeout')
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(raw), 1)

    def test_invalid_attempt_or_budget_limits_make_no_request(self):
        for options in ({'max_attempts': 0}, {'max_attempts': 3},
                        {'budget_seconds': 0}, {'budget_seconds': 31}):
            with self.subTest(options=options):
                session = Session(Response())
                with self.assertRaises(ValueError):
                    self.collect(session, **options)
                self.assertEqual(session.calls, [])


if __name__ == '__main__':
    unittest.main()
