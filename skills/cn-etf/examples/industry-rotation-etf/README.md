# 行业 ETF 轮动策略

> 在 8 只 A 股 ETF（1 宽基对照 + 6 行业 + 1 防守候选）中按多周期动量打分，每月底等权持有 Top-3，无入选时切换至红利 ETF 防守。

## 策略概述
- **策略类型**：多因子 / 动量轮动
- **适用市场**：A 股场内 ETF
- **适用周期**：日线 / 月度调仓
- **模板**：`skills/cn-etf/prompts/templates/rotation_etf.md`

## 核心逻辑
1. 月底用 t-1 日收盘价计算每只 ETF 的多周期动量分（20/60/120 日加权）。
2. 应用 MA60 趋势过滤，剔除站不上 60 日均线的标的。
3. 按动量分降序取 Top-3 等权持仓；若入选数量为 0，则全仓切换至 510880 红利 ETF 防守。
4. 调仓时受 A 股 T+1、涨跌停、单 ETF 仓位上限 35%、总仓位上限 95% 的硬约束。
5. 组合最大回撤触及 18% 时强制清仓，暂停 5 个交易日。

## 标的池

| 角色 | 代码 | 名称 |
|------|------|------|
| 宽基对照 | 510300.SH | 沪深300ETF |
| 行业 | 512170.SH | 医疗ETF |
| 行业 | 512480.SH | 半导体ETF |
| 行业 | 512760.SH | 芯片ETF |
| 行业 | 512800.SH | 银行ETF |
| 行业 | 512690.SH | 酒ETF |
| 行业 | 515030.SH | 新能源车ETF |
| 防守 | 510880.SH | 红利ETF |

## 关键参数

| 参数 | 默认值 | 含义 | 调参建议 |
|------|--------|------|---------|
| `lookback_short` | 20 | 短期动量窗口（约 1 月） | 10 ~ 40 |
| `lookback_mid` | 60 | 中期动量窗口（约 3 月） | 40 ~ 90 |
| `lookback_long` | 120 | 长期动量窗口（约 6 月） | 90 ~ 180 |
| `w_short / w_mid / w_long` | 0.5 / 0.3 / 0.2 | 动量权重 | 必须和 = 1 |
| `ma_period` | 60 | 趋势过滤均线周期 | 20 ~ 120 |
| `hold_top_n` | 3 | 等权持仓数量 | 2 ~ 5 |
| `rebalance_freq` | monthly | 调仓频率 | monthly / weekly |
| `rebalance_threshold` | 5（%） | 仓位差异容忍 | 3 ~ 10 |
| `max_position_pct` | 35（%） | 单 ETF 仓位上限 | 25 ~ 50 |

## 使用方式

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 拉取真实数据（需联网；首次运行）
python3 data/fetch_data.py --config config.yaml --output data/

# 3. 运行回测（在 strategycli 仓库根目录下）
python3 scripts/run_backtest.py skills/cn-etf/examples/industry-rotation-etf

# 4. 可选：训练/测试集分割
python3 scripts/run_backtest.py skills/cn-etf/examples/industry-rotation-etf --split 0.7

# 5. 可选：参数敏感度分析
python3 scripts/run_backtest.py skills/cn-etf/examples/industry-rotation-etf --sensitivity

# 6. 可选：净值曲线图
python3 scripts/run_backtest.py skills/cn-etf/examples/industry-rotation-etf --plot
```

> 离线/无 ftshare/akshare 时，脚本会自动回落到 mock 数据，仅用于回测脚手架自检（**结果不具任何参考价值**），请务必在拉真实数据后再评估策略表现。

## 风险提示
- 月频调仓在快速行情切换中反应较慢
- 防守候选只有 1 只红利 ETF，2022 年系统性下跌时仍承担 beta 风险
- 多周期动量权重未做参数寻优，存在过拟合风险
- ETF 池子是 2025Q4 时的人工策展，未来如有清盘需手动维护
- 仅供学习和研究用途，不构成任何投资建议
