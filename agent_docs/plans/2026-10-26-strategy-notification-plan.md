# 策略通知改版实施计划

依据`agent_docs/contracts/2026-10-26-strategy-notification-contract.md`，用户已授权全部实施与云端更新。

## 范围与复用

- 新增`cloud_runner/notification.py`，集中处理中文展示、参考线距离、配置百分比和阶段条件，复用Decimal与现有报告字段。
- `cloud_runner/runtime.py`的`message_for`保持调用接口并委托渲染；策略分析与持久状态逻辑保持原样。
- `tests/test_strategy_notification.py`先测试当前真实指标案例、四阶段、无决策、非法占比和字节上限，确认RED再实现。
- `docs/cloud-deployment.md`同步通知说明；契约、计划与正式测试作为源码保留；私密数据、WebHook、日志不进Git。

## 实施与验收

1. unittest确认旧通知无法满足新展示行为，再实现最小渲染器。
2. 回归既有70项；运行策略与云端包整体coverage，阈值80%；新模块单独报告覆盖率；compileall、git diff --check。
3. 暂停本项目timer、确认服务空闲，在锁内备份旧通知文件并更新，先云端测试再恢复timer；不修改配置和账本。
4. 用新唯一任务名从systemd真实取数、计算和推送，核对接口成功、消息字节数、无股数和技术字段、源码哈希一致。
5. 保存验收证据；不主动git提交推送，不重新运行已完成的旧任务。
