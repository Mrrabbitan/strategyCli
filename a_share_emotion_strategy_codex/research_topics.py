"""Private, dated sector notes displayed independently of trading strategies."""
from __future__ import annotations

import datetime as dt
import html
import re
from urllib.parse import urlsplit

from research_modules import TZ, content_hash, stamp
from research_store import research_path, read_json, atomic_json, update_lock


def validate(data, now):
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Invalid topic schema')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,39}', data.get('topic_id', '')):
        raise ValueError('Invalid topic identifier')
    cutoff, generated, expiry = (stamp(data.get(k)) for k in ('as_of', 'generated_at', 'valid_until'))
    if not cutoff or not generated or not expiry or not cutoff <= generated <= now or expiry < cutoff:
        raise ValueError('Invalid topic clocks')
    if data.get('status') not in ('complete', 'partial', 'unavailable'):
        raise ValueError('Invalid topic status')
    for key in ('title', 'summary'):
        if not isinstance(data.get(key), str):
            raise ValueError('Missing topic text')
    for key in ('changes', 'missing'):
        if not isinstance(data.get(key), list) or any(not isinstance(x, str) for x in data[key]):
            raise ValueError('Invalid topic notes')
    rows = data.get('directions')
    if not isinstance(rows, list) or len(rows) > 40:
        raise ValueError('Invalid topic directions')
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in
                                          ('name', 'industry_state', 'market_state', 'evidence', 'counter', 'next')):
            raise ValueError('Invalid direction')
    for source in data.get('sources', []):
        if not isinstance(source, dict) or any(not isinstance(source.get(k), str) for k in
                                              ('label', 'url', 'published_at', 'observation_period')):
            raise ValueError('Invalid source')
        urlsplit(source['url'])  # Reject malformed URLs before rendering this topic.
        retrieved = stamp(source.get('retrieved_at'))
        if (source.get('retrieved_at') is not None and not retrieved) or (retrieved and retrieved > generated):
            raise ValueError('Source retrieved after research')
    return data


def publish_topic(data, *, now=None):
    now = now or dt.datetime.now(TZ)
    validate(data, now)
    folder = research_path('topics') / data['topic_id']
    digest = content_hash(data)
    with update_lock():
        old = read_json(folder / 'current.json', {})
        if stamp(old.get('generated_at')) and stamp(old['generated_at']) > stamp(data['generated_at']):
            return {'changed': False, 'reason': 'newer research already published'}
        if old and content_hash(old) == digest:
            return {'changed': False, 'reason': 'same evidence'}
        if old:
            atomic_json(folder / 'history' / (content_hash(old) + '.json'), old)
        atomic_json(folder / 'current.json', data)
    return {'changed': True, 'fingerprint': digest}


def render_topics(*, now=None):
    now = now or dt.datetime.now(TZ)
    escape = lambda x: html.escape(str(x), quote=True)
    sections = []
    for path in sorted(research_path('topics').glob('*/current.json')):
        try:
            data = validate(read_json(path), now)
        except (ValueError, TypeError, AttributeError, KeyError):
            sections.append('<p class="notice">一份独立专题资料不可用；其他研究继续展示。</p>')
            continue
        state = ('观察窗口已结束，以下仅为历史研究' if stamp(data['valid_until']) < now else
                 '资料不足，等待核验' if data['status'] != 'complete' else '本轮专题研究已完成')
        rows = ''.join('<tr>' + ''.join(f'<td>{escape(row[k])}</td>' for k in
                       ('name', 'industry_state', 'market_state', 'evidence', 'counter', 'next')) + '</tr>'
                       for row in data['directions'])
        changes = ''.join(f'<li>{escape(x)}</li>' for x in data['changes'])
        missing = ''.join(f'<li>{escape(x)}</li>' for x in data['missing'])
        sources = []
        for source in data.get('sources', []):
            url = urlsplit(source['url'])
            if url.scheme not in ('https', 'http') or not url.netloc or url.username or url.password:
                continue
            sources.append(f'<li><a href="{escape(source["url"])}" target="_blank" rel="noopener noreferrer">'
                           f'{escape(source["label"])}</a> · 发布 {escape(source["published_at"])}'
                           f' · 观察期 {escape(source["observation_period"])} · 取得 {escape(source.get("retrieved_at") or "原取得时间未记录，不以本次生成时间替代")}</li>')
        sections.append(f'''<section class="playbook" id="topic-{escape(data['topic_id'])}">
<h2>{escape(data['title'])}</h2><p class="notice">{state}。静态专题不授予交易资格。</p>
<p>行情截止 {escape(data['as_of'])} · 本次计算 {escape(data['generated_at'])} · 观察期限 {escape(data['valid_until'])}</p>
<p>{escape(data['summary'])}</p><h3>相较上次的变化</h3><ul>{changes}</ul>
<div style="overflow-x:auto"><table><thead><tr><th>方向</th><th>产业状态</th><th>市场状态</th><th>依据</th><th>反证与缺口</th><th>下一步</th></tr></thead><tbody>{rows}</tbody></table></div>
<details><summary>来源时间与未验证条件</summary><ul>{''.join(sources)}</ul><ul>{missing}</ul></details>
</section>''')
    return ''.join(sections)
