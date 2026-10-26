# devsin2 云端只读通知运行手册

部署根目录：`/home/ubuntu/apps/qqq-tqqq-strategy`。独立 `app`、`.venv`、`codex-home`、`config`、`state`、`logs`、`reports`、`backups`。无需本机桌面应用在线；云主机和外部授权必须有效。本次没有接管其他项目、没有配置真实订单提交。

## 当前执行边界

- 沿用 `sma200-rolling20-guard-sgov-v1`，以 QQQ 完整日线及共享纯策略核心计算。SGOV 为均线防守资产，未改为美元防守。
- 用户选择保留现有 QQQ/TQQQ，接入状态为 MIXED，不强制回调60/40；原GTC单已由用户取消，程序按实际有效挂单提示，不创建新指令。
- 用户于2026-10-26确认：除IREN外，其他资产及全部账户现金均属于策略。云端只读通知不自动改变周期，不把信号当成交。收盘持仓对账、实时报价和指令到订单关联仍未完成，因此不输出可执行限价/数量。
- 观察持仓和信号分别标记；有均线或20日退出会报告风险信号。漂移超过70%必须有已核实收盘净值，不能用当前快照或猜测的现金替代。后续人工交易后，应通过原交易系统对账与确认周期，不应只手改比例猜BOOST。

## 调度与数据

`qqq-tqqq-strategy.timer` 每天固定新加坡21:20触发；Python用XNYS日历检查该日是否美股交易日，休市跳过。只保留 `日期:preopen` 一个持久任务键，已取消开盘后与独立收盘后任务。夏令时为开盘前10分钟，冬令时为开盘前70分钟。重启补跑仅限21:20至21:30或当日开盘（取早者），不含右边界，过时跳过。一次任务汇总行情、账户、订单/成交、收盘证据检查、策略、通知及备份；每个任务只尝试一次，失败明确通知，不盲目重发。采集需要时间，推送通常晚于21:20触发时刻。

Codex 只调用六类读取工具：持仓、余额、订单、非约束指令列表、成交、日线。程序从 CLI JSONL `item.completed` 中提取券商原始响应，不采纳模型最终文字中的价格或数量。QQQ取一年日线；TQQQ/SGOV取一个月，均为RTH日线。数据少于200日、缺失交易日、未完整或过期均阻断；当天未收盘的日线不会被算入。

当前API返回没有账号字段，尾号绑定来自用户先前确认，不是持续自动认证。普通仓漂移检查仍需真实收盘持仓与策略净值。同次任务检查成交时间相对信号日收盘的边界，并保存实际成交查询参数；当前接口没有完整入出金、分红及资产流水，因此无新成交也不代表收盘净值已核实。`close_review.verified`保持false，`price_decision`独立报告纯价格信号；HOLD与DRIFT_CAP无法区分时`decision=null`，不能用当前仓位较低回退HOLD。通知明确“70%漂移待核对”。

## 认证与秘密

云端独立 Codex 0.164.0 使用用户设备授权。IBKR连接器复用此账号已有授权，并已在云端实际读取验证。不是在服务器保存IBKR密码；未来服务方撤销授权、会话过期或额度不足仍可能需要重新授权。

Webhook 存于 `config/wecom-webhook`，权限0600。Codex运行环境不注入Webhook；程序直接从文件发送，异常只保留固定错误码。私密根目录0700，运行使用umask0077。不要把auth、Webhook、账户快照和工具日志推送GitHub。

专用Codex配置默认禁用其他app，IBKR工具默认禁用，再按已实测名称允许六类读取；禁止交易指令创建、删除等写入工具。CLI沙箱只读。工具返回中的第三方文字只能当数据。

## 运维命令

```sh
ssh devsin2
cd /home/ubuntu/apps/qqq-tqqq-strategy/app
systemctl --user status qqq-tqqq-strategy.timer
systemctl --user list-timers qqq-tqqq-strategy.timer
journalctl --user -u qqq-tqqq-strategy.service --since today
../.venv/bin/python -m cloud_runner --root "$HOME/apps/qqq-tqqq-strategy" status
../bin/codex-strategy login status
```

在服务器交互使用本项目 Codex 时运行 `~/apps/qqq-tqqq-strategy/bin/codex-strategy`，它固定专用配置与工作目录；不改用户全局PATH，不与其他项目共享默认配置。

暂停：`systemctl --user stop qqq-tqqq-strategy.timer`，并按需创建 `state/STOP`。STOP阻止新增采集任务；不能撤销券商已有挂单，也不自动取消已经发出的通知。

恢复：先核对最近报告、未完成RUNNING任务、UNKNOWN通知和授权，再移除STOP并启动timer。不要删除jobs.json来强制重发；需要人工重跑时使用新的、可审计的任务名，例如 `run --job manual-20261026-1`。同名SENDING/UNKNOWN不重发，因为企业微信没有本系统可用的恰好一次键。

Codex重新登录使用专用位置：

```sh
CODEX_HOME="$HOME/apps/qqq-tqqq-strategy/codex-home" ../bin/codex login --device-auth
```

## 备份与恢复

每次完整任务后在调度锁内备份 `state/*.json`、策略阶段配置及存在的SQLite账本；SQLite通过backup API生成一致副本。保留最新14个私密备份包，不包括Webhook和Codex认证。包在本机磁盘，**不是异地灾备**；整台云盘丢失仍需独立云盘快照或外部备份。

