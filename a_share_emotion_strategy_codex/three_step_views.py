"""Read-only three-step results; never import a feed or execute the skill here."""
import datetime as dt
from research_modules import load_module, TZ
from research_store import research_path, read_json
from research_views import e, lines, links, evidence_chart

NAMES={'pass':'通过','fail':'淘汰','pending':'待验证','skipped':'未进入'}


def percent(value):
    return f'{value*100:.4f}%' if isinstance(value,(int,float)) else '待验证'


def chip_chart(row):
    chip=row.get('chip') or {}
    points=[p for p in chip.get('history',[]) if isinstance(p.get('fraction'),(int,float)) and 0<=p['fraction']<=1
            and chip.get('start','')<=p.get('date','')<=chip.get('end','')]
    if not points:
        return '<p class="chart-missing">获利比例历史未取得；不使用价格、资金流或虚构曲线代替。</p>'
    xy=' '.join(f'{50+i*640/max(1,len(points)-1):.2f},{150-p["fraction"]*120:.2f}' for i,p in enumerate(points))
    grid=''.join(f'<line x1="50" x2="690" y1="{150-n*120}" y2="{150-n*120}" class="chart-grid"/><text x="8" y="{154-n*120}">{n*100:.0f}%</text>' for n in (0,.6,.7,.8,1))
    table=''.join(f'<tr><td>{e(p["date"])}</td><td>{percent(p["fraction"])}</td></tr>' for p in points)
    return f'<figure class="evidence-chart"><svg viewBox="0 0 740 180" role="img" aria-label="模型估算获利比例历史"><title>模型估算获利比例，不是真实账户盈利</title>{grid}<polyline fill="none" stroke="#286778" stroke-width="2" points="{xy}"/><text x="50" y="174">{e(points[0]["date"])}</text><text x="690" y="174" text-anchor="end">{e(points[-1]["date"])}</text></svg><figcaption>{e(chip.get("history_note"))}</figcaption></figure><details><summary>估算数值、窗口与指纹</summary><p>{e(chip.get("start"))} 至 {e(chip.get("end"))} · 210根 · 前复权 · 输入 {e(chip.get("input_sha256"))}</p><div class="table-scroll"><table><thead><tr><th>日期</th><th>估算获利比例</th></tr></thead><tbody>{table}</tbody></table></div></details>'


def render_row(row, as_of, *, full=False):
    m=row.get('metrics') or {}
    steps=row.get('steps') or []
    bucket='qualified' if row.get('research_passed') else 'pending' if any(s.get('state')=='pending' for s in steps) else 'other'
    badge={'qualified':'收盘条件通过 · 仅观察','pending':'待验证','other':'条件淘汰'}[bucket]
    decisions=''.join(f'<li>第{i+1}步 · {e(NAMES.get(s.get("state"),"待验证"))}：{e(s.get("reason"))}</li>' for i,s in enumerate(steps))
    count_text=(f'至少{m.get("limit_close_count",0)}次；另有{len(row["missing_limit_dates"])}日待核验'
                if row.get('missing_limit_dates') else str(m.get('limit_close_count','待验证')))
    values=f'<dl class="candidate-metrics"><div><dt>三日累计涨幅</dt><dd>{percent(m.get("three_day_return"))}</dd></div><div><dt>估算获利比例</dt><dd>{percent(m.get("profit_fraction"))}</dd></div><div><dt>全池换手名次</dt><dd>{e(m.get("turnover_rank") or "待验证")}</dd></div><div><dt>60日收盘封板</dt><dd>{e(count_text)}</dd></div></dl>'
    charts=''
    if full or row.get('bars') or row.get('chip'):
        plotted=dict(row,bars=[{k:float(v) if k in ('open','close','high','low','volume_shares','amount_cny') else v for k,v in b.items()} for b in row.get('bars',[])])
        charts='<p class="muted">价格采用信号日同截面前复权；成交量单位为股。封板计数另用未复权价格核验。</p>'+evidence_chart(plotted,as_of)+chip_chart(row)
    dates='、'.join(m.get('limit_close_dates',[])) or '尚无已核验日期'
    context=f'<p>近5日 {percent(m.get("return_5d"))} · 近20日 {percent(m.get("return_20d"))} · 日量/前5日均量 {e(m.get("volume_vs_prior5_mean", "待验证"))} · 20日价格区间位置 {percent(m.get("position_20d"))}。这些是背景，不是额外加分或买卖门槛。</p>'
    return f'<details id="three-step-{e(row.get("code"))}" class="candidate-card" data-candidate data-bucket="{bucket}" data-search="{e(row.get("code"))} {e(row.get("name"))}" {"open" if full else ""}><summary>{e(row.get("name"))} · {e(row.get("code"))} <span class="candidate-state">{badge}</span></summary>{values}<ol>{decisions}</ol><p>收盘封板日期：{e(dates)}</p>{charts}<h4>风险与缺口</h4>{context}<ul>{lines(row.get("risk"))}{lines(row.get("missing"))}</ul><p>{links(row.get("sources",[]))}</p></details>'


