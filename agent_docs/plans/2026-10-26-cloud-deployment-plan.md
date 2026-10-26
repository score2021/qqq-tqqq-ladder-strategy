# devsin2 云端部署实施计划

关联契约：`agent_docs/contracts/2026-10-26-cloud-deployment-contract.md`。用户已授权完整实施及验收，按此推进，不重复请求部署许可。

## 范围与结构

- `cloud_runner/`：日历调度、只读 Codex 编排、严格数据校验、策略计算、企业微信通知和持久去重。
- `tests/test_cloud_runner.py`：新增正式单测，沿用 unittest；先 RED，再 GREEN。
- `deploy/`：systemd 服务/定时器、受限工作指令、输入输出结构。
- `docs/cloud-deployment.md`：启动、停机、认证恢复与运维说明。
- 更新 CI 编译、覆盖率检查范围。新生产代码语句覆盖率不少于80%，原策略测试回归。
- 云主机安装目录与实际私密配置、运行报告、授权缓存不进入 Git；不主动提交推送。

## 顺序

1. 创建独立目录并保存 Webhook；安装隔离 Codex 与 Python，启动官方设备认证，等待用户授权期间完成独立实现。
2. 编写调度、指标、通知与去重边界测试并确认有效失败，再实现模块；不修改纯策略规则。
3. 云端安装 IBKR 插件并只读验证；缺授权发起官方流程，不复制未知本机券商密钥。
4. 正式运行 Python 测试、覆盖率、编译、合成报告、真实 IBKR 取数及企业微信端到端测试。
5. 安装独立 systemd 调度，验证重启恢复、配置权限、日志无秘密与单任务锁。验收报告分通过、失败、待用户授权和未运行，不声称未验证能力。

## 命令

工作目录为隔离代码仓库：`python -m unittest discover -s tests -v`、`python -m coverage run --source=trading_system,cloud_runner -m unittest discover -s tests -v`、`python -m coverage report --fail-under=80`、`python -m compileall -q trading_system cloud_runner tests`、`git diff --check`。
