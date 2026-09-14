"""Auditable helpers for independent comparisons, without strategy eligibility."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import html
import re


def verified_flow_history(body, unadjusted_bars, cutoff):
    """Read the provider's historical large-order table (amounts in CNY 10,000).

    Some responses contain both an intraday table and a closing table. Match
    exact cent prices, collapse identical records, and reject conflicting
    same-date records. Caller supplies independently checked unadjusted prices;
    adjusted chart prices must not be substituted across corporate actions.
    These are order-size classifications, not identified institutional flows.
    """
    def cent(value):
        return Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)

    prices = {}
    for bar in unadjusted_bars:
        day = bar['date']
        if day in prices:
            raise ValueError('Duplicate price date')
        prices[day] = cent(bar['close'])
    body = re.sub(r'<!--.*?-->', '', body, flags=re.S)
    position = body.find('id="history_table"')
    if position < 0:
        raise ValueError('Historical flow table missing')
    records, rejected = {}, 0
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', body[position:], re.S):
        fields = [html.unescape(re.sub('<[^>]+>', '', x)).strip()
                  for x in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S)]
        if len(fields) != 11 or not re.fullmatch(r'\d{8}', fields[0]):
            continue
        try:
            day = dt.datetime.strptime(fields[0], '%Y%m%d').date().isoformat()
            price = cent(fields[1])
            values = tuple(Decimal(fields[i].replace(',', '').replace('%', '')) for i in (4, 5, 6))
            if not all(value.is_finite() for value in values) or not price.is_finite():
                raise ValueError('Non-finite flow value')
        except (ValueError, InvalidOperation):
            rejected += 1
            continue
        if day > cutoff or day not in prices or price != prices[day]:
            rejected += 1
            continue
        records.setdefault(day, set()).add((price, *values))
    ambiguous = sorted(day for day, values in records.items() if len(values) != 1)
    rows = []
    for day in sorted(records):
        if day in ambiguous:
            continue
        price, five, net, pct = next(iter(records[day]))
        rows.append(dict(date=day, close=float(price), net_yi=float(net / 10000),
                         five_net_yi=float(five / 10000), net_pct=float(pct)))
    expected = sorted(day for day in prices if day <= cutoff)
    summaries = {}
    for window in (1, 5, 20):
        tail = rows[-window:]
        if (len(tail) == window and tail[-1]['date'] == cutoff
                and [row['date'] for row in tail] == expected[-window:]):
            summaries[str(window)] = round(sum(row['net_yi'] for row in tail), 6)
    sum_matches = (abs(summaries['5'] - rows[-1]['five_net_yi']) <= .00002
                   if '5' in summaries else None)
    return dict(rows=rows, net_yi=summaries, cutoff=cutoff,
                positive_days5=sum(row['net_yi'] > 0 for row in rows[-5:]) if '5' in summaries else None,
                five_sum_matches=sum_matches, ambiguous_dates=ambiguous, rejected_rows=rejected)
