# 全部非IREN资金与分区通知实施计划

用户已授权完整实施。关联`agent_docs/contracts/2026-10-26-account-scope-notification-contract.md`。

## 路径

- 新增`cloud_runner/portfolio.py`：基于原始快照和明确配置计算四资产/其他资产估值及权重，过滤完成订单；无券商写操作。
- 修改`cloud_runner/runtime.py`：把快照配置比例纳入报告，实际有效单计数，按真实确认状态移除现金范围阻断。
- 修改`cloud_runner/notification.py`：顶部配置和估值、分割线、阶段相关买回/退出段、动态订单提醒、紧凑安全脚注。
- `tests/test_strategy_notification.py`、新增`tests/test_portfolio_scope.py`：先RED再最小实现；保留现有测试意图，仅更新经用户明确改动的文案断言。
- `docs/cloud-deployment.md`：记录用户新范围和配置字段；不手改账本、不把现金确认扩大为实盘上线。

## 顺序与命令

1. 确认现有82项基线；构造可区分市值与历史收盘估值、BASE与USD、IREN与其他资产、取消与PendingCancel的测试，观察有效失败。
2. 实现与定向回归，运行`python -m coverage run --source=trading_system,cloud_runner -m unittest discover -s tests -v`、`coverage report --fail-under=80`、compileall及git diff --check；新模块覆盖不少于80%。
3. 暂停本项目timer并确认service空闲，在调度锁内验证旧源码、备份旧文件及云端配置，再原子替换代码和配置；失败恢复旧文件和配置，测试通过后恢复timer。
4. 从systemd用新唯一任务名执行一轮真实IBKR读取、计算、企业微信发送；验收比例、无有效挂单、原始响应来源、完整排版和消息长度，核对源码哈希。
5. 保存私密验收报告，保持现有认证、Webhook和只读权限；不主动git add/commit/push。
