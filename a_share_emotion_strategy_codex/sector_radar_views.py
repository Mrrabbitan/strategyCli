"""Read-only sector radar presentation; no feeds, skill execution or raw exports."""
from __future__ import annotations

import datetime as dt
import hashlib
import re

from research_modules import load_module, stamp, TZ
from research_views import e, finite, lines, links, evidence_chart

STRATEGIES = (
    ('prelaunch', '启动前潜伏 V3.4'), ('yichujifa', '一触即发'),
    ('dragon', '龙空龙'), ('late-day', '尾盘隔夜'), ('three-step', '三步选股'),
)
CHECK_NAMES = {'passed': '观察条件通过', 'failed': '不通过', 'pending': '待验证',
               'not_applicable': '不适用阶段', 'not_run': '未执行'}
STATE_NAMES = {'complete': '本轮研究完成', 'partial': '研究部分完成 · 证据待补',
               'empty': '筛选完成 · 正式观察为空', 'expired': '历史研究',
               'unavailable': '本轮研究受阻', 'not_run': '尚未执行'}
SECTOR_METRICS = (
    ('return_3d', '三日组合收益', 'pct'), ('relative_3d', '三日相对沪深300', 'pct'),
    ('relative_improvement', '三日相对强度改善', 'pct'), ('breadth_ma20', 'MA20上方广度', 'pct'),
    ('breadth_improvement', '广度三日改善', 'pct'), ('amount_ratio', '近三日 / 前20日成交额', 'ratio'),
    ('skill_pass_fraction', '独立策略观察通过占比', 'pct'),
    ('return_5d', '五日组合收益', 'pct'), ('return_20d', '二十日组合收益', 'pct'),
    ('leading_concentration', '领涨集中度', 'pct'),
)

TOP_COVERAGE = {'sectors_expected': '目标板块', 'membership_verified': '成分已核验板块',
                'sectors_complete': '证据完整板块', 'unique_stocks': '去重股票',
                'watch_count': '原生观察通过', 'matrix_complete': '五策略校验完整',
                'forward_ready': '板块前瞻可比较', 'observation_complete': '至少一项资格判断完整',
                'announcement_complete': '公告风险复核完整'}
SECTOR_COVERAGE = {'complete': '覆盖完整', 'membership_verified': '成分已核验',
                   'expected': '源总数', 'received': '收到成分', 'active': '合格主板',
                   'unknown': '资格待验证', 'history': '资格已核验主板中的完整日线'}


def coverage_line(coverage, labels):
    return ' · '.join(e(label) + '：' + e(coverage.get(field)) for field, label in labels.items() if field in coverage)


def mapping(value):
    return value if isinstance(value, dict) else {}


