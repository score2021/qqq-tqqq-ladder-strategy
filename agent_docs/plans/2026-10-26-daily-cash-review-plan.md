# 每日一次检查与闲置现金预算实施计划

依据：[契约](../contracts/2026-10-26-daily-cash-review-contract.md)。用户已明确授权实施和云端配置，无需重新确认。

1. RED：`tests/test_daily_cash_review.py`覆盖调度、预算、结算、优先级、收盘核查、通知；现有旧调度断言按新契约更新。
2. GREEN：修改`cloud_runner/runtime.py`调度和原始成交请求证据；`portfolio.py`保留已结算字段；新增`review.py`纯预算及核查；`notification.py`展示预算、漂移限制；纯价格策略核心不改。
3. 同步`deploy/qqq-tqqq-strategy.timer`、`docs/cloud-deployment.md`及`docs/trading-system.md`。
4. 运行unittest、coverage、compileall及diff检查；保留真实RED/GREEN结果。Git只检查，不提交或推送，保护原有改动。
5. devsin2暂停本项目定时器，锁内校验源文件清单，备份源文件、配置和单元，原子替换并修改现金政策配置，测试失败回滚；恢复定时器。
6. 独立手动任务验收IBKR原始读取→计算→企业微信；核查未提交订单、消息长度、预算和单一每日定时器，更新私密验收材料与源文件清单。
