# A股研究工作台

这是供当前电脑使用的只读研究页面。它把热点、龙空龙、三套策略和时点报告放在一起，记录依据、等待条件和风险，不管理账户，也不自动下单。

本项目提交到公开 `strategyCli` 仓库的内容仅限脱敏源码、可复用规则和人工示例。实际行情、研究名单、报告、图片、个人参数和监控状态留在本机，不随 Git 提交。

## 页面六个入口

| 入口 | 用途 | 必须分清的边界 |
| --- | --- | --- |
| 热点竞价 | 查看已核验热点、原始名次、独立最终竞价数据 | 收盘涨停池排名不等于全市场排名；没有最终竞价数据就不能宣称竞价已确认 |
| 龙空龙 | 记录观察股、回封证据、按日放量次数、退出与空仓原因 | 排名不自动授予建仓资格；资金分类不等于真实主力账户 |
| 一触即发 | 原生三分支筛选、首板锚点、退潮证据及独立盘中复核 | 收盘预资格不是盘中参与信号；历史重放不可充当live |
| 启动前潜伏 | V3.4数值筛选、冻结平台、最近压力、净空间及四类清单 | 局部初筛不是全市场核心排名；费用或关键证据未知只观察 |
| 我的策略 | 分别阅读启动前潜伏 V3.4、一触即发、龙空龙 | 三套规则独立，不互借门槛、资金参数或退出条件 |
| 时点报告 | 阅读五时点记录、板块异动及有日期的专题研究 | 议息专题放在这里；历史研究不能冒充当前信号 |

原个股计划和 ETF 压舱石不再作为页面或账户处理功能运行。板块研究使用的公共 ETF 行情、份额变化代理仍可保留，与个人 ETF 账户无关。

## 本机查看

在本项目目录中运行：

```sh
python3 build_investment_site.py
python3 dashboard_server.py
```

构建读取已保存的研究，不会重新扫描行情。日期可以省略；需要指定报告日期时使用 `--date YYYY-MM-DD`。缺少报告的时点显示等待，不借用账户文件或伪造日报。

服务默认只监听 `127.0.0.1:8767`，浏览器打开 `http://127.0.0.1:8767/latest.html`。不监听局域网或公网。服务关闭、电脑休眠、网络异常时，页面必须显示离线、过期或数据不足。

页面读取以下本地只读接口：

- `/api/monitor-status`：监控心跳、行情截止及覆盖状态。
- `/api/monitor-events`：已记录的板块异动等允许展示的事件。
- `/api/dashboard-version`：页面内容版本，供浏览器发现更新。

接口不启动交易，不接受任意文件路径，不返回通知凭据、账户或原始私有配置。打开页面并不等于开启监控；行情采样沿用已有本机服务，不重复创建扫描进程或定时任务。服务说明见 [SECTOR_MONITOR.md](SECTOR_MONITOR.md)。

## 更新研究

相关提问完成后，只要热点、龙空龙、一触即发或启动前潜伏任一模块的名单、排序、条件、风险、来源或数据质量发生实质变化，就发布该模块的私有快照，再重建页面。不要只修改生成的 HTML，否则后续构建会覆盖内容。

```sh
python3 refresh_research.py --module all --phase auto
python3 refresh_research.py --module hot --phase prepare
python3 refresh_research.py --module yichujifa --input "$AUTOSTRATEGY_PRIVATE_ROOT/native/report.json" --evidence "$AUTOSTRATEGY_PRIVATE_ROOT/native/evidence.json"
```

`--module`支持 `hot/dragon/yichujifa/prelaunch/all`；`--phase`支持 `auto/prepare/intraday/close`；`--as-of`接受上海时间的分析时点，不允许未来时间。历史重放使用截止时已存的原始资料，不能用当前接口补造历史。导入只接受工作台私有目录或本机一触即发运行目录内的JSON，不开放网页执行接口。

一触即发盘中导入还需 `--live-review` 与 `--snapshots`；潜伏可以用 `--input` 原始数据包加 `--enrichment` 已查阅并绑定信号日/数据/规则指纹的补证。缺证时保持观察；不得为了通过而填入未经核验的标记。一触即发安装缺失则报告不可用，空数据构建不需要安装技能。

