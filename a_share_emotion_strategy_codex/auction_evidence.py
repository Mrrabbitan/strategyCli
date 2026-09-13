"""Point-in-time auction evidence, independent from daily turnover and rankings.

The Eastmoney premarket minute feed is documented by AKShare in lots (100
shares), with amount in CNY. A minute bucket labelled 09:26 is NOT relabelled
09:25. Indicative pre-auction prices never become actual matched trades.
"""
from __future__ import annotations

import datetime as dt
import math
import urllib.parse
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
ENDPOINT = 'https://push2.eastmoney.com/api/qt/stock/trends2/get'
DOCUMENTATION = 'https://akshare.akfamily.xyz/data/stock/stock.html'
VERSION = 'auction-evidence-v1'


def endpoint(code):
    return ENDPOINT + '?' + urllib.parse.urlencode({
        'fields1': 'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58',
        'ndays': 1, 'iscr': 1, 'iscca': 0,
        'secid': ('1.' if code.startswith('6') else '0.') + code})


def number(value):
    if isinstance(value, bool):
        raise ValueError('数值字段不能为布尔值')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('非有限数值')
    return value


def pending(reason, *, source_url=ENDPOINT):
    return {'status': 'pending', 'verified': False, 'qualified': False,
            'price': None, 'volume_shares': None, 'amount_cny': None,
            'gap_pct': None, 'turnover_pct': None, 'source_asof': None,
            'process_verified': False, 'reason': reason, 'virtual_observations': [],
            'minute_observations': [], 'source_url': source_url,
            'volume_unit': '股（原源手×100）', 'amount_unit': '人民币元'}


def parse(payload, code, as_of, *, float_evidence=None, reference_evidence=None):
    """Validate exact 09:25 matched data, with separately sourced denominators.

    Float evidence must explicitly identify unrestricted shares with its own
    as-of/source/effective date. It cannot be estimated from all-day turnover or
    current market capitalisation. Reference price requires same-day effective
    evidence; the daily close alone is not an ex-rights-safe official reference.
    """
    as_of = as_of.astimezone(TZ)
    result = pending('9:25最终成交待核验', source_url=endpoint(code))
    if as_of.time().replace(tzinfo=None) < dt.time(9, 25):
        result['reason'] = '9:25以前仅盘前预观察，最终竞价尚未发生'
        return result
    try:
        data = payload.get('data')
        if payload.get('rc', 0) != 0 or not isinstance(data, dict) or str(data.get('code')) != code:
            raise ValueError('盘前分钟响应或股票代码不符')
        rows, stamps = [], set()
        for raw in data.get('trends') or []:
            cells = raw.split(',')
            if len(cells) != 8:
                raise ValueError('盘前分钟字段数量变化')
            stamp = dt.datetime.fromisoformat(cells[0]).replace(tzinfo=TZ)
            if stamp > as_of or stamp.date() != as_of.date():
                continue
            if stamp in stamps:
                raise ValueError('同一盘前分钟存在重复或冲突记录')
            stamps.add(stamp)
            values = [number(x) for x in cells[1:]]
            if min(values[4:6]) < 0:
                raise ValueError('成交量或成交额为负值')
            row = {'source_asof': stamp.isoformat(), 'open': values[0], 'close': values[1],
                   'high': values[2], 'low': values[3], 'volume_shares': values[4]*100,
                   'amount_cny': values[5], 'provider_average': values[6]}
            rows.append((stamp, row))
        if not rows:
            raise ValueError('没有分析当日且不晚于分析时点的分钟资料')
        result['virtual_observations'] = [r for t, r in rows if dt.time(9, 15) <= t.time() < dt.time(9, 25)]
        result['minute_observations'] = [r for t, r in rows if dt.time(9, 25) <= t.time() < dt.time(9, 30)]
        exact = [r for t, r in rows if t.time() == dt.time(9, 25)]
        if len(exact) != 1:
            raise ValueError('缺少源时点为9:25的记录；相邻分钟不能替代最终竞价')
        row = exact[0]
        if row['volume_shares'] <= 0 or row['amount_cny'] <= 0:
            raise ValueError('9:25未取得非零实际成交；9:26等分钟桶不冒充最终撮合')
        price = row['close']
        if price <= 0 or any(abs(row[k]-price) > .001 for k in ('open', 'high', 'low')):
            raise ValueError('9:25记录不是可核验的单一最终撮合价')
        if abs(row['amount_cny']/row['volume_shares'] - price) > .011:
            raise ValueError('9:25成交价、成交股数和成交额不一致，单位待核验')
        result.update(status='partial', verified=True, source_asof=row['source_asof'],
                      price=price, volume_shares=row['volume_shares'], amount_cny=row['amount_cny'])
        missing = []
        reference_evidence = reference_evidence or {}
        float_evidence = float_evidence or {}

        def timely(evidence):
            stamp = dt.datetime.fromisoformat(evidence['source_asof'])
            return (stamp.tzinfo is not None and stamp <= as_of
                    and evidence.get('effective_date') == as_of.date().isoformat()
                    and str(evidence.get('source_url', '')).startswith('https://')
                    and evidence.get('verified') is True)

        try:
            if not timely(reference_evidence) or reference_evidence.get('kind') != 'official_price_reference':
                raise ValueError('未取得当日有效涨跌幅参考前收盘价证据')
            previous = number(reference_evidence['price'])
            if previous <= 0:
                raise ValueError('参考价无效')
            result['gap_pct'] = (price/previous-1)*100
        except (KeyError, ValueError, TypeError) as exc:
            missing.append('当日涨跌幅参考价缺失或未经核验')
        try:
            if not timely(float_evidence) or float_evidence.get('kind') != 'unrestricted_float_shares':
                raise ValueError('流通股本口径未核验')
            shares = number(float_evidence['shares'])
            if shares <= 0 or shares < row['volume_shares']:
                raise ValueError('流通股数无效')
            result['turnover_pct'] = row['volume_shares']/shares*100
            result['float_evidence'] = dict(float_evidence)
        except (KeyError, ValueError, TypeError):
            missing.append('当时无限售流通股数缺失或未经核验')
        result['qualified'] = (not missing and result['gap_pct'] > 0 and result['turnover_pct'] > 0)
        result['status'] = 'verified' if not missing else 'partial'
        result['reason'] = '；'.join(missing) if missing else (
            '最终成交、高开和实际换手已核验；不代表当日回封或已可买入' if result['qualified'] else '竞价未高开，基础竞价条件不通过')
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        result['reason'] = str(exc)
    return result
