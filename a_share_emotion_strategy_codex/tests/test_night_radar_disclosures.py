"""Offline fixtures are invented; they contain no real radar candidates."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import night_radar_disclosures as radar


def notice(code='600001', event='TEST001', title='示例:2026年前三季度业绩预告',
           source='2026-10-12 18:00:00:001'):
    return {'art_code': event, 'title': title, 'display_time': source,
            'notice_date': '2026-10-13 00:00:00',
            'codes': [{'stock_code': code, 'short_name': '虚构样本', 'ann_type': 'A,SHA'}]}


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = Path(self.temp.name)
        self.cutoff = radar._time('2026-10-12T22:00:00+08:00')

    def test_timestamp_does_not_use_next_notice_midnight(self):
        rows = [notice(), notice(event='TEST002', source='2026-10-12 22:00:00'),
                notice(event='TEST003', source=None),
                notice(event='TEST004', source='2026-10-13 01:00:00')]
        with patch.object(radar, '_fetch', return_value={'success': 1, 'data': {
                'total_hits': 4, 'list': rows}}):
            chosen, missing, coverage = radar._notice_pages(
                radar._time('2026-10-10T22:00:00+08:00'), self.cutoff, self.raw)
        self.assertEqual([r['art_code'] for r in chosen], ['TEST001'])
        self.assertIn('缺少有效源展示时间', ''.join(missing))
        self.assertEqual(coverage['source_total'], 4)

    def test_duplicate_pages_cannot_claim_complete_coverage(self):
        duplicate = notice()
        with patch.object(radar, '_fetch', return_value={'success': 1, 'data': {
                'total_hits': 2, 'list': [duplicate, duplicate]}}):
            chosen, missing, _ = radar._notice_pages(
                radar._time('2026-10-10T22:00:00+08:00'), self.cutoff, self.raw)
        self.assertEqual(len(chosen), 1)
        self.assertIn('分页不完整', ''.join(missing))

    def test_late_revised_content_cannot_be_backdated(self):
        row = notice()
        row['eiTime'] = '2026-10-13 09:00:00:000'
        with patch.object(radar, '_fetch', return_value={'success': 1, 'data': {
                'total_hits': 1, 'list': [row]}}):
            chosen, missing, _ = radar._notice_pages(
                radar._time('2026-10-10T22:00:00+08:00'), self.cutoff, self.raw)
        self.assertEqual(chosen, [])
        self.assertIn('未倒填历史', ''.join(missing))

    def test_unknown_money_is_not_zero(self):
        self.assertIsNone(radar._number(None))
        self.assertIsNone(radar._number('nan'))
        self.assertIsNone(radar._number('--'))
        self.assertEqual(radar._number(0), 0)
        self.assertEqual(radar._number('12,345.6'), 12345.6)
        self.assertFalse(radar._has_amount('计划回购金额1亿元', None))
        self.assertTrue(radar._has_amount('单位：万元 1,200～2,400', 12000000))
        self.assertFalse(radar._has_amount('单位：万元 1,200～2,400', 1200000))

    def test_structured_period_cannot_be_renamed_by_another_section(self):
        self.assertEqual(radar._period('预告期间2026年7月1日至2026年9月30日', '2026-09-30'),
                         '2026年前三季度')
        self.assertIsNone(radar._profit_segment(
            '2026年第三季度业绩预告2026年7月1日至2026年9月30日'
            '归属于上市公司股东的净利润100至200万元', '2026-09-30', 1000000, 2000000))

    def test_money_from_other_period_cannot_verify_cumulative_forecast(self):
        text = ('2026年前三季度业绩预告归属于上市公司股东的净利润1000至2000万元。'
                '2026年第三季度业绩预计情况归属于上市公司股东的净利润100至200万元。')
        self.assertIsNotNone(radar._profit_segment(text, '2026-09-30', 10000000, 20000000))
        self.assertIsNone(radar._profit_segment(text, '2026-09-30', 1000000, 2000000))

    def test_historical_repurchase_stage_comes_from_dated_notice(self):
        self.assertEqual(radar._historical_progress('示例:关于回购股份的预案',
                         '董事会审议通过本回购方案'), '董事会预案')
        self.assertEqual(radar._historical_progress('示例:回购结果暨股份变动公告',
                         '公司完成回购'), '完成实施')

    def test_exhausted_budget_does_not_make_network_request(self):
        target = radar._RawTarget(self.raw)
        target.deadline = 0
        with patch.object(radar.requests, 'get') as get:
            with self.assertRaises(RuntimeError):
                radar._fetch('https://example.com', {}, target, 'budget')
        get.assert_not_called()

    def _collect(self, original=True, financial_failure=False, buyback_text=None, buyback_title=None):
        one = notice()
        two = notice(code='600002', event='TEST002', title='另例:关于回购股份的预案')
        if buyback_title:
            two['title'] = buyback_title
        three = notice(code='300001', event='TEST003', title='科例:关于采购合同的公告')
        originals = {
            'TEST001': {'text': '2026年前三季度业绩预告。归属于上市公司股东的净利润，单位万元，亏损100至200',
                        'pdf_url': 'https://example.com/a.pdf', 'body_fingerprint': 'first'},
            'TEST002': {'text': '董事会审议通过回购方案。回购预算1000万元至2000万元，回购价格不超过5.00元，'
                               '本次回购的股份用于转换公司发行的可转换公司债券',
                        'pdf_url': 'https://example.com/b.pdf', 'body_fingerprint': 'second'},
            'TEST003': {'text': '本公告虚构样本，公司向供应商采购相关产品，并未签订销售合同。',
                        'pdf_url': 'https://example.com/c.pdf', 'body_fingerprint': 'third'},
        }
        if buyback_text is not None:
            originals['TEST002']['text'] = buyback_text
        financial = [{'SECURITY_CODE': '600001', 'NOTICE_DATE': '2026-10-13',
                      'REPORT_DATE': '2026-09-30', 'PREDICT_AMT_LOWER': -2000000,
                      'PREDICT_AMT_UPPER': -1000000, 'ADD_AMP_LOWER': -120,
                      'ADD_AMP_UPPER': -110, 'PREDICT_TYPE': '首亏'}]
        # The source writes negatives with signs, as required by exact matching.
        originals['TEST001']['text'] += '表中归母净利润-200至-100万元'
        repurchases = [{'DIM_SCODE': '600002', 'UPDATEDATE': '2026-10-13',
                        'JEXX': 10000000, 'JESX': 20000000, 'REPURPRICECAP': 5,
                        'REPURAMOUNT': None, 'REPURPROGRESS': '001'}]
        def data(report, *args):
            if report == 'RPT_PUBLIC_OP_NEWPREDICT':
                if financial_failure:
                    raise RuntimeError('人工断线')
                return financial, []
            return repurchases, []
        def body(event, *args):
            if not original:
                raise RuntimeError('人工原文缺失')
            return originals[event]
        with patch.object(radar, '_notice_pages', return_value=([one, two, three], [], {'collection_complete': True})), \
             patch.object(radar, '_original', side_effect=body), \
             patch.object(radar, '_data_pages', side_effect=data):
            return radar.collect_disclosures(self.cutoff, raw_dir=self.raw)

    def test_original_verified_earnings_and_plan_not_actual(self):
        result = self._collect()
        self.assertEqual(result['earnings']['rows'][0]['profit_low_cny'], -2000000)
        self.assertEqual(result['repurchases']['rows'][0]['budget_low_cny'], 10000000)
        self.assertIsNone(result['repurchases']['rows'][0]['actual_cny'])
        self.assertEqual(result['repurchases']['rows'][0]['purpose'], '用于可转债转股')
        announcements = {r['code']: r for r in result['announcements']['rows']}
        self.assertEqual(announcements['600001']['stance'], 'risk')
        self.assertIn('采购方', announcements['300001']['summary'])
        self.assertTrue(announcements['300001']['verified'])
        self.assertTrue(result['announcements']['collection_complete'])
        self.assertEqual(result['announcements']['status'], 'partial')

    def test_missing_original_keeps_title_without_upgrading_numbers(self):
        result = self._collect(original=False)
        self.assertFalse(any(r['verified'] for r in result['announcements']['rows']))
        self.assertEqual(result['earnings']['rows'], [])
        self.assertEqual(result['repurchases']['rows'], [])
        self.assertEqual(result['earnings']['status'], 'partial')

    def test_one_financial_source_failure_is_isolated(self):
        result = self._collect(financial_failure=True)
        self.assertEqual(result['earnings']['status'], 'unavailable')
        self.assertFalse(result['earnings']['collection_complete'])
        self.assertTrue(result['repurchases']['collection_complete'])
        self.assertEqual(len(result['repurchases']['rows']), 1)
        self.assertGreater(len(result['announcements']['rows']), 0)

    def test_supporting_cancellation_papers_are_not_market_buybacks(self):
        for title in ('回购注销限制性股票', '回购注销完成公告', '回购股份法律意见书',
                      '董事会意见之回购方案', '限制性股票回购方案'):
            self.assertFalse(radar._market_repurchase_title(title))
        self.assertTrue(radar._market_repurchase_title('关于集中竞价回购股份方案的公告'))
        self.assertTrue(radar._market_repurchase_title('集中竞价回购股份实施完成公告'))
        self.assertTrue(radar._market_repurchase_title('关于集中竞价回购股份并注销减资的方案'))

    def test_structured_lists_collected_before_any_original(self):
        seen = set()
        sample = notice()
        def table(report, *args):
            seen.add(report)
            return [], []
        def original(event, *args):
            self.assertEqual(seen, {'RPT_PUBLIC_OP_NEWPREDICT', 'RPTA_WEB_GETHGLIST_NEW'})
            raise RuntimeError('测试原文耗尽，不能改变列表采集状态')
        with patch.object(radar, '_notice_pages', return_value=([sample], [], {'collection_complete': True})), \
             patch.object(radar, '_data_pages', side_effect=table), \
             patch.object(radar, '_original', side_effect=original):
            result = radar.collect_disclosures(self.cutoff, raw_dir=self.raw)
        self.assertTrue(result['earnings']['collection_complete'])
        self.assertTrue(result['repurchases']['collection_complete'])
        self.assertNotEqual(result['earnings']['status'], 'unavailable')
        self.assertNotEqual(result['repurchases']['status'], 'unavailable')

    def test_interleaved_profit_label_remains_same_period(self):
        text = ('2026年前三季度业绩预告单位万元'
                '归属于上市公司34000至36000股东的净利润同比90.78%至102%'
                '扣除非经常性损益后的净利润33000至35000万元')
        segment = radar._profit_segment(text, '2026-09-30', 340000000, 360000000)
        self.assertIsNotNone(segment)
        self.assertIsNone(radar._profit_segment(text, '2026-09-30', 330000000, 350000000))

    def test_actual_amount_is_never_a_budget(self):
        text = ('截至本日累计回购股份1000股，成交总金额为19991107元。')
        self.assertEqual(radar._repurchase_amounts('回购进展公告', text),
                         (None, None, 19991107.0))
        result = self._collect(buyback_text=text, buyback_title='示例：回购进展公告')
        row = result['repurchases']['rows'][0]
        self.assertIsNone(row['budget_low_cny'])
        self.assertIsNone(row['budget_high_cny'])
        self.assertEqual(row['actual_cny'], 19991107)
        self.assertTrue(row['verified'])
        self.assertFalse(row['budget_verified'])
        self.assertTrue(row['actual_verified'])

    def test_adjustment_uses_new_budget_and_is_not_a_new_plan(self):
        title = '关于调整回购股份方案的公告'
        text = ('公司将回购资金总额由不低于1300万元且不超过2600万元调整至'
                '不低于1400万元且不超过2800万元。'
                '此前董事会审议通过回购股份方案。已累计回购股份1000股，'
                '成交总金额为25998414.29元。')
        self.assertEqual(radar._repurchase_amounts(title, text),
                         (14000000, 28000000, 25998414.29))
        self.assertEqual(radar._historical_progress(title, text), '调整方案（实施中）')

    def test_rounded_actual_summary_cannot_replace_exact_yuan_line(self):
        text = ('预计回购金额3000万元~4000万元。实际回购金额3685.70万元。'
                '截至本日已实际回购股份5000股，已累计支付的总金额为36857031.69元。')
        self.assertEqual(radar._repurchase_amounts('回购实施结果', text),
                         (30000000, 40000000, 36857031.69))
        self.assertEqual(radar._historical_progress('回购实施结果暨股份变动公告', text), '完成实施')

    def test_unknown_stage_cannot_receive_fully_verified_badge(self):
        result = self._collect(buyback_title='示例：取得股份回购贷款承诺函的公告')
        row = result['repurchases']['rows'][0]
        self.assertFalse(row['verified'])
        self.assertFalse(row['progress_verified'])
        self.assertEqual(row['progress'], '进度待核验')

    def test_procedural_notices_cannot_be_featured_as_company_risks(self):
        for title in ('关于回购的法律意见书', '薪酬委员会关于终止激励的核查意见',
                      '股票停牌前十大股东情况公告', '可转债转股停牌公告',
                      '关于减持计划届满且未减持的公告', '股东大会关于重大收购的议案'):
            self.assertTrue(radar._procedural_announcement(title))
            self.assertEqual(radar._classification(title), ('neutral', 99))
        self.assertEqual(radar._classification('关于终止股权激励计划的公告'), ('neutral', 4))
        self.assertEqual(radar._classification('关于控股股东减持计划的公告'), ('risk', 0))

    def test_notice_failure_is_not_success_empty_pool(self):
        with patch.object(radar, '_notice_pages', side_effect=RuntimeError('人工分页失败')), \
             patch.object(radar, '_data_pages', return_value=([], [])):
            result = radar.collect_disclosures(self.cutoff, raw_dir=self.raw)
        self.assertTrue(all(x['status'] == 'unavailable' for x in result.values()))
        self.assertTrue(all(x['rows'] == [] for x in result.values()))


if __name__ == '__main__':
    unittest.main()