恢复前停止timer和运行中的service，保留原文件；将备份解到单独恢复目录，核对manifest内容，再恢复状态并重新读取券商事实。恢复账本不意味着订单已撤销或可重发。JSON/SQLite恢复已有自动化测试；本次不重启整个服务器，以免影响其他项目，只重启本项目服务与定时器验证持久性。

## 通知与错误

通知顶部展示【今日动作】【信号依据】【当前配置】及策略资产估值，以空行、粗分割线区分【优先关注：卖出参考】【买回观察】【执行提醒】。不再展示股数、内部阶段英文、任务ID、版本和UTC采集时间戳；这些内容仍在私密JSON报告与日志中保留。

配置比例统一使用策略总资产：QQQ/TQQQ/SGOV等非IREN资产的账户快照市值，加全部USD cash_balance；BASE汇总不得再加一次。已核实合约526906130的IREN被排除；其他美元资产计入“其他策略资产”并额外披露。账户快照估值与信号日收盘指标分开，比例不是已经核实的收盘仓位或可立即使用的购买力。

`config/cloud.local.json`明确`cash_scope_confirmed=true`、`portfolio_scope="ALL_EXCEPT_IREN"`、`excluded_contract_ids=[526906130]`和用户确认来源。`portfolio.py`每次从原始持仓/现金计算`allocation`，不依赖AI填写权重或更改confirmed冒充数据核实。其他币种余额为0时可通过；非零外币尚未核实换算语义、缺失/非法市值、重复余额、负现金或空资产时显示“配置估值待核对”，不静默漏计资金。后续若需支持外币自动换算，应先核实IBKR exchange_rate的基准币语义。

有效挂单来自`get_account_orders(show_completed=true)`的动态状态过滤。CANCELLED/CANCELED/FILLED/REJECTED/EXPIRED不计入有效单；PendingCancel、未知或缺失状态保守保留待核对。无有效单时明确显示“当前无有效挂单”，不会继续显示旧单保留。

参考价保留两位并标记“约”，计算和边界仍使用原始精度。普通混合仓不因满足入场条件重复加仓；退出后必须等后续新的收盘信号。均线清仓后的首次恢复为可投资资产100%TQQQ，普通重新入场60/40。不同阶段只展示适用退出，触发均线时优先。消息在1900字节限制内保留收盘确认、每日更新等关键说明。

通知发送前持久化SENDING，收到`errcode=0`才记SENT；超时/连接不确定记UNKNOWN，业务拒绝记FAILED。CLI公开输出只含状态和任务名；完整账户数据留在私密报告。HTTP200不能单独证明消息成功。用户已确认收到部署测试消息。

云端认证/读取错误、缺日线、历史schema变化、周期与持仓冲突不会被解释为卖空仓或无挂单。出现BLOCKED时不生成可执行订单。网络与接口未来发生变化需维护；本次验收不代表盈利、成交质量或永不中断。

## 普通持仓的闲置现金分配

用户于2026-10-26明确选择：超过现金预留的部分只建议补买QQQ，保留现有TQQQ；不把现仓强制改成60/40。配置`cash_allocation_policy="EXCESS_CASH_TO_QQQ_IN_NORMAL_STATE"`及确认来源。该规则补充资金分配，不修改纯价格信号与周期。

仅普通MIXED/QQQ_ONLY阶段、价格信号HOLD、范围/估值有效且无订单/指令冲突时，计算`reserve=max(20美元,策略快照资产×2%)`；`budget=max(0,min(USD cash_balance,USD settled_cash)-reserve)`。预算至少1美元才提醒。结算余额不是购买力；缺字段不猜。均线防守、8%退出、重新入场等更高优先动作优先，BOOST和DEFENSIVE不套用补QQQ规则。

预算是未扣佣金的上限，不是委托金额；下单前还应扣除实际费用并刷新行情和账户。当前没有核实完整收盘流水，因此预算状态为REVIEW，通知保留漂移核查要求，不输出限价、数量或可提交指令。现金比例32.8%来自保留现仓接入及资金尚未分配，不是策略目标预留比例；新的通知会突出这部分预算。只有真实成交才能改变持仓。

## 仓库配置与通知样例

[云端配置模板](../config/cloud.example.json)不含账号、认证或Webhook。复制到部署根目录`config/cloud.local.json`后，根据已核对的实际周期和资金范围填写；模板默认`cash_scope_confirmed=false`，不能照抄为资金归属已确认。`binding_tail`仅记录人工核对，不代表连接器能够自动核实账户号码。Codex登录与IBKR插件授权保存在仓库外专用`codex-home`，只开放读取工具；不要把真实凭据提交到Git。

[通知输入样例](../deploy/notification.example.json)完全使用合成数据，只演示渲染，不是采集包，不可用于真实账户初始化。README末尾样例由`cloud_runner.notification.render`生成。可在源码目录运行：

```sh
python - <<'PYTHON'
import json
from pathlib import Path
from cloud_runner.notification import render
print(render(json.loads(Path("deploy/notification.example.json").read_text())))
PYTHON
```

公共仓库只包含程序、合成测试、配置模板和文档；真实持仓、账户配置、验收消息、工具轨迹、Webhook及Codex授权留在私密部署目录。
