# strategycli

> GitHub 仓库名：**[strategyCli](https://github.com/Mrrabbitan/strategyCli)**（维护者：Mrrabbitan）。本地克隆目录可为 `strategycli` 或 `strategyCli`（与仓库名一致），二者指同一套代码。

> AI 驱动的量化策略自动生成工具。输入策略需求 → Agent 设计 → 代码生成 → 回测验证 → 自主优化。

[![Skill](https://img.shields.io/badge/Skill-strategycli-blue)](https://github.com/Mrrabbitan/strategyCli)
[![Market](https://img.shields.io/badge/Market-A%E8%82%A1%20%7C%20%E6%B8%AF%E8%82%A1%20%7C%20%E7%BE%8E%E8%82%A1-green)]()
[![SubSkill](https://img.shields.io/badge/SubSkill-cn--etf-orange)](skills/cn-etf/SKILL.md)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

> ⚠️ **免责声明**：本工具生成的策略仅供学习和研究用途，不构成任何投资建议。量化交易有风险，过往回测表现不代表未来收益。

## 它能做什么？

| 入口 | 你说 | 它做 |
|------|------|------|
| **明确需求** | "帮我设计一个双均线交叉策略" | 直接分析 → 设计文档 → 代码 + 回测 |
| **模糊需求** | "我想做A股量化，但不确定用什么方法" | 诊断推荐 → 选方向 → 生成策略 |
| **博主策略** | "按某大V的投资逻辑做个策略" | 互联网研究 → 提炼逻辑 → 量化策略 |
| **优化迭代** | "优化这个策略的回测结果" | 诊断弱点 → 5轮自主优化 → 输出报告 |
| **A 股 ETF** ⭐ | "帮我做个行业 ETF 轮动策略" | 转交 [`cn-etf` 子 skill](skills/cn-etf/SKILL.md)：ETF 专属知识库 + 三模板 |

## 核心设计

### 文档驱动

**STRATEGY_DESIGN.md 是「系统施工图纸」** — 所有策略逻辑先落在设计文档上，代码只是文档的严格翻译产物。

```
用户需求 → STRATEGY_DESIGN.md（精确规格）→ strategy.py（严格翻译）→ 回测验证
```

这意味着：AI 不会「自由发挥」，每行代码都有文档对应；修改策略时改文档，代码跟随更新。

### Agent 化工作流

strategycli 采用**多 Agent 串联**架构，用户只需在 3 个关键审批点参与决策：

```
用户输入
    ↓
┌─────────────────┐  Phase 1: 策略设计 Agent
│  设计 Agent      │  → 产出 STRATEGY_DESIGN.md
│  (design_agent)  │
└────────┬────────┘
         ↓ ⏸ 审批点 1：确认设计文档
┌─────────────────┐  Phase 2: 代码生成 Agent
│  代码 Agent      │  → 产出 strategy.py + 回测报告
│  (codegen_agent) │
└────────┬────────┘
         ↓ ⏸ 审批点 2：确认回测结果
┌─────────────────┐  Phase 3: 优化 Agent（自主/交互式）
│  优化 Agent      │  → 产出优化报告
│  (optimization)  │
└────────┬────────┘
         ↓ ⏸ 审批点 3：最终决策（接受 / 重做 / 回 Phase 1）
```

- **文件驱动状态转移**：STRATEGY_DESIGN.md → strategy.py → backtest_result.json → changelog.md
- **棘轮决策**：每次优化用 `score_strategy()` 评分，有效保留、无效回滚

### 子 skill 扩展机制

主 SKILL 是通用调度台，特定市场 / 品种的专属逻辑由子 skill 承载。当前已内置：

| 子 skill | 路径 | 何时触发 |
|---------|------|---------|
| **cn-etf**（中国场内 ETF） | [`skills/cn-etf/`](skills/cn-etf/) | 用户提到「ETF 轮动 / ETF 网格 / ETF 定投 / 行业 ETF」 |

子 skill 复用主 repo 的 `scripts/`（评分函数、回测器、质量检查），仅替换 prompts 与知识库，避免重复造轮子。

## 适用市场

| 市场 | 数据源 | 交易规则 | 推荐入口 |
|------|--------|---------|---------|
| **A股个股** | [FTShare](https://github.com/rivar0107/all-in-one)（免费） | T+1，涨跌停 ±10%/±20% | 主 SKILL |
| **A股 ETF** ⭐ | FTShare / akshare（免费） | T+1，**印花税 0**，涨跌停同上 | [cn-etf 子 skill](skills/cn-etf/) |
| **港股** | FutuAPI（需 Futu OpenD） | T+0，无涨跌停 | 主 SKILL |
| **美股** | FutuAPI（需 Futu OpenD） | T+0，PDT 规则 | 主 SKILL |

> 期货、期权暂不支持，后续版本逐步加入。

## 快速开始

### 安装

```bash
npx skills add Mrrabbitan/strategyCli --yes
```

安装后在 Claude Code / Gemini CLI / Copilot CLI 中直接使用，无需额外配置。

### 环境准备（可选）

```bash
pip install numpy pandas pyyaml
```

- **A股数据**：安装 [ftshare-all-in-one](https://github.com/rivar0107/all-in-one) Skill（免费）
- **A股 ETF 数据**：上述任一 + `pip install akshare`（cn-etf 子 skill 也支持纯 akshare）
- **港美股数据**：安装 FutuAPI Skill（需 [Futu OpenD](https://www.futunn.com/download/openAPI)）

### 使用示例

```
"帮我设计一个双均线交叉策略"
"我想做一个港股量化策略，但不清楚用什么方法"
"帮我根据某大V在微博上的投资观点做个量化策略"
"优化这个策略的回测结果，降低最大回撤"

# 触发 cn-etf 子 skill：
"帮我做个行业 ETF 轮动策略"
"沪深300 ETF 网格交易"
"半导体 + 医药 ETF 月度轮动"
"红利 ETF 定投加减仓"
```

## 项目结构

```
strategycli/
├── SKILL.md                          # 调度台：入口分流 + Agent 编排 + 审批点控制
├── prompts/                          # 主 SKILL 的通用 Agent 指令
│   ├── design_agent.md               # Phase 1：策略设计 Agent
│   ├── codegen_agent.md              # Phase 2：代码生成 Agent
│   └── optimization_agent.md         # Phase 3：自主优化 Agent
├── scripts/                          # 主 SKILL + 所有子 skill 共用
│   ├── env_setup.py                  # 环境检查与依赖安装
│   ├── quality_check.py              # 策略设计文档质量检查
│   └── run_backtest.py               # 回测执行与评分
├── examples/
│   └── dynamic-grid-multi-market/    # 示例：动态网格多标的（跨市场）
│       ├── STRATEGY_DESIGN.md
│       ├── config.yaml
│       ├── strategy.py
│       ├── requirements.txt
│       └── data/
│           └── fetch_data.py
├── skills/
│   └── cn-etf/                       # 子 skill：中国场内 ETF 专用
│       ├── SKILL.md                  # ETF 子 skill 入口（触发词 + 三模板分流）
│       ├── README.md                 # ETF 子 skill 说明
│       ├── prompts/
│       │   ├── etf_design_agent.md   # ETF 专用设计 Agent（强制读 knowledge）
│       │   ├── etf_codegen_agent.md  # ETF 专用代码 Agent（印花税 0、T+1、lot_size 100）
│       │   └── templates/
│       │       ├── grid_etf.md       # 模板1：宽基/行业 ETF 网格
│       │       ├── rotation_etf.md   # 模板2：行业/宽基轮动（默认）
│       │       └── dca_grid_etf.md   # 模板3：定投+均线偏离网格
│       ├── knowledge/
│       │   ├── cn_etf_universe.md    # 推荐池（宽基/行业/主题/红利/商品/QDII）
│       │   ├── cn_etf_trading_rules.md  # T+1/涨跌停/印花税豁免/流动性门槛
│       │   └── benchmarks.md         # 基准映射（沪深300/中证800/中证红利等）
│       └── examples/
│           └── industry-rotation-etf/  # 完整示例：8 只 ETF + 月度动量轮动
│               ├── STRATEGY_DESIGN.md
│               ├── config.yaml
│               ├── strategy.py       # 实现 run_backtest()，复用主 scripts 评分
│               ├── requirements.txt
│               ├── README.md
│               └── data/
│                   └── fetch_data.py
```

## 示例策略

| 示例 | 路径 | 类型 | 标的池 | 特点 |
|------|------|------|--------|------|
| 动态网格多标的 | [examples/dynamic-grid-multi-market/](examples/dynamic-grid-multi-market/) | 网格 | 5 只跨市场（A 股/港股/美股） | 演示多市场协同 |
| 行业 ETF 轮动 ⭐ | [skills/cn-etf/examples/industry-rotation-etf/](skills/cn-etf/examples/industry-rotation-etf/) | 多因子轮动 | 8 只 A 股 ETF（宽基/行业/红利） | 演示 cn-etf 子 skill 的产物 |

「动态网格多标的」2024-2025 mock 回测：年化 11.99%，最大回撤 30.47%，夏普 0.49，胜率 75.2%。
「行业 ETF 轮动」需先 `pip install akshare` 拉真实数据后回测；离线 mock 数据下分数仅供脚手架自检。

## 评估与设计原则

**评分函数**：5 个维度共 100 分 + 简洁性惩罚（条件数 > 10 时每个扣 1.5 分）。

| 维度 | 满分 | 满分条件 | 设计原则 |
|------|------|---------|---------|
| 年化收益率 | 25 | > 基准×2（沪深300 8% / 恒生 5% / 标普 10%）| 简洁性优先：分数提升必须大于复杂度增加 |
| 最大回撤 | 20 | < 10%（回撤≥30%得0分）| 文档是核心：所有逻辑先写入 DESIGN.md |
| 夏普比率 | 25 | > 2.0 | 人在回路：设计文档和回测结果人类确认 |
| 胜率 | 15 | > 60% | 量化评估：用 score_strategy() 决定 keep/revert |
| 盈亏比 | 15 | > 2.5 | 不推实盘：定位是策略创建和验证工具 |

同时检测：过拟合、幸存者偏差、未来函数、流动性、前后半段稳定性。

## 技术栈

- **语言**：Python 3.9+
- **数据处理**：NumPy, Pandas
- **数据源**：[FTShare](https://github.com/rivar0107/all-in-one)（A股 / A股 ETF）、akshare（A股 ETF 备选）、FutuAPI（港美股）
- **AI Agent 兼容**：Claude Code, Gemini CLI, Copilot CLI, Codex, Cline 等

## 相关项目

- [all-in-one](https://github.com/rivar0107/all-in-one) — 免费的 A 股/港股行情数据 Skill
- [darwin-skill](https://github.com/alchaincyf/darwin-skill) — AI Skill 持续优化框架

## License

MIT License
