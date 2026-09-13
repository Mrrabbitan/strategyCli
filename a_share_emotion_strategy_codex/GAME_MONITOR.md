# 游戏板块盘中提醒

本线程监控已于 2026-09-12 扩展为全行业及概念板块，当前规则见 [全板块监控说明](SECTOR_MONITOR.md)。本页保留原游戏规则及手机订阅、验收说明；实际范围以私有配置 `scope` 为准。

`game_monitor.py` 是独立的只读行情监控服务，不执行交易。默认每30秒采集一次，在交易日09:30–11:30和13:00–15:00（Asia/Shanghai）计算游戏行业的双向异动。09:15起更新成分股；晚启动时先更新当日名单。沿用 `market_calendar.json`，缺失年份时跳过，需在跨年前更新日历。

## 已实现规则

- 板块5分钟涨跌达到±1%，至少60%有效成分股同向。
- 个股5分钟涨跌达到±3%，该窗口成交额达到此前三个5分钟窗口均值2倍。
- 至少3只成分股5分钟同向涨跌达到2%。

价格涨跌以行情时间戳的窗口首尾价格计算。窗口锚点取目标时刻之前最多40秒内的最近样本，窗口内采样缺口不得超过90秒。放量需要20分钟连续数据；开盘、午后和数据断线后不会用不完整窗口触发。板块联动需要90%成分股具备有效5分钟窗口，未达到时个股规则仍可独立运行。过期超过90秒、未来时间戳、非正价格、非有限数值和累计成交额回退不会用于触发。

同对象、同规则、同方向的持续信号只报一次。条件解除后且距离上次触发满10分钟才能再次提醒；冷却期内的新信号不会在持续状态下延迟补报。反方向独立处理。每轮事件合并推送，正文按长度截取；完整规则命中写入日志。重启保留历史和去重状态，跨午休/交易日清空窗口。

## 安装与手机连接

```sh
python3 a_share_emotion_strategy_codex/game_monitor.py install
python3 a_share_emotion_strategy_codex/game_monitor.py subscription
```

安装会创建 `~/Library/LaunchAgents/local.autostrategy.game-monitor.plist` 并加载后台服务，默认关闭正式监控。私有配置、订阅主题、队列和日志放在 `~/Library/Application Support/AutoStrategy/game-monitor/`，目录权限700，配置权限600，不进入仓库或私人报告站点。再次安装保留配置。LaunchAgent在当前用户登录后运行，崩溃自动重启；启用且处于交易时段时使用caffeinate阻止自动空闲休眠，无法阻止关机、主动睡眠或合盖睡眠。

安卓安装官方 ntfy：https://docs.ntfy.sh/subscribe/phone/ 。使用subscription输出的服务器和主题订阅。启用即时接收/前台订阅服务、通知权限和高优先级通道振动，允许后台运行、取消电池优化限制。F-Droid版本默认即时接收，不依赖Firebase。随机主题是私密订阅地址，公共ntfy服务器并非访问受控的私人服务器；仅发送公开行情，不发送持仓或账户信息。需要访问控制时可配置有认证的ntfy服务器及token。

用户完成订阅后发送测试：

```sh
python3 a_share_emotion_strategy_codex/game_monitor.py test-push
```

分别在Wi-Fi、移动网络和锁屏至少15分钟后测试，用户确认每次收到且振动，记录发送和接收时间。服务回执只表示服务器接收，不能证明手机振动。只有完成实机验收后才能运行：

```sh
python3 a_share_emotion_strategy_codex/game_monitor.py enable --phone-verified
```

正常数据和网络条件下，以行情可用至手机收到60秒内为验收目标，不承诺公网行情延迟或后台被系统终止时仍能满足。周末probe返回上次收盘行情属于正常，不代表盘中实时性已经验收。

## 运维与验证

```sh
python3 a_share_emotion_strategy_codex/game_monitor.py status
python3 a_share_emotion_strategy_codex/game_monitor.py probe
python3 a_share_emotion_strategy_codex/game_monitor.py disable
python3 -m unittest discover -s a_share_emotion_strategy_codex/tests -p test_game_monitor.py -v
```

配置可修改阈值、30秒间隔、覆盖率、ntfy地址和token；不要降低数据新鲜度来掩盖延迟。代码按每轮读取配置。日志使用轮换文件 `monitor.log`（每份2MB，保留3份备份）；状态在status.json，行情与去重在state.json，未发送通知在outbox.json。

东方财富板块入口按可用性切换，可能使用push2delay入口，但必须通过相同90秒时间戳门槛。个股采用腾讯行情。主入口失败时备用入口未必实时；过期时暂停相关规则，3分钟异常提醒一次，恢复提醒一次。推送单独线程发送，失败5秒、15秒后各重试一次，等待重试不阻塞采集；2分钟以上待发消息丢弃并记日志。网络超时导致服务端已接收但回执丢失时，重试可能重复送达。

尚需外部验收的项目：安卓实机接收/振动、真实交易时段数据延迟与60秒端到端目标。没有手机确认前，status明确显示未启用。

## 未完成手机验收时的本机观察

`python3 a_share_emotion_strategy_codex/game_monitor.py observe` 开启交易时段行情采集与本机异动记录，无需手机验收。`status` 会明确区分 `enabled`（本机采集）和 `phone_alerts_enabled`（手机通知）。观察模式将最近200条事件写入私有目录events.json，不向ntfy发送或积压历史消息。开启手机推送时仅重新判定当前新鲜行情；`disable`同时关闭观察和手机推送。该模式不自动验证研究计划中的完整买入条件。
