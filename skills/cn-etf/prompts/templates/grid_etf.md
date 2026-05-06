# 模板 1：ETF 网格策略

> 适用场景：单只或少数（≤ 3 只）有显著震荡特征的 ETF，在均线附近开网格捕捉波动 alpha。
> 不适用场景：单边大行情（持续上涨被动减仓 / 持续下跌持续加仓导致深套）。

---

## 1. 默认标的池建议

- **保守方案**：`510300.SH` 沪深300ETF（波动率低，网格密集）
- **均衡方案**：`510300.SH` + `510500.SH` 中证500ETF
- **激进方案**：`588000.SH` 科创50ETF（±20%涨跌停 + 高波动）

> 单只 ETF 仓位上限建议 35%（保留 65% 现金作为加仓燃料）。

---

## 2. 指标定义

### 指标 1: ATR（平均真实波幅）
- 公式：`TR = max(H-L, |H-PrevC|, |L-PrevC|)`，`ATR(N) = SMA(TR, N)`
- 默认参数：`atr_period = 14`，合理范围 `[7, 28]`
- 用途：决定网格间距，让网格随波动率自适应

### 指标 2: 网格中线
- 公式：`Base = SMA(CLOSE, ma_period)`
- 默认参数：`ma_period = 60`，合理范围 `[20, 120]`
- 用途：网格中心，每 N 天 rebalance 一次

### 指标 3: 网格间距
- 公式：`GridStep = ATR(14) × grid_multiplier`
- 默认参数：`grid_multiplier = 1.5`，合理范围 `[0.5, 3.0]`

### 指标 4: 网格线序列
- 公式：`GridLine(i) = Base + i × GridStep`，`i ∈ [-grid_levels, grid_levels]`
- 默认参数：`grid_levels = 5`（单侧 5 条 = 总 11 条）

### 指标 5: Rebalance 周期
- 默认 `rebalance_days = 5`（每周）

---

## 3. 信号决策树

### 买入（所有条件同时满足）
1. `Close[t] < GridLine(i)` 且 `Close[t-1] >= GridLine(i)`（向下穿越某条网格线）
2. 该网格线上当前持仓 = 0（不重复建仓）
3. 当前总持仓 < `total_position_pct`（默认 95%）
4. 不在涨停板（防止追高）
5. 单笔买入金额 = `total_value × single_grid_pct`（默认 5%）

### 卖出（满足任一即触发，止损优先）
1. **止损**：持仓亏损 > `stop_loss_pct`（默认 8%）
2. **网格盈利**：`Close[t] >= 买入网格线 + GridStep`（向上穿越上一条线）
3. **T+1 检查**：当日买入的不可当日卖出

---

## 4. 仓位管理
- 单笔金额 = `total_value × single_grid_pct`（默认 5%）
- 单只 ETF 仓位上限 = 35%
- 总仓位上限 = 95%
- 买入数量 = `floor(金额 / 价格 / 100) × 100`

---

## 5. 风控规则

| 规则 | 默认阈值 | 触发动作 |
|------|---------|---------|
| 单笔最大仓位 | 5%（每格） | 限制买入数量 |
| 单笔止损 | 8% | 触发卖出 |
| 单只 ETF 仓位上限 | 35% | 不再加仓 |
| 总仓位上限 | 95% | 不再开新仓 |
| 最大回撤 | 18% | 全部清仓暂停 5 日 |
| 极端跌幅 | 单日 < -7% | 暂停该 ETF 3 日 |

---

## 6. 推荐回测参数

```yaml
initial_cash: 1000000
start_date: "2020-01-01"
end_date: "2025-12-31"
indicators:
  atr_period: 14
  ma_period: 60
  grid_multiplier: 1.5
  grid_levels: 5
  rebalance_days: 5
risk:
  single_grid_pct: 5
  stop_loss_pct: 8
  max_position_pct: 35
  total_position_pct: 95
  max_drawdown_pct: 18
  extreme_drop_pct: 7
```

---

## 7. 已知局限
- 单边趋势市中持续被动加仓 / 减仓，深套或踏空风险
- ETF 流动性较好，但极端行情仍可能出现盘口缺口导致网格未触发
- 跨境 ETF 在二级市场存在折溢价波动，IOPV 与价格脱锚时网格信号失真
- ATR 在低波动期间偏窄，网格密集触发更多无效交易

---

## 8. 参考示例
本模板可与主 repo 已有的 [examples/dynamic-grid-multi-market/](../../../../examples/dynamic-grid-multi-market/) 类比，但本模板：
- 池子改为 1-3 只 A 股 ETF
- 印花税强制 0
- T+1 强制
- 取消港美股标的与汇率折算
