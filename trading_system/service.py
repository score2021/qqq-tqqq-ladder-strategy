"""策略目标与真实成交分离；本模块没有网络或券商发送能力。"""

from datetime import timedelta
from functools import wraps
import uuid

from .ledger import Ledger, digest
from .market import Calendar, InvalidInput, floor_step, number, positive, stamp
from .strategy import PHASES, SYMBOLS, VERSION, decide, indicators

CONTRACTS = {"QQQ": "320227571", "TQQQ": "72539702", "SGOV": "424099317"}
TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
RISK = {"MA_DEFENSE", "ROLLING20_EXIT"}


def operation(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        try:
            with self.store.transaction():
                result = function(self, *args, **kwargs)
                self.store.put(
                    "run",
                    uuid.uuid4().hex,
                    dict(
                        command=function.__name__,
                        at=stamp(args[-1]).isoformat(),
                        status=result["status"],
                        reason=result.get("reason"),
                    ),
                )
                return result
        except (InvalidInput, KeyError, TypeError, ValueError) as exc:
            return dict(status="BLOCKED", reason=str(exc))

    return call


class Service:
    def __init__(self, path, config=None):
        self.config = dict(config or {})
        allowed = {
            "mode",
            "binding_ref",
            "scope_confirmed",
            "account_binding_confirmed",
            "migration_confirmed",
            "fee_budget_confirmed",
            "fee_cap_per_order",
            "instrument_rules_confirmed",
            "quantity_steps",
            "ticks",
            "quote_seconds",
            "snapshot_seconds",
            "plan_seconds",
            "max_spread",
            "dust_usd",
            "stop_file",
            "strategy_version",
        }
        if set(self.config) - allowed or self.config.get("mode", "SHADOW") not in (
            "SHADOW",
            "REVIEW",
        ):
            raise InvalidInput("INVALID_CONFIG")
        if self.config.get("strategy_version", VERSION) != VERSION:
            raise InvalidInput("STRATEGY_VERSION_MISMATCH")
        for key, default in [
            ("quote_seconds", 30),
            ("snapshot_seconds", 30),
            ("plan_seconds", 300),
            ("max_spread", ".003"),
            ("dust_usd", "1"),
        ]:
            positive(self.config.get(key, default))
        self.store = Ledger(path)
        self.calendar = Calendar()

    def close(self):
        self.store.close()

    def backup(self, destination):
        self.store.backup(destination)
        return dict(
            status="BACKUP_CREATED",
            path=str(destination),
            restore_requires_reconciliation=True,
        )

    def state(self):
        state = self.store.get("state", "current")
        if state is None:
            raise InvalidInput("INITIALIZATION_REQUIRED")
        if state["version"] != VERSION or state["binding_ref"] != self.config.get(
            "binding_ref"
        ):
            raise InvalidInput("STATE_BINDING_OR_VERSION_MISMATCH")
        return state

    def save(self, state):
        self.store.put("state", "current", state)

    def _adoption(self, positions, phase, recovery_eligible):
        if phase not in PHASES:
            raise InvalidInput("INVALID_PHASE")
        if phase == "QQQ_ONLY" and (
            number(positions["TQQQ"]) or number(positions["SGOV"])
        ):
            raise InvalidInput("PHASE_POSITION_CONFLICT")
        if phase in ("BOOST", "MIXED") and (
            number(positions["TQQQ"]) <= 0 or number(positions["SGOV"])
        ):
            raise InvalidInput("PHASE_POSITION_CONFLICT")
        if phase == "DEFENSIVE" and (
            number(positions["QQQ"])
            or number(positions["TQQQ"])
            or recovery_eligible is not True
        ):
            raise InvalidInput("DEFENSE_HISTORY_REQUIRED")

    def _snapshot(self, snap, now=None):
        if snap["binding_ref"] != self.config.get("binding_ref"):
            raise InvalidInput("ACCOUNT_BINDING_MISMATCH")
        if (
            snap.get("orders_complete") is not True
            or snap.get("instructions_complete") is not True
        ):
            raise InvalidInput("INCOMPLETE_SNAPSHOT")
        at = stamp(snap["at"])
        if now is not None and not 0 <= (stamp(now) - at).total_seconds() <= float(
            self.config.get("snapshot_seconds", 30)
        ):
            raise InvalidInput("STALE_SNAPSHOT")
        if set(snap["positions"]) != set(SYMBOLS):
            raise InvalidInput("STRATEGY_SCOPE_REQUIRED")
        for qty in snap["positions"].values():
            if number(qty) < 0:
                raise InvalidInput("SHORT_POSITION_NOT_ALLOWED")
        if number(snap["cash"]) < 0 or number(snap["settled_available_usd"]) < 0:
            raise InvalidInput("NEGATIVE_CASH")
        for symbol in SYMBOLS:
            positive(snap["marks"][symbol])

    def _matching(self, state, snap):
        return all(
            number(state["positions"][s]) == number(snap["positions"][s])
            for s in SYMBOLS
        ) and abs(number(state["cash"]) - number(snap["cash"])) <= number(".01")

    def initialize(
        self, snapshot, phase="UNINITIALIZED", confirmed=False, recovery_eligible=False
    ):
        with self.store.transaction():
            if self.store.get("state", "current") is not None:
                raise InvalidInput("ALREADY_INITIALIZED")
            # 未绑定的SHADOW仍可保存事实，但不能授权执行。
            if "binding_ref" not in self.config:
                self.config["binding_ref"] = snapshot["binding_ref"]
            self._snapshot(snapshot)
            enabled = confirmed is True and all(
                self.config.get(x) is True
                for x in (
                    "migration_confirmed",
                    "account_binding_confirmed",
                    "scope_confirmed",
                )
            )
            phase = phase if enabled else "UNINITIALIZED"
            pos = snapshot["positions"]
            if phase != "UNINITIALIZED":
                self._adoption(pos, phase, recovery_eligible)
            state = dict(
                version=VERSION,
                binding_ref=snapshot["binding_ref"],
                phase=phase,
                positions={s: str(number(pos[s])) for s in SYMBOLS},
                cash=str(number(snapshot["cash"])),
                receivables="0",
                fees="0",
                recovery_eligible=bool(recovery_eligible),
                last_signal_day=None,
                goal_id=None,
                reconcile_required=False,
                pause=None,
            )
            self.save(state)
            self.store.put("snapshot", digest(snapshot), snapshot)
            return dict(status="INITIALIZED", phase=phase)

    def _goal(self, state):
        return (
            self.store.get("goal", state["goal_id"]) if state.get("goal_id") else None
        )

    def _external_pending(self):
        return [
            p
            for p in self.store.all("plan")
            if p["status"] in ("PUBLISHING", "PUBLISHED", "BOUND")
        ]

    @operation
    def evaluate(self, frame, now):
        state = self.state()
        day = self.calendar.validate_bars(frame["bars"], now)
        if not frame.get("source") or frame.get("adjustment") != "split_adjusted":
            raise InvalidInput("PRICE_BASIS_REQUIRED")
        close = frame["close"]
        if close.get("confirmed") is not True or close["date"] != day:
            raise InvalidInput("CLOSE_FACTS_UNCONFIRMED")
        if set(close["positions"]) != set(SYMBOLS):
            raise InvalidInput("STRATEGY_SCOPE_REQUIRED")
        # 缺失的历史持仓不能用当前持仓冒充；调用方需有收盘事实。
        amounts = {
            s: number(close["positions"][s]) * positive(close["marks"][s])
            for s in SYMBOLS
        }
        if any(x < 0 for x in amounts.values()):
            raise InvalidInput("NEGATIVE_CLOSE_POSITION")
        nav = (
            sum(amounts.values())
            + number(close["cash"])
            + number(close.get("receivables", "0"))
        )
        if nav <= 0:
            raise InvalidInput("INVALID_CLOSE_NAV")
        values = indicators([x["close"] for x in frame["bars"]])
        if positive(close["marks"]["QQQ"]) != positive(values["close"]):
            raise InvalidInput("QQQ_CLOSE_BASIS_MISMATCH")
        if state["phase"] == "UNINITIALIZED":
            if self.config.get("mode", "SHADOW") != "SHADOW":
                raise InvalidInput("MIGRATION_CONFIRMATION_REQUIRED")
            self.store.put("input", digest(frame), frame)
            return dict(
                status="SHADOW_ANALYSIS",
                reason="PHASE_UNCONFIRMED_NO_GOAL_CREATED",
                indicators=values,
                scenarios={
                    p: decide(
                        p,
                        **values,
                        tqqq_weight=amounts["TQQQ"] / nav,
                        recovery_eligible=p == "DEFENSIVE",
                    )
                    for p in PHASES
                },
            )
        result = decide(
            state["phase"],
            **values,
            tqqq_weight=amounts["TQQQ"] / nav,
            recovery_eligible=state["recovery_eligible"],
        )
        self.store.put(
            "observation",
            digest([frame, state["phase"]]),
            dict(
                date=day, input_hash=digest(frame), indicators=values, decision=result
            ),
        )
        self.store.put("input", digest(frame), frame)
        key = digest([VERSION, state["binding_ref"], day])
        previous = self.store.get("decision", key)
        if previous is not None:
            if previous["input_hash"] != digest(frame):
                raise InvalidInput("SIGNAL_DATA_REVISION_REQUIRES_REVIEW")
            return dict(status="EXISTING_DECISION", decision=previous)
        if state["last_signal_day"] and day < state["last_signal_day"]:
            raise InvalidInput("SIGNAL_TIME_REVERSED")
        state["last_signal_day"] = day
        state["entry_allowed"] = positive(values["close"]) > positive(
            values["sma200"]
        ) * number("1.01") and positive(values["close"]) > positive(
            values["peak20"]
        ) * number(".92")
        old = self._goal(state)
        if old and old["status"] == "ACTIVE":
            if old["reason"] in RISK and (
                old["reason"] == "MA_DEFENSE" or result["reason"] != "MA_DEFENSE"
            ):
                self.save(state)
                return dict(status="WAIT_EXECUTION", reason="PENDING_RISK_GOAL")
            if result["reason"] == old["reason"] or (
                result["reason"] == "HOLD" and state["entry_allowed"]
            ):
                self.save(state)
                return dict(status="WAIT_EXECUTION", reason="PENDING_GOAL")
            if self._external_pending():
                state["pause"] = "SIGNAL_CHANGED_REQUIRES_ORDER_RESOLUTION"
                self.save(state)
                return dict(status="BLOCKED", reason=state["pause"])
            old["status"] = "SUPERSEDED"
            self.store.put("goal", old["id"], old)
            for plan in self.store.all("plan"):
                if plan["status"] == "PLANNED":
                    plan["status"] = "SUPERSEDED"
                    self.store.put("plan", plan["id"], plan)
        state["pause"] = None
        record = dict(
            id=key, date=day, input_hash=digest(frame), indicators=values, **result
        )
        self.store.put("input", record["input_hash"], frame)
        self.store.put("decision", key, record)
        if result["weights"] is not None:
            goal = dict(
                id=key,
                signal_day=day,
                phase=result["phase"],
                reason=result["reason"],
                weights=result["weights"],
                status="ACTIVE",
                target_qty=None,
                stage="SELL",
            )
            self.store.put("goal", key, goal)
            state["goal_id"] = key
        else:
            state["goal_id"] = None
        self.save(state)
        return dict(
            status="DECISION_READY" if result["weights"] else "NO_ACTION",
            decision=record,
        )

    def _quotes(self, quotes, now):
        for symbol in SYMBOLS:
            quote = quotes[symbol]
            if (
                quote.get("live") is not True
                or quote.get("timestamp_verified") is not True
            ):
                raise InvalidInput("UNVERIFIED_LIVE_QUOTE")
            bid, ask = positive(quote["bid"]), positive(quote["ask"])
            if ask < bid or (ask - bid) / bid > positive(
                self.config.get("max_spread", ".003")
            ):
                raise InvalidInput("INVALID_SPREAD")
            if (
                not 0
                <= (stamp(now) - stamp(quote["at"])).total_seconds()
                <= float(self.config.get("quote_seconds", 30))
            ):
                raise InvalidInput("STALE_QUOTE")

    @operation
    def prepare(self, snapshot, quotes, now):
        state = self.state()
        self._snapshot(snapshot, now)
        if state["phase"] == "UNINITIALIZED":
            raise InvalidInput("MIGRATION_CONFIRMATION_REQUIRED")
        if state["pause"] or state["reconcile_required"]:
            raise InvalidInput(state["pause"] or "RECONCILE_REQUIRED")
        from pathlib import Path

        if self.config.get("stop_file") and Path(self.config["stop_file"]).exists():
            raise InvalidInput("STOP_FILE_PRESENT")
        for name in (
            "scope_confirmed",
            "account_binding_confirmed",
            "migration_confirmed",
            "fee_budget_confirmed",
            "instrument_rules_confirmed",
        ):
            if self.config.get(name) is not True:
                raise InvalidInput(name.upper() + "_REQUIRED")
        if not self._matching(state, snapshot):
            raise InvalidInput("RECONCILE_REQUIRED")
        for order in snapshot["orders"]:
            if self.store.get("order", order["order_id"]) is None:
                if any(p["status"] == "PUBLISHED" for p in self._external_pending()):
                    return dict(
                        status="NEEDS_BINDING",
                        reason="UNBOUND_REAL_ORDER",
                        order_id=order["order_id"],
                    )
                raise InvalidInput("UNCLASSIFIED_ORDER")
        for instruction in snapshot["instructions"]:
            if self.store.get("instruction", instruction["id"]) is None:
                raise InvalidInput("UNCLASSIFIED_INSTRUCTION")
        goal = self._goal(state)
        if not goal or goal["status"] != "ACTIVE":
            return dict(status="NO_ACTION", reason="NO_ACTIVE_GOAL")
        pending = self._external_pending()
        if pending:
            plan = pending[0]
            status = {
                "PUBLISHING": "UNKNOWN_PUBLICATION",
                "PUBLISHED": "WAIT_USER",
                "BOUND": "WAIT_FILL",
            }[plan["status"]]
            return dict(
                status=status,
                reason="STALE_INSTRUCTION"
                if stamp(plan["expires"]) < stamp(now)
                else plan["status"],
                plan=plan,
            )
        if state["last_signal_day"] != self.calendar.latest_complete(now):
            raise InvalidInput("REEVALUATE_LATEST_CLOSE")
        if not self.calendar.executable(state["last_signal_day"], now):
            raise InvalidInput("OUTSIDE_EXECUTION_SESSION")
        self._quotes(quotes, now)
        fee = positive(self.config["fee_cap_per_order"])
        qty = {s: number(state["positions"][s]) for s in SYMBOLS}
        cash = number(state["cash"])
        nav = cash + sum(
            qty[s] * (positive(quotes[s]["bid"]) + positive(quotes[s]["ask"])) / 2
            for s in SYMBOLS
        )
        reserve = max(number("20"), nav * number(".02"))
        # 六笔是三只资产分别卖/买的预算上限，不是实际佣金或按成交片段收费。
        investable = max(number("0"), nav - reserve - fee * 6)
        if investable <= 0:
            return dict(
                status="WAIT_FUNDS", reason="RESERVE_AND_FEE_BUDGET_EXHAUST_CAPITAL"
            )
        steps = {
            s: positive(self.config.get("quantity_steps", {}).get(s, ".000001"))
            for s in SYMBOLS
        }
        dust = positive(self.config.get("dust_usd", "1"))
        if goal["stage"] == "SELL":
            target = {
                s: floor_step(
                    investable
                    * number(goal["weights"][s])
                    / positive(quotes[s]["ask"]),
                    steps[s],
                )
                for s in SYMBOLS
            }
            goal["target_qty"] = {s: str(target[s]) for s in SYMBOLS}
        else:
            target = {s: number(goal["target_qty"][s]) for s in SYMBOLS}
        sells = [
            s
            for s in SYMBOLS
            if qty[s] > target[s]
            and (
                (qty[s] - target[s]) * positive(quotes[s]["bid"]) > dust
                or number(goal["weights"][s]) == 0
            )
        ]
        if not sells:
            goal["stage"] = "BUY"
        buys = [
            s
            for s in SYMBOLS
            if target[s] > qty[s]
            and (target[s] - qty[s]) * positive(quotes[s]["ask"]) > dust
        ]
        if not sells and not buys:
            self._finish(state, goal)
            return dict(
                status="NO_ACTION",
                reason="TARGET_REACHED",
                residuals={s: str(target[s] - qty[s]) for s in SYMBOLS},
            )
        side = "SELL" if sells else "BUY"
        symbol = (sells or buys)[0]
        if side == "BUY" and symbol == "TQQQ" and not state.get("entry_allowed"):
            raise InvalidInput("ENTRY_FILTER_NO_LONGER_VALID")
        tick = positive(self.config.get("ticks", {}).get(symbol, ".01"))
        price = (
            floor_step(quotes[symbol]["bid"], tick)
            if side == "SELL"
            else -floor_step(-positive(quotes[symbol]["ask"]), tick)
        )
        positive(price)
        amount = floor_step(
            qty[symbol] - target[symbol] if sells else target[symbol] - qty[symbol],
            steps[symbol],
        )
        if side == "BUY":
            usable = (
                min(cash, number(snapshot["settled_available_usd"]))
                - reserve
                - fee * len(buys)
            )
            amount = min(
                amount, floor_step(max(number("0"), usable) / price, steps[symbol])
            )
        if amount <= 0 and side == "SELL":
            raise InvalidInput("UNTRADEABLE_RESIDUAL")
        if amount <= 0:
            return dict(status="WAIT_FUNDS", reason="RESERVE_OR_SETTLEMENT")
        context = digest(
            [
                goal,
                qty_as_strings(qty),
                snapshot["cash"],
                snapshot["settled_available_usd"],
                {s: [quotes[s]["bid"], quotes[s]["ask"]] for s in SYMBOLS},
                self.config,
            ]
        )
        for old in self.store.all("plan"):
            if old["status"] == "PLANNED":
                if old["context"] == context and stamp(now) < stamp(old["expires"]):
                    old["validated_at"] = stamp(now).isoformat()
                    old["quote_at"] = {s: quotes[s]["at"] for s in SYMBOLS}
                    old["snapshot_at"] = snapshot["at"]
                    self.store.put("plan", old["id"], old)
                    return dict(
                        status="PLAN_READY"
                        if self.config.get("mode") == "REVIEW"
                        else "SHADOW_PLAN",
                        plan=old,
                    )
                old["status"] = "EXPIRED"
                self.store.put("plan", old["id"], old)
        plan = dict(
            id=uuid.uuid4().hex,
            goal_id=goal["id"],
            symbol=symbol,
            status="PLANNED",
            context=context,
            created=stamp(now).isoformat(),
            validated_at=stamp(now).isoformat(),
            config_hash=digest(self.config),
            expires=(
                stamp(now)
                + timedelta(seconds=float(self.config.get("plan_seconds", 300)))
            ).isoformat(),
            reserve=str(reserve),
            fee_budget=str(fee),
            target_qty=goal["target_qty"],
            strategy_version=VERSION,
            binding_ref=state["binding_ref"],
            signal_day=state["last_signal_day"],
            reason=goal["reason"],
            actual_positions=state["positions"],
            cash_before=state["cash"],
            quote_at={s: quotes[s]["at"] for s in SYMBOLS},
            snapshot_at=snapshot["at"],
            payload=dict(
                contract_id_ex=CONTRACTS[symbol],
                side=side,
                quantity=str(amount),
                order_type="LIMIT",
                limit_price=str(price),
                time_in_force="DAY",
            ),
        )
        self.store.put("goal", goal["id"], goal)
        self.store.put("snapshot", digest(snapshot), snapshot)
        self.store.put("quotes", digest(quotes), quotes)
        self.store.put("plan", plan["id"], plan)
        return dict(
            status="PLAN_READY"
            if self.config.get("mode") == "REVIEW"
            else "SHADOW_PLAN",
            plan=plan,
        )

    def _finish(self, state, goal):
        state["phase"] = goal["phase"]
        state["recovery_eligible"] = goal["phase"] == "DEFENSIVE"
        goal["status"] = "DONE"
        self.store.put("goal", goal["id"], goal)
        self.save(state)

    @operation
    def record(self, event, now):
        state = self.state()
        kind = event["kind"]
        self.store.put("external_evidence", digest(event), event)
        if kind == "migration_confirmation":
            if state["phase"] != "UNINITIALIZED" or event.get("confirmed") is not True:
                raise InvalidInput("MIGRATION_CONFIRMATION_REQUIRED")
            if not all(
                self.config.get(x) is True
                for x in (
                    "migration_confirmed",
                    "scope_confirmed",
                    "account_binding_confirmed",
                )
            ):
                raise InvalidInput("MIGRATION_CONFIGURATION_UNCONFIRMED")
            self._snapshot(event["snapshot"], now)
            if not self._matching(state, event["snapshot"]) or self._external_pending():
                raise InvalidInput("RECONCILE_REQUIRED")
            phase = event["phase"]
            eligible = event.get("recovery_eligible", False)
            self._adoption(state["positions"], phase, eligible)
            state.update(
                phase=phase, recovery_eligible=eligible, reconcile_required=False
            )
            self.save(state)
            return dict(status="MIGRATION_CONFIRMED", phase=phase)
        if kind == "publication_start":
            plan = self.store.get("plan", event["plan_id"])
            if plan is None:
                raise InvalidInput("UNKNOWN_PLAN")
            if plan["status"] == "PUBLISHING":
                return dict(status="UNKNOWN_PUBLICATION", reason="QUERY_BEFORE_RETRY")
            if self.config.get("mode", "SHADOW") != "REVIEW":
                raise InvalidInput("SHADOW_CANNOT_PUBLISH")
            if state["pause"] or state["reconcile_required"]:
                raise InvalidInput("RECONCILE_REQUIRED")
            if plan["status"] != "PLANNED":
                raise InvalidInput("PLAN_ALREADY_PUBLISHED_OR_SUPERSEDED")
            if stamp(now) >= stamp(plan["expires"]):
                raise InvalidInput("PLAN_EXPIRED")
            if plan["config_hash"] != digest(self.config):
                raise InvalidInput("EXECUTION_CONFIG_CHANGED")
            if any(
                not 0
                <= (stamp(now) - stamp(at)).total_seconds()
                <= float(self.config.get("quote_seconds", 30))
                for at in plan["quote_at"].values()
            ) or not 0 <= (
                stamp(now) - stamp(plan["snapshot_at"])
            ).total_seconds() <= float(self.config.get("snapshot_seconds", 30)):
                raise InvalidInput("REPREPARE_FRESH_QUOTES")
            from pathlib import Path

            if self.config.get("stop_file") and Path(self.config["stop_file"]).exists():
                raise InvalidInput("STOP_FILE_PRESENT")
            plan["status"] = "PUBLISHING"
            self.store.put("plan", plan["id"], plan)
            return dict(status="PUBLISHING", payload=plan["payload"])
        if kind == "instruction_receipt":
            plan = self.store.get("plan", event["plan_id"])
            if not plan or plan["status"] not in ("PUBLISHING", "PUBLISHED"):
                raise InvalidInput("NO_PUBLICATION_ATTEMPT")
            if event["payload"] != plan["payload"]:
                raise InvalidInput("INSTRUCTION_MISMATCH")
            iid = str(event["instruction_id"])
            existing = self.store.get("instruction", iid)
            if existing and existing["plan_id"] != plan["id"]:
                raise InvalidInput("DUPLICATE_INSTRUCTION_ID")
            if plan.get("instruction_id") and plan["instruction_id"] != iid:
                raise InvalidInput("MULTIPLE_INSTRUCTIONS_FOR_PLAN")
            self.store.put(
                "instruction",
                iid,
                dict(
                    id=iid,
                    plan_id=plan["id"],
                    url=event.get("url"),
                    remote_expiration=event.get("expiration"),
                ),
            )
            plan.update(status="PUBLISHED", instruction_id=iid)
            self.store.put("plan", plan["id"], plan)
            return dict(status="WAIT_USER", instruction_id=iid)
        if kind == "instruction_absent":
            plan = self.store.get("plan", event["plan_id"])
            if (
                not plan
                or plan["status"] not in ("PUBLISHING", "PUBLISHED")
                or event.get("user_confirmed_no_order") is not True
            ):
                raise InvalidInput("ABSENCE_AND_NO_REAL_ORDER_MUST_BE_CONFIRMED")
            plan["status"] = "CANCELLED"
            self.store.put("plan", plan["id"], plan)
            return dict(status="RECOVERED", reason="NO_EXTERNAL_ORDER_CONFIRMED")
        if kind == "order_binding":
            instruction = self.store.get("instruction", event["instruction_id"])
            if not instruction or event.get("user_confirmed") is not True:
                raise InvalidInput("ORDER_BINDING_CONFIRMATION_REQUIRED")
            plan = self.store.get("plan", instruction["plan_id"])
            payload = plan["payload"]
            if (
                event["binding_ref"] != state["binding_ref"]
                or event["symbol"] != plan["symbol"]
                or event["side"] != payload["side"]
                or number(event["quantity"]) != number(payload["quantity"])
            ):
                raise InvalidInput("ORDER_BINDING_MISMATCH")
            oid = str(event["order_id"])
            old = self.store.get("order", oid)
            if old and old["plan_id"] != plan["id"]:
                raise InvalidInput("ORDER_ID_ALREADY_BOUND")
            if old:
                return dict(
                    status="EXISTING_BINDING", order_id=oid, order_status=old["status"]
                )
            if plan.get("order_id") and plan["order_id"] != oid:
                raise InvalidInput("MULTIPLE_ORDERS_FOR_PLAN")
            self.store.put(
                "order",
                oid,
                dict(
                    id=oid,
                    plan_id=plan["id"],
                    symbol=plan["symbol"],
                    side=payload["side"],
                    quantity=payload["quantity"],
                    status="NEW",
                ),
            )
            plan.update(status="BOUND", order_id=oid)
            self.store.put("plan", plan["id"], plan)
            return dict(status="WAIT_FILL", order_id=oid)
        if kind in ("cashflow", "dividend_ex", "dividend_pay"):
            eid = str(event["event_id"])
            old = self.store.get("cash_event", eid)
            if old:
                if old != event:
                    raise InvalidInput("EVENT_ID_CONFLICT")
                return dict(status="EXISTING_EVENT")
            if not event.get("source"):
                raise InvalidInput("CASH_EVENT_SOURCE_REQUIRED")
            amount = number(event["amount"])
            if kind == "cashflow":
                if (
                    event.get("confirmed") is not True
                    or number(state["cash"]) + amount < 0
                ):
                    raise InvalidInput("INVALID_CASHFLOW")
                state["cash"] = str(number(state["cash"]) + amount)
            elif kind == "dividend_ex":
                positive(amount)
                state["receivables"] = str(number(state["receivables"]) + amount)
            else:
                ex = self.store.get("cash_event", event["ex_event_id"])
                if (
                    not ex
                    or ex["kind"] != "dividend_ex"
                    or amount < 0
                    or amount > number(ex["amount"])
                ):
                    raise InvalidInput("INVALID_DIVIDEND_PAYMENT")
                if any(
                    x.get("ex_event_id") == event["ex_event_id"]
                    for x in self.store.all("cash_event")
                ):
                    raise InvalidInput("DIVIDEND_ALREADY_PAID")
                state["receivables"] = str(
                    number(state["receivables"]) - number(ex["amount"])
                )
                state["cash"] = str(number(state["cash"]) + amount)
            self.store.put("cash_event", eid, event)
            self.save(state)
            return dict(status="EVENT_RECORDED")
        raise InvalidInput("UNKNOWN_EXTERNAL_EVENT")

    @operation
    def reconcile(self, event, now):
        state = self.state()
        snap = event["snapshot"]
        self._snapshot(snap, now)
        for fill in event["fills"]:
            order = self.store.get("order", fill["order_id"])
            if order is None:
                raise InvalidInput("UNBOUND_ORDER")
            if fill["symbol"] != order["symbol"] or fill["side"] != order["side"]:
                raise InvalidInput("FILL_ORDER_MISMATCH")
            tid = str(fill["trade_id"])
            old = self.store.get("fill", tid)
            size, price = positive(fill["quantity"]), positive(fill["price"])
            fee = (
                number(fill["commission"])
                if fill.get("commission") is not None
                else number(old["commission"])
                if old and old.get("commission") is not None
                else number("0")
            )
            if old and (
                any(str(old[k]) != str(fill[k]) for k in ("order_id", "symbol", "side"))
                or any(number(old[k]) != number(fill[k]) for k in ("quantity", "price"))
            ):
                raise InvalidInput("TRADE_CORRECTION_REQUIRES_REVIEW")
            prior = number(old.get("commission") or "0") if old else number("0")
            if not old:
                total = (
                    sum(
                        number(x["quantity"])
                        for x in self.store.all("fill")
                        if x["order_id"] == str(fill["order_id"])
                    )
                    + size
                )
                if total > number(order["quantity"]):
                    raise InvalidInput("OVERFILL_REQUIRES_REVIEW")
                delta = size if fill["side"] == "BUY" else -size
                symbol = fill["symbol"]
                state["positions"][symbol] = str(
                    number(state["positions"][symbol]) + delta
                )
                state["cash"] = str(number(state["cash"]) - delta * price)
                if number(state["positions"][symbol]) < 0:
                    raise InvalidInput("OVERSELL")
            state["cash"] = str(number(state["cash"]) - (fee - prior))
            state["fees"] = str(number(state["fees"]) + fee - prior)
            pending = fill.get("commission") is None and (
                not old or old.get("commission_pending", False)
            )
            saved = dict(
                fill,
                order_id=str(fill["order_id"]),
                commission=str(fee),
                commission_pending=pending,
            )
            self.store.put("fill", tid, saved)
            goal = self._goal(state)
            if (
                goal
                and goal["reason"] == "MA_DEFENSE"
                and number(state["positions"]["QQQ"]) == 0
                and number(state["positions"]["TQQQ"]) == 0
            ):
                state["phase"] = "DEFENSIVE"
                state["recovery_eligible"] = True
            if (
                goal
                and goal["reason"] == "MA_RECOVERY"
                and fill["symbol"] == "TQQQ"
                and fill["side"] == "BUY"
            ):
                state["phase"] = "BOOST"
                state["recovery_eligible"] = False
            if (
                goal
                and goal["phase"] == "MIXED"
                and fill["symbol"] == "TQQQ"
                and fill["side"] == "BUY"
            ):
                state["phase"] = "MIXED"
                state["recovery_eligible"] = False
        for view in event["orders"]:
            order = self.store.get("order", view["order_id"])
            if order is None:
                raise InvalidInput("UNBOUND_ORDER")
            status = view["status"]
            if status not in TERMINAL | {
                "NEW",
                "SUBMITTED",
                "PARTIALLY_FILLED",
                "PENDING_CANCEL",
            }:
                raise InvalidInput("UNKNOWN_ORDER_STATUS")
            if order["status"] in TERMINAL and status not in TERMINAL:
                raise InvalidInput("ORDER_STATUS_REVERSED")
            filled = sum(
                number(x["quantity"])
                for x in self.store.all("fill")
                if x["order_id"] == order["id"]
            )
            if status == "FILLED" and filled != number(order["quantity"]):
                raise InvalidInput("MISSING_FILLED_EXECUTIONS")
            order["status"] = status
            self.store.put("order", order["id"], order)
            if status in TERMINAL:
                plan = self.store.get("plan", order["plan_id"])
                plan["status"] = "TERMINAL"
                self.store.put("plan", plan["id"], plan)
        matched = self._matching(state, snap)
        state["reconcile_required"] = not matched
        self.save(state)
        self.store.put("snapshot", digest(snap), snap)
        if not matched:
            return dict(status="RECONCILE_REQUIRED", reason="BROKER_LEDGER_MISMATCH")
        if self._external_pending():
            return dict(status="WAIT_FILL", reason="EXTERNAL_WORK_PENDING")
        goal = self._goal(state)
        if goal and goal["status"] == "ACTIVE" and goal["target_qty"]:
            reached = all(
                (
                    number(state["positions"][s]) == 0
                    if number(goal["weights"][s]) == 0
                    else abs(
                        number(state["positions"][s]) - number(goal["target_qty"][s])
                    )
                    * positive(snap["marks"][s])
                    <= positive(self.config.get("dust_usd", "1"))
                )
                for s in SYMBOLS
            )
            if reached:
                self._finish(state, goal)
        return dict(status="RECONCILED", phase=state["phase"])

    def status(self):
        state = self.store.get("state", "current")
        return dict(
            status="STATUS",
            **(state or {"phase": "UNINITIALIZED"}),
            goals=self.store.all("goal"),
            plans=self.store.all("plan"),
            pending_commissions=[
                x["trade_id"]
                for x in self.store.all("fill")
                if x.get("commission_pending")
            ],
            open_capabilities=[
                "LIVE_BID_ASK_TIMESTAMP",
                "INSTRUCTION_ORDER_LINK",
                "ACCOUNT_ID_IN_CONNECTOR",
            ],
        )


def qty_as_strings(values):
    return {s: str(values[s]) for s in SYMBOLS}