各模块完成后独立发布，记录运行启动时间，拒绝迟到任务覆盖新结果。采集失败保留历史但停止当前资格；构建失败保留上一版页面并返回失败状态。正常空池和接口失败分别展示。原始数据、最后尝试与历史快照保存在各模块私有目录。

- 热点快照通过 `research_path('hot_sectors/current.json')` 读取。
- 龙空龙快照通过 `research_path('dragon/current.json')` 读取。
- 一触即发、潜伏快照分别在 `research_path('yichujifa/current.json')`、`research_path('prelaunch/current.json')`。
- 每条研究分别保留分析时间、行情截止、适用窗口、来源、缺失项及规则版本。
- 专题保存为有日期的独立记录，放在时点报告中，不自动加入任何交易观察池。
- 没有实质变化时不制造新版本；仅刷新页面或重写生成时间不算研究更新。

潜伏首次执行默认有界复核最多240只、详情最多30只（核心仍最多10），按代码等距抽样只是采集预算，不是排名；覆盖不足必须标局部扫描。可在专用采集入口用 `--max-stocks` 显式扩大复核范围，不改变规则门槛。市场基准固定沪深300，行业归属/广度、证券状态、历史实际涨停、最近压力及成本不能从形态推断。三种策略各自的日量、分钟量、冻结期限和退出条件保持独立。

现有9:00准备、9:55盘中复核、15:40收盘任务增加模块发布步骤，不新增计划。页面可见时每30秒读取版本，保留入口、查找条件、展开证据和阅读位置；这不是每30秒重新选股。每条盘中通过状态与潜伏原冻结期限独立到期，旧记录不继续显示当前参与。

纯数据更新只留在本机，**不产生 Git 提交**。代码或规则确有变化时，完成验证后只提交允许公开的文件，并推送到 `origin` 的 `codex/streamline-investment-dashboard` 分支。详细协作要求见 [AGENTS.md](AGENTS.md)。

## 数据放在哪里

`research_store.py`统一管理路径。默认工作目录由 `Path.home()` 下的 `Library/Application Support/AutoStrategy/workbench` 组成；测试可用 `AUTOSTRATEGY_PRIVATE_ROOT` 指向临时目录。

| 接口 | 内容 |
| --- | --- |
| `research_path(relative)` | 当前研究与有日期的专题快照 |
| `private_path('reports')` | 日报 HTML、JSON 及本机工作台产物 |
| `private_path('data/cache')` | 公共行情的本地缓存 |
| `private_path('cache/sector_flow')` | 板块资金历史快照 |
| `load_config(name)` | 优先读取私有 `config/`，不存在时读取 `examples/config/` |

历史项目已做校验归档，归档位于 `Path.home()` 下的 `Library/Application Support/AutoStrategy/archive`。历史账户与原始文件只作本地留存，不重新加入网页或仓库。

`.gitignore`只是第一层保护。提交前仍需逐个核验文件；不得把 `inputs/`、`data/`、`reports/`、`site/`、实际研究图片或整个私有目录公开。

## 规则与验证

三策略登记在 [docs/playbooks/registry.json](docs/playbooks/registry.json)，各自完整说明见 [STRATEGY.md](STRATEGY.md)。规则版本与研究结果分开记录；公开分数和阈值是研究假设，不是胜率或收益保证。

运行离线回归：

```sh
python3 -m unittest discover -s tests -v
```

测试应使用人工行情和临时目录，不读取实际持仓、不抓实时行情、不发送通知。回归覆盖交易日历、历史量能单位、数据过期、规则资格、退出计数、事件去重、私有存储及页面接口。测试通过不等于收益回测。

日常报告入口仍为 `run_report.py`，五个时点为 09:00、10:30、13:30、14:30、19:00。真实运行会访问行情并写入本机研究，详见 [CODEX_TASK.md](CODEX_TASK.md)；不要把它当作纯页面构建命令。
