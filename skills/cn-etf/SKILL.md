---
name: cn-etf
description: |
  中国场内 ETF 基金量化策略子 skill，专注 A 股 ETF（宽基/行业/主题/红利/商品/QDII），内嵌 ETF 池子、交易规则（T+1、涨跌停、印花税豁免、流动性门槛）和基准对照。
  提供三个开箱即用模板：网格（grid_etf）、行业/宽基轮动（rotation_etf）、定投+网格（dca_grid_etf）。
  触发词：「ETF 策略」「ETF 量化」「ETF 轮动」「ETF 定投」「ETF 网格」「行业 ETF」「宽基 ETF」「沪深300 ETF」「红利 ETF」「指数基金量化」「大类资产轮动」。
  场景触发：「帮我做一个行业 ETF 轮动策略」「沪深300 ETF 网格交易」「ETF 定投加减仓」「半导体医药轮动」「红利 ETF 防守策略」。
  本 skill 复用主 autostrategy 的 Phase 1/2/3 流程与回测器（scripts/run_backtest.py），仅替换 prompts 为 ETF 专用版。
  适用市场：仅 A 股场内 ETF（含 QDII，但默认按 T+1 处理；不做高频套利、不做场外公募基金）。
---

# cn-etf · 中国 ETF 策略生成子 skill

> ⚠️ 本 skill 生成的策略仅供学习和研究用途，不构成任何投资建议。
> 量化交易有风险，过往回测表现不代表未来收益。

---

## 1. 何时触发本 skill

主 [autostrategy SKILL.md](../../SKILL.md) 在入口分流时，遇到以下场景应优先调用本子 skill：

| 用户输入特征 | 路径 |
|-------------|------|
| 明确说「ETF 策略 / ETF 轮动 / ETF 定投 / ETF 网格」 | 直接进入本 skill |
| 标的明显是 ETF（如 510300 / 588000 / 159915 / 512760 等） | 直接进入本 skill |
| 模糊说「指数基金量化」「大类资产轮动」「行业轮动」 | 默认进入本 skill 的 `rotation_etf` 模板 |
| 用户要做 A 股个股策略（如 600519、000001）或港美股 | 走主 autostrategy 流程，不进入本 skill |

---

## 2. 三模板入口分流

| 用户表述 | 模板 | 文件 |
|---------|------|------|
| 「ETF 网格 / 震荡 ETF / 网格交易」 | 网格 | [prompts/templates/grid_etf.md](prompts/templates/grid_etf.md) |
| 「ETF 轮动 / 行业轮动 / 强弱轮动 / 动量轮动 / 大类资产轮动」 | 轮动 | [prompts/templates/rotation_etf.md](prompts/templates/rotation_etf.md) |
| 「ETF 定投 / 长期持有 + 加减仓 / 智能定投」 | 定投+网格 | [prompts/templates/dca_grid_etf.md](prompts/templates/dca_grid_etf.md) |
| 仅说「ETF 量化」未指定 | **默认轮动**，并在报告中说明可重选 | rotation_etf |

> 模板只是设计阶段的"决策树脚手架"，最终生成的 `STRATEGY_DESIGN.md` 由 `etf_design_agent.md` 根据模板填充。

---

## 3. 与主 autostrategy 的契约

本 skill **不重复实现**回测器、评分函数、质量检查脚本，全部复用主 repo：

| 复用项 | 主 repo 路径 |
|--------|------------|
| 评分函数 `score_strategy()` | [scripts/run_backtest.py](../../scripts/run_backtest.py) |
| 回测引擎（`run_backtest(config) -> dict` 接口） | [scripts/run_backtest.py](../../scripts/run_backtest.py) |
| 设计文档质量检查 | [scripts/quality_check.py](../../scripts/quality_check.py) |
| 环境检测 | [scripts/env_setup.py](../../scripts/env_setup.py) |
| Phase 3 优化 Agent | [prompts/optimization_agent.md](../../prompts/optimization_agent.md) |

调用约定：调度方在启动 Agent 时，`scripts_dir` **必须指向主 repo 的 `scripts/`** 而非本 skill 子目录。

---

## 4. 执行流程（与主 SKILL 相同节奏）

