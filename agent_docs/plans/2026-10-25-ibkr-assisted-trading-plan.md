# IBKR辅助交易系统实施计划

日期：2026-10-25。关联契约：`agent_docs/contracts/2026-10-25-ibkr-assisted-trading-contract.md`。
用户明确要求更新设计及代码，授权继续实施，不再重复请求确认。

## 项目证据

远端基线`b6e7601`：unittest共22项通过；公开仓库没有旧本地execution目录及CI，不从有未提交改动的旧检出复制实盘执行器。使用新的干净检出，保留旧目录所有改动。复用Python、unittest和现有pandas；交易日历采用先前项目已使用的exchange_calendars 4.13.2。

## 文件与顺序

1. `tests/test_trading_system.py`：先写严格边界、计划/持仓隔离、报价/账户阻断、部分成交、去重、发布未知、资金/分红及重启测试；确认新接口缺失导致RED。
2. `trading_system/strategy.py`：Decimal信号核心；`market.py`：数值、时间及XNYS完整交易日校验；`connector.py`：按已观察的IBKR字段映射，缺字段阻断。
3. `trading_system/ledger.py`：SQLite事务、事实/目标/计划/指令/订单/成交隔离；`service.py`：evaluate/prepare/record/reconcile状态机，无券商发送接口。
4. `trading_system/__main__.py`及`config/assisted.example.json`、`examples/assisted/`：离线CLI、默认SHADOW、合成fixture和可复制演示。
5. `research/sma200_sgov.py`：研究入口调用相同信号核心；不冒充重新计算旧图表。
6. `docs/trading-system.md`、README、SGOV说明及`.github/workflows/tests.yml`：运行手册、能力证据与限制、CI、覆盖率；仅提交必要源码及合成资料。

## 验收

运行目录：本仓库根。解释器暂复用相邻旧检出的已安装虚拟环境，不改旧目录。

- 基线：`python -m unittest discover -s tests -q`。
- RED/GREEN：`python -m unittest discover -s tests -p test_trading_system.py -v`。
- 全量：`python -m coverage run --source=trading_system -m unittest discover -s tests -v`；`python -m coverage report --fail-under=80`。
- 编译：`python -m compileall -q trading_system research tests`。
- 合成CLI演示、重启恢复、实际连接器字段测试；不访问账户或提交交易。
- `git diff --check`、提交范围和敏感信息核对；提交推送后验证远端版本。

## 字段映射与Git卫生

`bid-ask`不同于`last`，买卖报价时间不能借用`last.ts`；`total_size`缺失时不把描述中的美元金额当股数；trade_id去重不同于order_id分组；实际佣金不同于研究费率；指令expiration不同于DAY。

不提交本地金融快照、账号、授权、数据库、日志、覆盖率、虚拟环境或缓存。新包不依赖旧整股执行器，不启用定时任务。正式测试和合成fixture保留。

## 执行记录

- [x] 远端基线22项测试通过。
- [x] 新接口不存在的有效RED；后续新增用例也先确认失败再修复。
- [x] GREEN、实际连接器缺失/冻结字段、部分成交、跨日风险和崩溃恢复。
- [x] 2026-10-26本地全量58项通过（原有22＋新增36）；整个新包语句覆盖率88%，策略核心100%；CLI合成演示及一致性备份恢复通过。
- [x] compileall、Ruff格式及定向E9/F63/F7/F82/F401检查、git diff --check通过。未引入新格式工具依赖。
- [x] 提交范围与敏感信息检查：仅源码、文档、配置模板及合成fixture，排除所有真实金融数据和运行状态。

GitHub推送与远端CI状态以实际提交及Actions记录为准；不把本地通过当成云端CI已通过。