def render_page(now=None):
    now=now or dt.datetime.now(TZ)
    state=load_module('three-step',now)
    r=state['data']
    if not r:
        title='最近执行失败' if state['state']=='unavailable' else '尚未执行'
        return '<aside class="notice"><b>'+title+'</b><p>'+e(state['note'])+'</p><a href="#strategy-three-step">阅读完整规则</a></aside>'
    cov=r.get('coverage',{})
    labels={'complete':'条件通过','empty':'完成筛选但空池','partial':'数据不足','expired':'历史结果','unavailable':'最近执行失败'}
    funnel=''.join('<tr>'+''.join(f'<td>{e(f.get(k,0))}</td>' for k in ('stage','entered','pass','fail','pending','skipped'))+'</tr>' for f in r.get('funnel',[]))
    stats={'收到主板目录':cov.get('roster_received'),'预计主板范围':cov.get('roster_expected'),
           '证券状态已核验':cov.get('security_verified'),'同日换手已核验':cov.get('turnover_verified'),
           '三日价格已计算':cov.get('price_computed'),'筹码已计算':cov.get('chip_computed')}
    metrics=''.join(f'<div><dt>{e(k)}</dt><dd>{e(v if v is not None else "未知")}</dd></div>' for k,v in stats.items())
    rows=r.get('rows',[])
    final=sorted((x for x in rows if x.get('research_passed')),key=lambda x:(x['metrics'].get('turnover_rank') or 999999,x.get('code','')))
    others=[x for x in rows if not x.get('research_passed')]
    model=r.get('model',{})
    cards=''.join(render_row(x,r.get('as_of'),full=True) for x in final)
    rest=''.join(render_row(x,r.get('as_of')) for x in others)
    progress_names={'all_a_roster_expected':'供应商全A目录预期数','all_a_roster_received':'实际取得全A目录数',
                    'mainboard_roster_received':'主板范围目录数','quote_received':'取得报价数','signal_day_quotes':'属于信号日的报价数',
                    'verified_security':'完成逐项证券状态核验','daily_probe_count':'东财日K连通性探测股票数',
                    'daily_probe_success':'取得目标日K线的探测数','daily_retry_failures':'日K请求失败次数','same_day_cache_used':'使用同源同日缓存'}
    progress=''.join(f'<li>{e(progress_names.get(k,k))}：{e(v)}</li>' for k,v in r.get('acquisition',{}).items())
    last=read_json(research_path('three_step/last_success.json'),{})
    last_note=('最后一次完整成功信号日 '+e(last.get('signal_date'))+'；没有用旧结果替代本次缺失。') if last else '尚无完成全部必要证据核验的成功研究；当前不完整尝试单独保存。'
    return f'''<div data-module="three-step" data-module-until="{e(r.get('valid_until'))}">
<aside class="notice research-status"><b>{e(labels.get(state['state'],state['state']))}</b><p class="module-validity">{e(state['note'])}</p><p>信号日 {e(r.get('signal_date'))} · 执行 {e(r.get('generated_at'))} · 最近尝试 {e(state['attempt'].get('attempted_at'))}</p><p>{e(r.get('summary'))}</p><p>{last_note}</p><ul>{lines(r.get('missing'))}</ul></aside>
<dl class="candidate-metrics">{metrics}<div><dt>研究确认通过（历史截面）</dt><dd>{len(final)}只</dd></div><div><dt>仍在观察窗口</dt><dd class="qualified-count">{len(final) if state['state'] not in ('expired','unavailable') else 0} 条</dd></div></dl>
<p class="muted">规则 {e(r.get('rule_version'))} · {e(model.get('version'))} · 210根前复权日K · 模型估算0—100%，不是同花顺数值、真实账户盈利率或主力持仓。</p>
<h2>三步筛选漏斗</h2><div class="table-scroll"><table><thead><tr><th>步骤</th><th>进入</th><th>通过</th><th>淘汰</th><th>待验证</th><th>前步未通过</th></tr></thead><tbody>{funnel}</tbody></table></div>
<p>全池换手排名：{'已核验' if cov.get('global_rank_verified') else '未核验，不确认前500'}。最终获利比例须严格大于80%；未进入不等于本步淘汰。</p>
<details id="three-step-collection"><summary>采集进度与来源指纹</summary><ul>{progress}</ul><p>规则 {e(r.get('rule_hash'))}<br>模型内核 {e(model.get('kernel_sha256'))}<br>输入 {e(r.get('input_fingerprint'))}</p>{links(r.get('sources',[]))}</details>
<div class="research-toolbar"><label>搜索股票 <input type="search" data-research-search="three-step" placeholder="名称或代码"></label><label>状态 <select data-research-filter="three-step"><option value="all">全部</option><option value="qualified">条件通过</option><option value="pending">待验证</option><option value="other">淘汰或历史</option></select></label><span data-result-count>{len(rows)} 条记录</span></div><p class="filter-empty" hidden>没有匹配记录。</p>
<h2>全部最终候选 · 按全池换手名次</h2><div class="research-group">{cards or '<p>暂无已确认通过项。若数据不足，不能据此认定市场没有合格股票。</p>'}</div>
<details id="three-step-other-records"><summary>其余筛选记录 · {len(others)}只</summary><div class="research-group">{rest}</div></details>
<p>{links(r.get('sources',[]))}</p><p class="notice">这是收盘观察筛选，不是买入指令。高换手与高获利比例可能伴随兑现风险；本策略尚无收益优势证明。普通A股受T+1与涨跌停成交约束。</p><a href="#strategy-three-step">查看规则与执行边界</a></div>'''
