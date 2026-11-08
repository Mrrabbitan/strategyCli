"""Offline extraction of both THS publisher lists, without claiming main funds.

Collection remains in the bounded THS collector. This adapter never networks,
fills a missing flow date, or substitutes another provider's money-flow metric.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path

from etf_monitor import _market_name, _stamp
from research_store import atomic_json, private_path
from sector_fund_alternative import (THS_PUBLIC_URLS, THS_MAX_BODY_BYTES,
                                     parse_ths_page, parse_ths_published_top10)

TZ = dt.timezone(dt.timedelta(hours=8))


def unavailable_flow(reason='unavailable'):
    notes = {
        'timeout': '本轮同花顺公开入口超时，未取得有效资金榜；不以旧榜或其他来源填补。',
        'access_denied': '本轮同花顺拒绝访问，未取得有效资金榜；已停止请求。',
        'access_challenge': '本轮同花顺要求访问验证，未取得有效资金榜；不执行挑战。',
        'rate_limited': '本轮同花顺限流，未取得有效资金榜；已停止请求。',
        'invalid_content': '本轮同花顺正文未通过榜单、金额或行业身份核验。',
    }
    return {'status': 'unavailable', 'provider': '同花顺',
            'metric': '普通资金净额（未标主力）', 'as_of': None,
            'retrieved_at': None, 'inflows': [], 'outflows': [],
            'source': THS_PUBLIC_URLS[0],
            'note': notes.get(reason, '尚未取得本轮可核验资金榜，不能据此判断当日主力流入或流出。'),
            'coverage_kind': 'publisher_top10_each', 'universe_complete': False,
            'is_main_metric': False}


def parse_ths_two_sides(body, *, retrieved_at, source=THS_PUBLIC_URLS[0], now=None):
    """Preserve each native sign group's array order and same-response quotes."""
    current = now or dt.datetime.now(TZ)
    if current.tzinfo is None:
        raise ValueError('A timezone-aware analysis clock is required')
    retrieved = _stamp(retrieved_at)
    if source not in THS_PUBLIC_URLS or retrieved is None or retrieved > current:
        raise ValueError('THS reference source or retrieval clock is invalid')
    if not isinstance(body, str) or len(body.encode('utf-8')) > THS_MAX_BODY_BYTES:
        raise ValueError('THS response is missing or too large')
    # Reuse all existing amount, identity, auxiliary-table and inflow-order checks.
    verified = parse_ths_published_top10(body)
    parsed = parse_ths_page(body)
    auxiliary = {row['code']: row for row in parsed['rows']}
    groups = {'inflows': [], 'outflows': []}
    for item in parsed['chart']:
        amount = item['net_cny']
        if not isinstance(amount, (int, float)) or not math.isfinite(amount):
            raise ValueError('THS reference amount is not finite')
        group = groups['inflows' if amount >= 0 else 'outflows']
        matched = auxiliary.get(item['code'])
        group.append({'code': item['code'], 'name': _market_name(item['name']),
                      'net_yuan': amount, 'rank': len(group)+1,
                      'change_pct': matched['change_pct'] if matched else None})
    if (len(groups['inflows']) != verified['publisher_inflow_count']
            or len(groups['outflows']) != verified['publisher_outflow_count']):
        raise ValueError('THS sign-group coverage changed')
    result = unavailable_flow()
    result.update(status='reference_only', retrieved_at=retrieved.isoformat(),
                  source=source, **groups,
                  note='同花顺原站发布两类前十，保留原数组组内顺序，不是全行业重排。'
                       '净额未标为主力，资金日期与时刻未知；取得时间不代替资金时间。'
                       '缺少的辅助涨幅留空，不能与指定日期K线合成当日资金撤退信号。')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True, type=Path,
                        help='One saved collector response or failed status JSON')
    parser.add_argument('--output', required=True, type=Path,
                        help='Private flow JSON consumed by etf_pressure.py')
    args = parser.parse_args(argv)
    try:
        args.output.resolve().relative_to(private_path().resolve())
    except ValueError:
        parser.error('Flow outputs must remain inside the workbench private directory')
    value = json.loads(args.evidence.read_text())
    if not isinstance(value, dict):
        raise ValueError('Offline THS evidence must be an object')
    if value.get('outcome') == 'accepted' and isinstance(value.get('body'), str):
        flow = parse_ths_two_sides(value['body'], retrieved_at=value.get('retrieved_at'),
                                  source=value.get('source_url') or value.get('url'))
    else:
        flow = unavailable_flow(value.get('error_kind'))
    atomic_json(args.output, flow)
    print(json.dumps({'status': flow['status'], 'inflows': len(flow['inflows']),
                      'outflows': len(flow['outflows']), 'as_of': flow['as_of']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
