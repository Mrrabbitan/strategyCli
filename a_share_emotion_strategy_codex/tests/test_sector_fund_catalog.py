"""Offline synthetic fallbacks; no real industry names or requests."""
import datetime as dt
import time
import unittest

from sector_fund_catalog import collect_web_universe, parse_catalog_html

NOW = dt.datetime(2026, 1, 5, 10, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def html(count=51, duplicate=False, concept_collision=False):
    industry = ''.join(f'<a href="/bkzj/BK{i:04d}.html">人工行业{i}</a>' for i in range(count))
    if duplicate:
        industry += '<a href="/bkzj/BK0000.html">人工重复</a>'
    concept_code = 'BK0000' if concept_collision else 'BK9000'
    return ('<html><!-- 2026-01-05 10:30:00 at server[1] --><div class="bkselectorpop"><ul class="pop_slider">'
            '<li>行业</li><li>概念</li><li>地域</li></ul>'
            '<div class="pop-cont">' + industry + '</div>'
            f'<div class="pop-cont"><a href="/bkzj/{concept_code}.html">人工概念</a></div>'
            '<div class="pop-cont"><a href="/bkzj/BK9999.html">人工地域</a></div>'
            '</div></html>')


class Feed:
    timeout = 1
    def __init__(self, missing=None, wrong=None, batch=False):
        self.deadline = time.monotonic() + 70
        self.missing, self.wrong, self.batch = missing, wrong, batch
    def eastmoney(self, endpoint, params):
        if endpoint == 'ulist.np/get':
            if not self.batch:
                raise RuntimeError('人工批量接口故障')
            codes = [value.split('.')[1] for value in params['secids'].split(',')]
            diff = [dict(f12=code, f14='人工行业' + str(int(code[2:])), f2=2345.67,
                         f3=1.25, f6=123456789, f124=int(NOW.timestamp())) for code in codes]
            return dict(total=len(diff), diff=diff), 'https://example.invalid/quote-batch'
        assert endpoint == 'stock/get'
        assert params['fltt'] == 2
        code = params['secid'].split('.')[1]
        if code == self.missing:
            raise RuntimeError('人工报价故障')
        data = dict(f57=code if code != self.wrong else 'BK9998',
                    f58='人工行业' + str(int(code[2:])), f43=2345.67, f48=123456789,
                    f170=1.25, f86=int(NOW.timestamp()))
        return data, 'https://example.invalid/quote/' + code


class CatalogueTests(unittest.TestCase):
    def test_complete_group_identity_and_no_concept_leak(self):
        groups = parse_catalog_html(html())
        self.assertEqual([len(group) for group in groups], [51, 1, 1])
        self.assertNotIn('BK9000', [row['f12'] for row in groups[0]])

    def test_reject_truncation_short_hotlist_duplicate_cross_class(self):
        for value in [html().replace('</html>', '').replace('</div></html>', ''),
                      html(3), html(duplicate=True), html(concept_collision=True),
                      html().replace('<li>行业</li>', '<li>热门行业</li>')]:
            with self.subTest(value=value[:40]), self.assertRaises(ValueError):
                parse_catalog_html(value)

    def test_independent_catalogues_must_match(self):
        calls = []
        def getter(url, timeout):
            calls.append(url)
            return html(51 if len(calls) == 1 else 52)
        with self.assertRaisesRegex(ValueError, '不一致'):
            collect_web_universe(Feed(), [], clock=lambda: NOW, http=getter)

    def test_old_catalogue_is_not_current(self):
        with self.assertRaisesRegex(ValueError, '当日'):
            collect_web_universe(Feed(), [], clock=lambda: NOW,
                                 http=lambda url, timeout: html().replace('2026-01-05', '2026-01-04'))

    def test_failed_html_body_is_saved_before_validation(self):
        for body in [html().replace('</html>', ''),
                     html().replace('2026-01-05', '2026-01-04')]:
            raw = []
            with self.subTest(body=body[-20:]), self.assertRaises(ValueError):
                collect_web_universe(Feed(), raw, clock=lambda: NOW,
                                     http=lambda url, timeout: body)
            self.assertEqual(len(raw), 1)
            self.assertEqual(raw[0]['html'], body)
            self.assertFalse(raw[0]['validation_passed'])
            self.assertTrue(raw[0]['validation_error'])
            self.assertEqual(len(raw[0]['source_fingerprint']), 64)

    def test_batch_quotes_preserve_all_identities_and_native_units(self):
        raw = []
        rows = collect_web_universe(Feed(batch=True), raw, clock=lambda: NOW,
                                    http=lambda url, timeout: html(128))
        self.assertEqual(len(rows), 128)
        self.assertTrue(all(row.get('f124') == int(NOW.timestamp()) for row in rows))
        self.assertEqual(len([r for r in raw if r['kind'] == 'industry-native-quote-batch']), 2)
        self.assertFalse(any(r['kind'] == 'industry-native-quote' for r in raw))

    def test_quote_native_clock_and_units_missing_quote_keeps_identity(self):
        raw = []
        rows = collect_web_universe(Feed(missing='BK0001', wrong='BK0002'), raw,
                                    clock=lambda: NOW, http=lambda url, timeout: html())
        self.assertEqual(len(rows), 51)
        first = rows[0]
        self.assertEqual(first['f3'], 1.25)
        self.assertEqual(first['f6'], 123456789)
        self.assertEqual(first['f124'], int(NOW.timestamp()))
        self.assertNotIn('f62', first)
        self.assertNotIn('f184', first)
        for row in rows[1:3]:
            self.assertIn('quote_error', row)
            self.assertNotIn('f124', row)
        catalogues = [item for item in raw if item['kind'] == 'industry-web-catalogue']
        self.assertEqual(len(catalogues), 2)
        self.assertEqual(catalogues[0]['coverage_basis'], '完整非分页行业导航；非clist.total')


if __name__ == '__main__':
    unittest.main()