def records(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def texts(value):
    return [item for item in value if isinstance(item, (str, int, float))] if isinstance(value, list) else []


def pct(value):
    return f'{value * 100:.2f}%' if finite(value) else '待验证'


def number(value):
    return f'{value:,.2f}' if finite(value) else '待验证'


def key(value):
    """Stable anchors cannot contain supplier markup or local paths."""
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def stock_anchor(code):
    return 'radar-stock-' + (code if re.fullmatch(r'\d{6}', str(code)) else key(code))


def check_label(check, historical=False):
    status = check.get('status')
    # A passed check is never enough without the native observation assertion.
    if status == 'passed' and check.get('observation_passed') is not True:
        label = '待验证 · 观察资格未确认'
    else:
        label = CHECK_NAMES.get(status, '待验证')
    return ('历史判断 · ' if historical else '') + label


def observed(row):
    checks = mapping(row.get('strategy_checks'))
    return row.get('state') == 'watch' and any(
        isinstance(checks.get(sid), dict) and checks[sid].get('status') == 'passed'
        and checks[sid].get('observation_passed') is True for sid, _ in STRATEGIES)


def row_state(row, historical=False):
    if historical:
        return '历史记录 · 不取得当前资格', 'other'
    if observed(row):
        return '收盘观察 · 非买点', 'qualified'
    if row.get('state') == 'excluded':
        return '已排除', 'excluded'
    return '待验证', 'pending'


def group_names(row, sector_names):
    return [sector_names.get(str(g), str(g)) for g in texts(row.get('groups'))]


def safe_chart(row, as_of):
    # Only public evidence fields go to the shared chart renderer.
    bars = []
    for bar in records(row.get('bars')):
        clean = {'date': str(bar.get('date', ''))[:10]}
        for field in ('open', 'high', 'low', 'close', 'volume_shares', 'amount_cny'):
            value = bar.get(field)
            if finite(value):
                clean[field] = value
        bars.append(clean)
    return evidence_chart({'name': row.get('name'), 'bars': bars, 'levels': []}, as_of)


def native_evidence(sid, check, historical):
    """Show native branch / volume audit fields without exporting nested blobs."""
    parts = []
    if check.get('native_status') is not None:
        parts.append('<p><b>原生阶段：</b>' + e(check.get('native_status')) + '</p>')
    if sid == 'yichujifa':
        branches = records(check.get('branches'))
        if branches:
            branch_rows = ''.join('<tr><td>' + e(branch.get('branch')) + '</td><td>'
                                  + e(branch.get('original_rank')) + '</td><td>'
                                  + e(check_label(branch, historical)) + '</td><td>'
                                  + e(branch.get('native_status')) + '</td><td><ul>'
                                  + lines(texts(branch.get('reasons'))) + '</ul></td></tr>' for branch in branches)
            parts.append('<h5>三分支独立判断 · 不跨分支合并资格</h5><div class="table-scroll"><table class="radar-native-table"><thead><tr><th>分支</th><th>原行业名次</th><th>判断</th><th>原生阶段</th><th>原因</th></tr></thead><tbody>' + branch_rows + '</tbody></table></div>')
    if sid == 'dragon':
        parts.append('<p>热点板内原始名次：' + e(check.get('original_rank')) + '。不按本雷达名次替换原资格。</p>')
        ledger = mapping(check.get('volume_ledger'))
        cutoff = stamp(check.get('as_of'))
        days = []
        for day in records(ledger.get('days')):
            at = stamp(day.get('confirmed_as_of') or day.get('date'))
            if cutoff is not None and at is not None and at <= cutoff:
                days.append(day)
        if days:
            table = ''.join('<tr>' + ''.join('<td>' + e(value) + '</td>' for value in (
                day.get('date'), day.get('volume_shares'), day.get('previous_volume_shares'),
                day.get('volume_ratio'), day.get('expansion_count'), day.get('confirmed_as_of'),
                day.get('time_note') or ('完整收盘数据，不倒推盘中触发' if day.get('data_kind') == 'daily_close' else '实际盘中累计量，非估算全天量')
            )) + '</tr>' for day in days)
            parts.append('<h5>逐日放量记录 · 含首板，每日最多一次</h5><div class="table-scroll"><table class="radar-native-table"><thead><tr><th>日期</th><th>成交量 / 股</th><th>前日全天量 / 股</th><th>前日倍数</th><th>累计次数</th><th>确认时间</th><th>时点口径</th></tr></thead><tbody>' + table + '</tbody></table></div>')
            if any(finite(day.get('expansion_count')) and day['expansion_count'] >= 2 for day in days):
                parts.append('<p class="notice">原龙空龙证据已记录第二次放量，退出预警优先；不新增介入资格。</p>')
        else:
            parts.append('<p class="muted">暂无在证据截止时间内可展示的逐日放量记录，不填零、不推断未触发。</p>')
        parts.append('<ul>' + lines(texts(ledger.get('errors'))) + '</ul>')
    return ''.join(parts)


def check_detail(sid, label, check, historical):
    levels = ''.join(f'<li>{e(x.get("label"))}：{number(x.get("value"))} 元</li>'
                     for x in records(check.get('levels')))
    return f'''<section class="radar-check"><h4><a href="#strategy-{sid}">{label}</a><span class="radar-check-state">{e(check_label(check, historical))}</span></h4>
<p class="muted">规则 {e(check.get('rule_version'))} · 证据截至 {e(check.get('as_of'))} · 指纹 {e(str(check.get('rule_hash') or '未提供')[:24])}</p>
<ul>{lines(texts(check.get('reasons'))) or '<li>尚无完整原生校验证据，不推断通过。</li>'}</ul>{native_evidence(sid, check, historical)}
{('<p>关键位置（本策略口径）</p><ul>' + levels + '</ul>') if levels else ''}
{('<p>后续确认</p><ul>' + lines(texts(check.get('confirmation'))) + '</ul>') if check.get('confirmation') else ''}
{('<p>反对证据与风险</p><ul>' + lines(texts(check.get('risks'))) + '</ul>') if check.get('risks') else ''}
<p class="source-links">{links(check.get('sources'))}</p></section>'''


def candidate_detail(row, as_of, sector_names, historical):
    code = str(row.get('code') or '')
    label, _ = row_state(row, historical)
    checks = row.get('strategy_checks') if isinstance(row.get('strategy_checks'), dict) else {}
    sections = ''.join(check_detail(sid, name, checks.get(sid) if isinstance(checks.get(sid), dict) else {}, historical)
                       for sid, name in STRATEGIES)
    return f'''<details class="radar-stock-detail" id="{stock_anchor(code)}" data-radar-detail="{e(code)}"><summary>{e(row.get('name'))} · {e(code)} <span class="candidate-state">{e(label)}</span></summary>
<p>所属板块：{e(' / '.join(group_names(row, sector_names)))}</p>
<ul>{lines(texts(row.get('security_reasons')))}</ul>
<p class="muted">日线采用信号日同截面前复权，成交量单位为股；仅显示已取得证据，图表不授予任何策略资格。五种策略的关键位置、分支与风险分别列于下方。</p>
{safe_chart(row, as_of)}<div class="radar-check-grid">{sections}</div><p>{links(row.get('sources'))}</p></details>'''


def candidate_row(row, sector_names, historical):
    code = str(row.get('code') or '')
    groups = group_names(row, sector_names)
    label, bucket = row_state(row, historical)
    metrics = mapping(row.get('metrics'))
    checks = row.get('strategy_checks') if isinstance(row.get('strategy_checks'), dict) else {}
    values = [e(code), f'<a href="#{stock_anchor(code)}">{e(row.get("name"))}</a>', e(' / '.join(groups)),
              f'<span class="candidate-state">{e(label)}</span>']
    for sid, _ in STRATEGIES:
        check = checks.get(sid) if isinstance(checks.get(sid), dict) else {}
        reasons = '；'.join(str(x) for x in texts(check.get('reasons')))
        values.append(f'<span class="radar-check-state">{e(check_label(check, historical))}</span><small>{e(reasons or "尚无完整证据")}</small>')
    values += [pct(metrics.get('return_5d')), pct(metrics.get('drawdown_5d')),
               pct(metrics.get('close_position_5d')), number(metrics.get('median_amount_5d')),
               e('；'.join(str(x) for x in texts(row.get('security_reasons'))))]
    attrs = f'data-candidate data-radar-code="{e(code)}" data-search="{e(" ".join([code, str(row.get("name") or "")] + groups))}" data-bucket="{bucket}" data-radar-groups="{e(" ".join(key(g) for g in texts(row.get("groups"))))}"'
    return '<tr ' + attrs + '>' + ''.join('<td>' + value + '</td>' for value in values) + '</tr>'


def top_stock(row, sector, historical):
    code = str(row.get('code') or '')
    sid = str(sector.get('id') or '')
    rank = mapping(mapping(row.get('ranks')).get(sid))
    checks = mapping(row.get('strategy_checks'))
    support = [label for strategy, label in STRATEGIES if isinstance(checks.get(strategy), dict)
               and checks[strategy].get('status') == 'passed' and checks[strategy].get('observation_passed') is True]
    return f'''<article class="radar-top-stock"><span class="eyebrow">板内观察 {e(rank.get('rank'))} · 排序分 {number(rank.get('score'))}</span>
<h4><a href="#{stock_anchor(code)}">{e(row.get('name'))} <small>{e(code)}</small></a></h4>
<p class="candidate-state">{'历史观察 · 不取得当前资格' if historical else '收盘观察 · 非买点'}</p>
<p>五日相对本板块 {pct(rank.get('relative_5d'))}；支持策略：{e('、'.join(support))}。</p>
<p class="muted">排序兼看相对强度、回撤、收盘位置与成交额，不按通过策略数量加分。</p><a href="#{stock_anchor(code)}">查看五策略反证、位置与确认条件 →</a></article>'''


def sector_section(sector, candidate_by_code, historical, sector_names=None):
    sid = str(sector.get('id') or '')
    sector_names = sector_names or {}
    provider = {'ths': '同花顺', 'eastmoney': '东方财富', 'em': '东方财富'}.get(sector.get('provider'), sector.get('provider'))
    code_list = texts(sector.get('top_codes'))[:3]
    selected = [candidate_by_code[str(code)] for code in code_list if str(code) in candidate_by_code
                and observed(candidate_by_code[str(code)])]
    cards = ''.join(top_stock(row, sector, historical) for row in selected)
    metrics = mapping(sector.get('metrics'))
    facts = ''.join('<div><dt>' + label + '</dt><dd>' + (pct(metrics.get(field)) if unit == 'pct' else number(metrics.get(field)) + ('×' if finite(metrics.get(field)) else '')) + '</dd></div>'
                    for field, label, unit in SECTOR_METRICS)
    coverage = sector.get('coverage') if isinstance(sector.get('coverage'), dict) else {}
    coverage_text = coverage_line(coverage, SECTOR_COVERAGE)
    counts = ' · '.join(label + '：' + e(metrics.get(field)) for field, label in (('limit_up_count', '收盘封板'), ('broken_limit_count', '触板回落')))
    overlap = '、'.join(sector_names.get(str(item.get('sector_id')), str(item.get('sector_id'))) + ' ' + str(item.get('count')) + '只' for item in records(sector.get('overlap')))
    return f'''<section class="radar-sector" id="radar-sector-{key(sid)}"><header><div><span class="eyebrow">{'近似分类 · 范围不同' if sector.get('approximate') else '原分类范围核验'}</span><h3>{e(sector.get('label'))}</h3></div><span>正式观察 {len(selected)} / 最多3只</span></header>
<p class="muted">实际供应商 {e(provider)} · 板块代码 {e(sector.get('provider_code'))} · 成分取得时点 {e(sector.get('membership_as_of'))}</p>
{('<p class="notice">分类差异：' + e(sector.get('difference')) + '</p>') if sector.get('difference') else ''}
<p class="coverage-line">{coverage_text or '成分完整性待核验；不以现有记录数代替完整数量。'}</p>
<dl class="candidate-metrics">{facts}</dl><p class="muted">{counts}。未知不记为0。{e("成分重叠：" + overlap) if overlap else ""}</p><div class="radar-top-grid">{cards or '<p class="notice">暂没有已完整核验的正式观察股；缺证据与筛选后空池请以本板块状态为准，不补位。</p>'}</div>
<details id="radar-sector-audit-{key(sid)}"><summary>本板块缺口、启动确认与反证</summary>
<ul>{lines(texts(sector.get('missing'))) or '<li>没有额外列明缺口；完整性仍以本次覆盖核验为准。</li>'}</ul>
<h4>启动确认</h4><ul>{lines(texts(sector.get('confirmation'))) or '<li>暂未形成充分确认条件。</li>'}</ul>
<h4>反证与失效</h4><ul>{lines(texts(sector.get('risks'))) or '<li>观察排序未经收益验证，不承诺启动。</li>'}</ul><p>{links(sector.get('sources'))}</p></details></section>'''


def forward_section(data, sectors, historical):
    indexed = {str(s.get('id')): s for s in sectors}
    picked = []
    for entry in data.get('forward_top') or []:
        sid = str(entry.get('id') or entry.get('sector_id') or '') if isinstance(entry, dict) else str(entry)
        if sid in indexed and sid not in [x.get('id') for x in picked]:
            picked.append(indexed[sid])
    cards = []
    for sector in picked[:3]:
        cards.append(f'''<article><span class="eyebrow" data-radar-forward-label>{'历史前瞻' if historical else '未来1—5个交易日优先复核'} · {e(sector.get('forward_rank'))}</span><h3><a href="#radar-sector-{key(sector.get('id'))}">{e(sector.get('label'))}</a></h3><p>研究排序分 {number(sector.get('forward_score'))} · 不代表启动概率</p><ul>{lines(texts(sector.get('confirmation')))}</ul><p class="muted">反证与失效</p><ul>{lines(texts(sector.get('risks')))}</ul></article>''')
    return '<section class="radar-forward"><h2>板块前瞻 · 最多三个方向</h2><p>只比较截图中的12类；以过滤后成分构建等权研究组合，不冒充供应商指数或全市场排名。</p><div class="radar-top-grid">' + (''.join(cards) or '<p class="notice">未发布板块前瞻前三：可能是比较覆盖不足或条件不满足。查看逐板块证据与本次缺口，不据此推断没有机会。</p>') + '</div></section>'


def focus_stock(row, historical):
    label = '潜伏核心观察' if row.get('tier') == 'core' else '待启动形态 · 待验证'
    if historical:
        label = '历史复核 · 不取得当前资格'
    return f'''<article class="radar-top-stock radar-focus-stock"><h4><a href="#{stock_anchor(str(row.get('code')))}">{e(row.get('name'))} <small>{e(row.get('code'))}</small></a></h4>
<p class="candidate-state">{e(label)} · 非买点</p>
<p>收盘 {number(row.get('price'))} 元 · 5日 {number(row.get('return_5d_pct'))}% · 20日 {number(row.get('return_20d_pct'))}%</p>
<p>试盘日 {e(row.get('probe_date'))} · 当前量 / 前20日中位量 {number(row.get('volume_ratio'))}×</p><p>{e(row.get('probe_evidence'))}</p>
<p>冻结支撑 {number(row.get('support'))} 元 · 冻结上沿 {number(row.get('upper'))} 元 · 最近压力 {number(row.get('pressure'))} 元</p>
<p>毛空间比 {number(row.get('gross_rr'))} · 扣费空间比 {number(row.get('net_rr'))}；缺费用不升级核心。</p>
<h5>仍需核验</h5><ul>{lines(texts(row.get('missing'))) or '<li>原生核心条件已核验；后续变化仍需重新确认。</li>'}</ul>
<p>{e(row.get('confirmation'))}</p><h5>失效与转跟踪条件</h5><ul>{lines(texts(row.get('risks')))}</ul>
<p class="source-links">{links(row.get('sources'))}</p><a href="#{stock_anchor(str(row.get('code')))}">查看价格量能图与五策略反证 →</a></article>'''


def prelaunch_focus_section(data, historical):
    focus = mapping(data.get('prelaunch_focus'))
    if not focus:
        return ''
    title = '<section class="radar-focus" id="radar-prelaunch-focus"><h2>低位待启动 · 优先复核</h2>'
    if focus.get('signal_date') != data.get('signal_date') or focus.get('status') == 'unavailable':
        return title + '<p class="notice">未取得同日有效潜伏证据，不能沿用旧优先名单。</p><ul>' + lines(texts(focus.get('missing'))) + '</ul></section>'
    groups = []
    for sector in records(focus.get('sectors')):
        core = [r for r in records(sector.get('entries')) if r.get('tier') == 'core']
        pending = [r for r in records(sector.get('entries')) if r.get('tier') != 'core']
        cards = ''
        if core:
            cards += '<h4>第一组 · 原生核心条件完整</h4><div class="radar-top-grid">' + ''.join(focus_stock(r, historical) for r in core) + '</div>'
        if pending:
            cards += '<h4>形态优先补证 · 尚非正式候选</h4><div class="radar-top-grid">' + ''.join(focus_stock(r, historical) for r in pending) + '</div>'
        if not cards:
            cards = '<p class="notice">本板块暂无同时满足已核验趋势、试盘、缩量承接且无已知否决的待启动形态；留空，不用弱势低价股或已启动股补位。</p>'
        reasons = ''.join('<li>' + e(r.get('reason')) + '：' + e(r.get('count')) + '只</li>' for r in records(sector.get('rejection_reasons'))[:5])
        rejected = '<p>未进入优先复核：' + e(sector.get('rejected_count')) + '只（包括明确否决和必需数据不足，不等同于全板块已完成筛选）。</p><ul>' + reasons + '</ul>'
        near = records(sector.get('near_misses'))
        if near:
            rejected += '<details id="radar-near-miss-' + key(sector.get('id')) + '" open><summary>有近似形态但不入第一池 · 反证</summary>'
            for r in near:
                rejected += ('<p><a href="#' + stock_anchor(str(r.get('code'))) + '">' + e(r.get('name')) + ' ' + e(r.get('code'))
                             + '</a>：收盘 ' + number(r.get('price')) + ' 元，冻结支撑 ' + number(r.get('support')) + ' 元，冻结上沿 '
                             + number(r.get('upper')) + ' 元。</p><ul>' + lines(texts(r.get('failure_reasons'))) + '</ul>')
            rejected += '<p class="muted">只展示形态接近但存在否决的复核例子，按代码顺序，不是备选推荐或收益排名。</p></details>'
        started = records(sector.get('started'))
        tracking = ''
        if started:
            rows = ''.join('<tr><td><a href="#' + stock_anchor(str(r.get('code'))) + '">' + e(r.get('name')) + ' ' + e(r.get('code')) + '</a></td><td>'
                           + number(r.get('price')) + '</td><td>' + number(r.get('return_5d_pct')) + '%</td><td>' + number(r.get('return_20d_pct'))
                           + '%</td><td>' + e(r.get('native_status')) + '</td></tr>' for r in started)
            tracking = '<details id="radar-started-' + key(sector.get('id')) + '"><summary>已启动另行跟踪 · ' + str(len(started)) + '只，不列潜伏优先</summary><p class="muted">突破或加速后的状态保留，涨幅回落不恢复本轮潜伏资格；仍需查看五策略与公告风险。</p><div class="table-scroll"><table><thead><tr><th>股票</th><th>收盘 / 元</th><th>5日</th><th>20日</th><th>原生状态</th></tr></thead><tbody>' + rows + '</tbody></table></div></details>'
        groups.append('<section class="radar-sector"><h3>' + e(sector.get('label')) + '</h3><p class="muted">' + e(sector.get('ordering')) + '。本板块满足形态复核条件 ' + e(sector.get('available_count')) + ' 只，展示最多3只；其余 ' + e(sector.get('omitted_count')) + ' 只保留原生记录。</p>' + cards + rejected + tracking + '</section>')
    scope_note = mapping(data.get('acquisition')).get('scope_note')
    return (title + '<p>先看尚未明显加速、已有温和试盘与缩量承接的形态，不把绝对低价或跌幅大当作启动证据。历史涨停、费用、公告或行业等缺口存在时，仅列待验证。</p>'
            + ('<p class="notice">本轮比较范围：' + e(scope_note) + '。</p>' if scope_note else '')
            + '<p class="muted">行情截至 ' + e(focus.get('as_of')) + ' · 原规则 V' + e(focus.get('rule_version')) + '。'
            + e(focus.get('ordering_note')) + '形态复核名额不是正式前三，也不是上涨概率排名。</p>'
            + ('<p class="notice">本段是历史复核，不取得当前观察或参与资格。</p>' if historical else '')
            + '<ul>' + lines(texts(focus.get('missing'))) + '</ul>' + ''.join(groups) + '</section>')


def render_page(now=None):
    now = now or dt.datetime.now(TZ)
    current = load_module('sector-radar', now=now)
    data = current.get('data') or {}
    state = current.get('state', 'not_run')
    attempt = current.get('attempt') or {}
    historical = state in ('expired', 'unavailable')
    sectors = records(data.get('sectors'))
    sector_names = {str(row.get('id')): str(row.get('label') or row.get('id')) for row in sectors}
    rows = records(data.get('candidates'))
    # Full matrix is one record per code, not one row per overlapping theme.
    unique = {str(row.get('code') or ''): row for row in rows}
    rows = list(unique.values())
    qualified = sum(observed(row) for row in rows) if not historical else 0
    overlap = sum(len(set(texts(row.get('groups')))) > 1 for row in rows)
    missing = texts(data.get('missing'))
    if attempt.get('status') == 'unavailable':
        missing = [attempt.get('summary') or '最近执行失败，未取得本轮完整结果'] + missing
    expiry = stamp(data.get('valid_until'))
    options = ''.join(f'<option value="{key(sid)}">{e(label)}</option>' for sid, label in sector_names.items())
    header = '<tr>' + ''.join('<th scope="col">' + label + '</th>' for label in (
        '代码', '股票', '所属板块', '研究状态', *(name for _, name in STRATEGIES),
        '五日收益', '五日最大回撤', '五日收盘区间位置', '五日成交额中位数 / 元', '证券与公告风险')) + '</tr>'
    matrix = ''.join(candidate_row(row, sector_names, historical) for row in rows)
    details = ''.join(candidate_detail(row, data.get('as_of'), sector_names, historical) for row in rows)
    coverage = data.get('coverage') if isinstance(data.get('coverage'), dict) else {}
    coverage_text = coverage_line(coverage, TOP_COVERAGE)
    strategy_names = dict(STRATEGIES)
    run_names = {'complete': '已执行', 'partial': '部分完成', 'empty': '已执行 · 空池',
                 'unavailable': '数据不可用', 'failed': '执行失败', 'pending': '待验证',
                 'not_run': '未执行', 'historical': '历史复核'}
    executions = ''
    for row in records(data.get('executions')):
        module = row.get('module') or row.get('strategy')
        execution_reasons = texts(row.get('missing')) + texts(row.get('errors'))
        if row.get('summary') or row.get('reason'):
            execution_reasons.insert(0, row.get('summary') or row.get('reason'))
        executions += ('<li><b>' + e(strategy_names.get(module, module)) + ' · '
                       + e(run_names.get(row.get('status'), row.get('status'))) + '</b> · 证据截至 '
                       + e(row.get('as_of')) + ' · 规则 ' + e(row.get('rule_version'))
                       + ('<ul>' + lines(execution_reasons) + '</ul>' if execution_reasons else '') + '</li>')
    return f'''<div class="research-surface radar-surface" data-module="sector-radar" data-module-until="{e(expiry.isoformat() if expiry else '')}" {'data-expired="true"' if historical else ''}>
<div class="research-heading"><div><span class="eyebrow">12类全量成分 / 五策略独立校验</span><h1>板块雷达</h1><p class="lead">{e(data.get('summary') or '等待首次研究。只展示已发布的成分与证据，不以旧名单或其他板块补位。')}</p></div><div class="research-status"><b>{e(STATE_NAMES.get(state, '待验证'))}</b><span>收盘观察 · 非交易建议</span></div></div>
<div class="research-stats"><div><span>信号日 / 行情截至</span><strong>{e(data.get('signal_date') or data.get('as_of'))}</strong></div><div><span>最近执行 · 上海时间</span><strong>{e(attempt.get('attempted_at') or data.get('generated_at'))}</strong></div><div><span>去重股票记录</span><strong>{len(rows)} 条</strong></div><div><span>当前有效收盘观察 / 非买点</span><strong class="qualified-count">{qualified} 条</strong></div></div>
<div class="notice module-validity" role="status">{e(current.get('note'))}<p>观察有效至 {e(data.get('valid_until'))}。网页只读取报告；五项校验相互独立，通过数量不加分，也不代表现在可以买入。</p></div>
<p class="coverage-line">{coverage_text or '完整股票池覆盖尚未核验。'}</p><p><a href="#radar-universe">跳到全部股票与五策略矩阵 ↓</a></p><p class="muted">{overlap} 只存在跨板块重叠，全局按代码去重；板块重复持有可能放大主题集中风险。</p>
{prelaunch_focus_section(data, historical)}
<details class="research-limitations" id="missing-sector-radar" open><summary>本次缺失证据与执行状态</summary><ul>{lines(missing) or '<li>请逐股核对证券资格、公告风险和原策略证据；研究完成不等于收益有效。</li>'}</ul><ul>{executions}</ul></details>
{forward_section(data, sectors, historical)}<section class="radar-sectors"><h2>各板块正式观察 · 最多三只</h2><p class="muted">候选必须由至少一项原策略完整支持；排序依据五日相对强度、回撤、收盘位置与成交额等权百分位，未经收益验证。</p>{''.join(sector_section(sector, unique, historical, sector_names) for sector in sectors) or '<p class="notice">尚未取得本轮板块成分；不展示虚构或过期股票池。</p>'}</section>
<section class="radar-universe" id="radar-universe"><h2>全部股票与五策略矩阵</h2><div class="research-toolbar"><label>查找股票或板块 <input type="search" data-research-search="sector-radar" placeholder="名称、代码或板块" autocomplete="off"></label><label>记录范围 <select data-research-filter="sector-radar"><option value="all">全部记录</option><option value="qualified">收盘观察</option><option value="pending">待验证</option><option value="excluded">已排除</option><option value="other">历史记录</option></select></label><label>所属板块 <select data-radar-group><option value="all">全部板块</option>{options}</select></label><button type="button" class="radar-export" data-radar-export>导出当前筛选 CSV</button><span data-result-count="sector-radar">{len(rows)} 条记录</span></div>
<p class="muted">完整展示全部已取得记录，不截断数量。导出仅包含页面上可见字段，过滤不会重新计算排名。</p><div class="table-scroll radar-matrix"><table data-radar-table><thead>{header}</thead><tbody>{matrix}</tbody></table></div><p class="filter-empty" hidden>没有匹配记录；页面筛选不改变研究结论。</p>{('<p class="notice">本轮暂无可展示股票。数据不足不能写成成功空池。</p>') if not rows else ''}
<div class="radar-stock-evidence">{details}</div></section>
<details id="radar-changes"><summary>历史变化与本轮来源</summary><ul>{lines(texts(data.get('changes'))) or '<li>暂无已核验的历史变化记录。</li>'}</ul><p>{links(data.get('sources'))}</p></details>
<p class="muted">模块版本 {e(data.get('version'))} · 来源与规则指纹 {e(str(data.get('rule_hash') or '未提供')[:24])}。本榜不替换热点原始排名、不改变尾盘冻结范围，也不自动加入资金监测。</p></div>'''
