# 模板 2：行业 / 宽基 ETF 轮动策略（默认模板）

> 适用场景：6-9 只 ETF 池中按多周期动量打分定期持有 Top-N，捕捉行业/风格轮动 alpha。
> 不适用场景：单只 ETF；高频信号；纯波段交易。

> 本 skill 在用户未指定模板时**默认采用本模板**。完整可运行的示例见 [examples/industry-rotation-etf/](../../examples/industry-rotation-etf/)。

---

## 1. 默认标的池建议

总数 8 只 = 1 宽基对照 + 6 行业 + 1 防守候选：

| 角色 | 代码 | 名称 |
|------|------|------|
| 宽基对照 | 510300.SH | 沪深300ETF |
| 行业 | 512170.SH | 医疗ETF |
| 行业 | 512480.SH | 半导体ETF |
| 行业 | 512760.SH | 芯片ETF |
| 行业 | 512800.SH | 银行ETF |
| 行业 | 512690.SH | 酒ETF |
| 行业 | 515030.SH | 新能源车ETF |
| 防守候选 | 510880.SH | 红利ETF |

> 上述均满足"日均成交 ≥ 5000 万、规模 ≥ 5 亿"流动性门槛（截至 2025Q4）。

---

## 2. 指标定义

### 指标 1: 多周期动量分
- 公式：
  ```
  Momentum(t) = w_short × Return(t, lookback_short)
              + w_mid   × Return(t, lookback_mid)
              + w_long  × Return(t, lookback_long)
  ```
  其中 `Return(t, n) = (Close[t] / Close[t-n]) - 1`
- 默认权重：`w_short=0.5, w_mid=0.3, w_long=0.2`
- 默认窗口：`lookback_short=20, lookback_mid=60, lookback_long=120`（约 1/3/6 个月）

### 指标 2: 趋势过滤（MA 60）
- 公式：`Trend(t) = Close[t] > SMA(Close, 60)`
- 含义：仅当价格站上 60 日均线，标的才进入候选

### 指标 3: 防守切换条件
- 当所有标的的 `Momentum < 0` 或入选数量 = 0 时，切换至 `510880.SH 红利ETF` 等权持仓
- 防守仓位上限同 `max_position_pct`

---

## 3. 信号决策树

### 调仓周期
- 默认：**月底**（每月最后一个交易日）执行调仓
- 备选：周频（每周最后一个交易日）

### 调仓日逻辑
```
1. 计算所有 ETF 的 Momentum(t)
2. 应用趋势过滤：保留 Close > MA(60) 的标的
3. 按 Momentum 降序排，取 Top-hold_top_n（默认 3）
4. 计算目标仓位：每只 = total_value × (1 / hold_top_n) × position_scale
5. 卖出"当前持有但不在新 Top-N 内"的标的（受 T+1 约束）
6. 买入"在新 Top-N 内但当前未持有/仓位不足"的标的
7. 同标的当前持仓 vs 目标仓位差 < 偏差容忍 → 不动（减少摩擦）

防守切换：
  if 入选数量 == 0:
      切换至 510880.SH，等权持有
```

### T+1 处理
- 卖出 `S1`、买入 `S2` 同日发生 → **允许**（不同标的）
- 当日刚买入 `S1`，本日不可再卖 `S1`

---

## 4. 仓位管理
- 持仓数量 `hold_top_n = 3`，等权持有
- 每只 ETF 目标仓位 = `total_value × position_scale / hold_top_n`
- 默认 `position_scale = 0.95`（留 5% 现金缓冲）
- 单只仓位上限 35%
- 调仓时不卖出未触发 T+1 限制的盈亏 < `rebalance_threshold` 的小幅偏差（默认 5%）

---

## 5. 风控规则

| 规则 | 默认阈值 | 触发动作 |
|------|---------|---------|
| 单 ETF 仓位上限 | 35% | 调仓时按上限截断 |
| 总仓位上限 | 95% | 留 5% 现金 |
| 个股止损 | 不设 | 轮动策略以排序换仓代替 |
| 最大回撤暂停 | 18% | 全部清仓，暂停 5 个交易日 |
| 极端单日跌幅 | -7% | 暂停该 ETF 3 个交易日 |
| 防守切换 | 入选 = 0 或全负 | 切换至红利 ETF |

---

## 6. 推荐回测参数

```yaml
initial_cash: 1000000
start_date: "2020-01-01"
end_date: "2025-12-31"
benchmark: "000906.SH"   # 中证800（行业策略综合基准）
market: "A股"

commission: 0.0005
stamp_tax: 0
slippage: 0.001

indicators:
  lookback_short: 20
  lookback_mid: 60
  lookback_long: 120
  w_short: 0.5
  w_mid: 0.3
  w_long: 0.2
  ma_period: 60
  hold_top_n: 3
  rebalance_freq: "monthly"   # monthly | weekly
  rebalance_threshold: 5
  position_scale: 0.95

risk:
  max_position_pct: 35
  total_position_pct: 95
  max_drawdown_pct: 18
  extreme_drop_pct: 7
```

---

## 7. 已知局限
- 月频调仓在快速行情切换中反应较慢，错过短期机会
- 动量策略在风格突变（如 2021 末大盘股切到小盘股）时回撤较大
- 多周期动量权重 `(0.5, 0.3, 0.2)` 是经验值，未做参数寻优
- 防守候选只有 1 只红利 ETF，极端市场（2022 全市场下跌）时仍承担 beta 风险
- 不考虑 ETF 间相关性，可能选出强相关标的（如同时持有半导体 + 芯片）

---

## 8. 与主 design_agent 的差异
- 不需要"未来函数"检查（动量信号天然滞后 1 日）
- 不需要"幸存者偏差"检查（ETF 池子人工策展，不存在退市）
- 需要在「已知局限」中说明 ETF 池子是历史选股，未来若 ETF 退市需手动剔除