```
用户输入（含 ETF 关键词）
    ↓
Phase 1: 启动 etf_design_agent → 产出 STRATEGY_DESIGN.md
    ↓
⏸ 审批点 1: 用户确认 STRATEGY_DESIGN.md
    ↓
Phase 2: 启动 etf_codegen_agent → 产出 strategy.py + config.yaml + 回测
    ↓
⏸ 审批点 2: 用户确认回测结果
    ↓
Phase 3: 复用主 repo optimization_agent → 5 轮自主优化
    ↓
⏸ 审批点 3: 用户最终决策
```

### Phase 1: 启动 etf_design_agent

```
Agent({
  prompt: 读取本 skill 目录下 prompts/etf_design_agent.md 的完整内容，
          + knowledge/cn_etf_universe.md
          + knowledge/cn_etf_trading_rules.md
          + knowledge/benchmarks.md
          在末尾追加：
          """
          ## 实际参数
          - user_input: {用户原始输入}
          - template: {grid_etf | rotation_etf | dca_grid_etf}
          - market: A股
          - scripts_dir: {主 repo 的 scripts 绝对路径}
          - workspace: {可留空，由 agent 自行决定}
          """,
  model: sonnet
})
```

### Phase 2: 启动 etf_codegen_agent

```
Agent({
  prompt: 读取本 skill 目录下 prompts/etf_codegen_agent.md 的完整内容，
          + knowledge/cn_etf_trading_rules.md（再次强调规则）
          在末尾追加：
          """
          ## 实际参数
          - workspace: {Phase 1 产出的策略目录绝对路径}
          - scripts_dir: {主 repo 的 scripts 绝对路径}
          """,
  model: sonnet
})
```

### Phase 3: 复用主 repo optimization_agent

直接调用主 repo 的 [prompts/optimization_agent.md](../../prompts/optimization_agent.md)，无需替换。
ETF 策略的优化候选方案库与个股策略一致（收益率/回撤/夏普/胜率/盈亏比 5 维度）。

---

## 5. 闸门与审批点（与主 SKILL 一致）

**Phase 1 → Phase 2 闸门**：
1. 策略目录已创建，路径已明确
2. `STRATEGY_DESIGN.md` 已写入磁盘（ls/cat 验证）
3. ETF 池子全部满足流动性门槛（日均成交 ≥ 5000 万、规模 ≥ 5 亿），不满足的已在「已知局限」标注
4. `config.yaml` 字段计划：每只 ETF 含 `lot_size: 100, stamp_tax: 0, t_plus_1: true, price_limit_pct: <值>`
5. 用户确认设计

**Phase 2 → Phase 3 闸门**：
1. `strategy.py` 已生成且暴露 `run_backtest(config: dict) -> dict`
2. 回测脚本退出码 0
3. `backtest_result.json` 中所有必填字段（annual_return / max_drawdown / sharpe / win_rate / profit_loss_ratio / total_trades）齐全
4. 用户确认进入优化或接受当前结果

---

## 6. 已包含的完整示例

| 示例 | 模板 | 路径 |
|------|------|------|
| 行业 ETF 轮动（8 只行业 + 1 只防守） | rotation_etf | [examples/industry-rotation-etf/](examples/industry-rotation-etf/) |

后续可继续补充 `wide-base-grid-etf/`（宽基网格）和 `csi300-dca-etf/`（沪深300 智能定投）示例。

---

## 7. 禁止事项（ETF 专属）

1. ❌ `config.yaml` 中 `stamp_tax` 设为非 0（ETF 卖出免印花税）
2. ❌ 选用日均成交 < 5000 万的 ETF 而不在「已知局限」标注
3. ❌ ETF 标的没有 `t_plus_1: true` 字段
4. ❌ `lot_size` 不是 100
5. ❌ 跨境 QDII ETF 按 T+0 处理（除非用户明确要做套利策略）
6. ❌ 行业轮动池子里没有任何宽基 ETF（缺少基准 beta 锚点）
7. ❌ 推荐场外公募基金（OTC）— 本 skill 仅限场内 ETF
8. ❌ 推荐实盘交易 / 提供投资建议
9. ❌ 引导用户购买付费数据服务
