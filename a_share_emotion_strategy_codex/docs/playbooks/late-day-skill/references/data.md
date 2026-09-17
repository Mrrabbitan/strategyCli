# 数据契约与运行

scripts/run.py --input evidence.json --phase auto 输出JSON，不联网、不下单、不修改持仓。阶段 auto/preview/live/review/next-open，--as-of 可指定截止。历史文件使用review。scripts/validate_input.py evidence.json 退出码2表示证据不完整，不等于策略不合格。

工作台：python3 late_day_research.py --collect [--codes 600001,000001] 获取公开初筛与补证原文；--input导入经过核验的完整契约并发布。公共采集标志不能自动升级为人工核验。无参数仅登记未执行状态，不伪造最新研究。

## 输入 schema_version=1

顶层 as_of 带时区，calendar、coverage、market、stocks，可选positions。价格/金额用人民币元，量用股；支持十进制字符串。

- calendar: verified,source,valid_from,valid_until,days。有序唯一，覆盖此前20交易日至下一交易日；不靠工作日猜节假日。
- coverage: verified,description,universe_count,scanned_count。scanned_count等于详情stocks数，元数据/初筛另记prefilter_count，不冒充全市场详情。
- market: source,time,index_pct,rising,falling,flat,total,universe_verified。涨跌平=已核验有效主板total，不用全A替代。
- 股票: code,name,security,shares,quote,history_meta,history,minute_meta,opening_auction,minutes,industry。
- security: source,date,verified,mainboard,st,suspended,delisting,normal_limit。后五项布尔值，不从正常名称推断所有状态都正常。
- shares: source,valid_on,verified,float_a,total，核验当日有效股本。
- quote: source,time,price,reference_close,high,upper_limit,volume_shares,amount_cny,scope="includes_opening_auction"。可选conflict、ratio_verified、volume_ratio、ratio_method="cumulative_per_minute_over_previous5_full_day_per_minute"。
- history_meta: source,verified,adjustment="none",limits_verified,volume_comparable,scope="includes_opening_auction"。history严格为此前连续20完整交易日，行字段date,high,upper_limit,volume_shares；股本变化影响可比性须降级。
- minute_meta: source,verified,label="interval_end",scope="continuous_only",volume_unit="share",amount_unit="CNY"。第一分钟9:30—9:31标为9:31，上午末为11:30，午后首为13:01。先核验供应商标签，再转换；不能把9:30数据猜成竞价或随意平移时间。
- opening_auction: source,time(当日9:25),verified,final,price,volume_shares,amount_cny。只用最终真实成交，且分钟不能重复计入。
- minutes: 完整连续竞价分钟，行字段end,close,low,high,volume_shares,amount_cny。零量显式给出沿用价格，缺失/重复/午休/未来分钟均不可删除后计算。金额/量需与该分钟价格范围一致。
- industry: source,time,classification="eastmoney_industry",mapping_verified,mapping_date,code,pct。固定当日归属，不能换概念。

每个源保留原文、取得时间、来源时间及指纹于私有目录。无时间资金/成交字段不可套用新价格时间；90秒时效和跨报价60秒分别核验。历史回放不授予实时资格。

## 持仓冻结

positions仅接受用户确认的id,code,confirmed=true,bought_at,cost,entry_source,entry_minutes。entry_minutes为买入前30完整分钟，各有end和low。私有冻结库保存日期、成本、风险线和证据指纹；页面不发布成本或个人数量。同id再次导入不能改变代码/日期/成本/风险参考/证据，冲突报告，不能默默重置。

## 输出

报告记录规则/输入指纹、as_of/generated_at/valid_until、phase/state、coverage、rows、qualified_count/display_codes、exits、missing。checks使用true/false/null；research_passed仅表示当时规则判断，eligible始终false，不授予别的策略资格。阶段与缺数据独立显示，失败与最近成功分开留档，不以旧名单补位。默认最多5项，完整记录留本地。

[AKShare文档](https://akshare.akfamily.xyz/data/stock/stock.html)中的一分钟接口仅覆盖近期资料。先核验实际字段、日期、单位与增量/累计，不因接口名字含“实时”就通过时效；未经核验的数据仅作为补证原文。
