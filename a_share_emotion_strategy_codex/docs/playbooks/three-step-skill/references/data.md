# 数据、模型与证据契约

## 模型来源

- [AKShare股票文档](https://akshare.akfamily.xyz/data/stock/stock.html#筹码分布)说明 `stock_cyq_em` 接口；在线文档会升级，**执行固定本地1.18.60版本**，不随网页升级。
- `vendor/cyq.js` 逐字提取该版本 `stock_feature/stock_cyq_em.py` 的 `html_str`；源文件和内核SHA256登记在 `vendor/model.json`。许可证为MIT，完整版权与许可保留于 `vendor/LICENSE`。算法来源记录为[上游源码](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_cyq_em.py)，该链接不是版本锁；锁由已核验安装版本和文件指纹实现。
- 内核价格网格150格、最小精度0.01，按日换手衰减并分布OHLC估算成本。这些是模型假设，不证明真实持仓分布。源注释 `this.range=120` 不执行；保持原文。
- 固定210根完整日K，前复权价格、原生换手百分数。运行器只执行受指纹约束的内核，传入JSON不能成为代码。模型输出 `benefitPart` 为0—1，不能把80当成0.80，也不能混用同花顺、其他供应商或另一窗口。

## 原生输入JSON

所有 `verified:true` 只能由可复查证据支持；写一个布尔值本身不是核验。

顶层字段：`signal_date`、`calendar`、`universe`、`stocks`、`sources`、`missing`。

- `calendar`: `verified:true`、`source`、升序唯一的ISO交易日`days`，涵盖输入窗口和下一交易日。工作台导入会替换为项目已核交易所日历，防止导入文件漏日。独立脚本使用者须先核验该日历。
- `universe`: `date`=D、`verified`、`source`、`expected_count`、完整主板`codes`；只有全目录、全部状态及所有合格证券换手覆盖齐全才确认排名。历史目录必须为当时截面，当前幸存者池不能回测历史。
- 每个stock有六位`code`、`name`、`state`、`turnover`、`daily`、`history`、`sources`、可选`risk`。
- `state`: `date`、`source`、`verified`，及明确布尔 `ordinary_a/st/delisting/suspended/normal_limit`。`conflict:true` 阻断核验。当前和历史同一结构，未知字段不能默认False。
- `turnover`: `date`、`source`、`verified`、`volume_shares`（股）、`float_a_shares`（当日有效流通A股数）、`denominator:"circulating_a_shares"`；不是总股本或自由流通股本。
- `daily`: `provider:"eastmoney"`、`adjustment:"qfq"`、`adjustment_as_of:D`、`source`、`verified`、`bars`。每根含 `date/open/high/low/close/volume_shares/amount_cny/turnover_pct/complete:true`。保存同一复权截面来源和输入SHA。小数可以用字符串保留精度；若来源已降精度，不能再声称原始更高精度。
- `history`: 逐日 `date/state/source/verified/adjustment:"none"/close/limit_up`。当日有效涨停价必须实际核验。60日完整记录含停牌/ST等排除日；已知排除日无需虚构价格。四价中的high只作风险背景，不用于“收盘封板”。
- `sources`: 可公开的来源标题与HTTPS链接；原始响应、抓取时钟、源数据日期、私有文件和审计详情留在本机。`risk`不包含账户成本、数量或凭据。

引擎只接收证据并重算，不导入“已通过”的结果标记或外部筹码数字。输出保留全部逐股记录、三步计数、覆盖、模型及输入指纹。无资格是有效结果，但仅在全池/关键证据完整时称“完成但空池”。

## 采集与缓存

东财日K固定 `klt=101/fqt=1/lmt=210/end=D`；f51日期，f52开，f53收，f54高，f55低，f56手转股×100，f57元，f61原生换手百分数。核对证券代码、响应结构、末日和日线完成状态。请求时钟不能替代源日期；不能用未复权输入计算前复权筹码。

接口最多两次尝试；只用同供应商、同信号日、同复权参数且指纹一致的缓存。全局源故障先停止昂贵详情采集，报告已收目录、报价及阻断；缺210根、历史涨停价、有效股本或状态时不补造。当前公开报价采集器只提供待核验背景，不能自动证明证券法定状态和股本有效期。导入人工/授权数据源核验后的完整原生包可执行完整筛选；不宣称仅凭公开目录已经完成全市场选股。

## 私有发布与时效

模块 `three-step` 独立发布；失败尝试不覆盖最后完整成功报告。盘中仍标注昨日收盘；下一完整收盘后旧记录标历史，不能因重新打开页面获得新资格。页面只读已保存报告，八个入口共享版本检查和阅读位置恢复。本技能不改变旧定时模块集合、旧策略规则或龙空龙资金监控名单。
