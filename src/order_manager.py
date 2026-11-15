"""Live channel-bracket orders for the SkyQ RANGE strategy.

The entry is a market order.  A GTC stop and a GTC limit target are children of
the parent and share an OCA group, so a fill in either cancels the other.  The
live default is the SPY ETF book: 1,000 shares, a 100-tick stop beyond the
channel and a target at 20% of the channel range.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from ib_async import IB, Contract, LimitOrder, MarketOrder, Order, StopOrder, Trade

from config.settings import Instrument, NY_TZ
from src.strategy_logic import EntryPlan, Side

log = logging.getLogger(__name__)

_ACTIVE_STATUSES = frozenset({"PendingSubmit", "PreSubmitted", "Submitted"})
_DEAD_STATUSES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick must be positive")
    return round(float(price) / float(tick)) * float(tick)


@dataclass
class Bracket:
    side: Side
    quantity: int
    entry_price: float
    stop_price: float
    target_price: float
    parent: Order
    take_profit: Order
    stop_loss: Order
    oca_group: str


@dataclass
class PositionSnapshot:
    parent_filled: bool
    position_open: bool
    flat: bool
    exit_reason: str | None
    entry_price: float
    exit_price: float | None
    pnl: float
    remaining_quantity: int
    orphaned: bool = False


def build_bracket(
    profile: Instrument,
    side: Side,
    quantity: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    next_id: Callable[[], int],
) -> Bracket:
    """Build the parent/child chain with an OCA stop/target pair."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    tick = profile.tick_size
    entry_price = round_to_tick(entry_price, tick)
    stop_price = round_to_tick(stop_price, tick)
    target_price = round_to_tick(target_price, tick)

    if side is Side.LONG:
        if not stop_price < entry_price < target_price:
            raise ValueError("long bracket needs stop < entry < target")
    else:
        if not target_price < entry_price < stop_price:
            raise ValueError("short bracket needs target < entry < stop")

    parent = MarketOrder(side.action, quantity, orderId=next_id(), transmit=False, tif="DAY")
    take_profit = LimitOrder(
        side.exit_action,
        quantity,
        target_price,
        orderId=next_id(),
        transmit=False,
        parentId=parent.orderId,
        tif="GTC",
    )
    stop_loss = StopOrder(
        side.exit_action,
        quantity,
        stop_price,
        orderId=next_id(),
        transmit=True,
        parentId=parent.orderId,
        tif="GTC",
    )
    oca_group = f"SKYQ-RANGE-{parent.orderId}"
    for order in (take_profit, stop_loss):
        order.ocaGroup = oca_group
        order.ocaType = 1
    return Bracket(
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        parent=parent,
        take_profit=take_profit,
        stop_loss=stop_loss,
        oca_group=oca_group,
    )


class LivePosition:
    """Track one live bracket and derive its state from broker order statuses."""

    def __init__(
        self,
        ib: IB,
        contract: Contract,
        profile: Instrument,
        bracket: Bracket,
        plan: EntryPlan,
        next_id: Callable[[], int],
    ) -> None:
        self.ib = ib
        self.contract = contract
        self.profile = profile
        self.bracket = bracket
        self.plan = plan
        self.next_id = next_id
        self.entry_time = datetime.now(tz=NY_TZ)
        self.parent_trade: Trade | None = None
        self.tp_trade: Trade | None = None
        self.sl_trade: Trade | None = None
        self.flatten_trade: Trade | None = None
        self.flatten_reason: str | None = None

    def place(self) -> None:
        self.parent_trade = self.ib.placeOrder(self.contract, self.bracket.parent)
        self.tp_trade = self.ib.placeOrder(self.contract, self.bracket.take_profit)
        self.sl_trade = self.ib.placeOrder(self.contract, self.bracket.stop_loss)
        log.info(
            "Placed %s %s x%d: entry~%.2f stop=%.2f target=%.2f",
            self.profile.symbol,
            self.bracket.side.value,
            self.bracket.quantity,
            self.bracket.entry_price,
            self.bracket.stop_price,
            self.bracket.target_price,
        )

    @staticmethod
    def _status(trade: Trade | None) -> str | None:
        return trade.orderStatus.status if trade is not None else None

    def snapshot(self) -> PositionSnapshot:
        parent_status = self._status(self.parent_trade)
        tp_status = self._status(self.tp_trade)
        sl_status = self._status(self.sl_trade)
        flatten_status = self._status(self.flatten_trade)

        parent_filled = parent_status == "Filled"
        tp_filled = tp_status == "Filled"
        sl_filled = sl_status == "Filled"
        flatten_filled = flatten_status == "Filled"

        entry_price = self.bracket.entry_price
        if self.parent_trade and self.parent_trade.orderStatus.avgFillPrice:
            entry_price = float(self.parent_trade.orderStatus.avgFillPrice)

        exit_reason: str | None = None
        exit_price: float | None = None
        if tp_filled:
            exit_reason = "TARGET"
            exit_price = float(self.tp_trade.orderStatus.avgFillPrice or self.bracket.target_price)
        elif sl_filled:
            exit_reason = "STOP"
            exit_price = float(self.sl_trade.orderStatus.avgFillPrice or self.bracket.stop_price)
        elif flatten_filled:
            exit_reason = self.flatten_reason or "FLATTEN"
            exit_price = float(self.flatten_trade.orderStatus.avgFillPrice or self.bracket.entry_price)
        elif parent_status in _DEAD_STATUSES:
            exit_reason = "REJECTED"
            exit_price = None

        flat = bool(exit_reason)
        position_open = parent_filled and not flat
        tp_dead = tp_status in _DEAD_STATUSES
        orphaned = bool(parent_filled and not flat and tp_dead and sl_status in _DEAD_STATUSES)
        pnl = 0.0
        if exit_price is not None:
            pnl = (
                (exit_price - entry_price)
                * self.bracket.side.direction
                * self.profile.point_value
                * self.bracket.quantity
            )
        return PositionSnapshot(
            parent_filled=parent_filled,
            position_open=position_open,
            flat=flat,
            exit_reason=exit_reason,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            remaining_quantity=0 if flat else self.bracket.quantity,
            orphaned=orphaned,
        )

    def cancel_order(self, trade: Trade | None) -> None:
        if trade is None:
            return
        if trade.orderStatus.status in _ACTIVE_STATUSES:
            self.ib.cancelOrder(trade.order)

    def cancel_children(self) -> None:
        self.cancel_order(self.tp_trade)
        self.cancel_order(self.sl_trade)

    def _children_dead(self) -> bool:
        for trade in (self.tp_trade, self.sl_trade):
            if trade is None:
                continue
            status = self._status(trade)
            if status is None or status == "Filled" or status in _DEAD_STATUSES:
                continue
            return False
        return True

    def _wait_children_dead(self, timeout_seconds: float) -> bool:
        deadline = datetime.now().timestamp() + timeout_seconds
        while datetime.now().timestamp() < deadline:
            if self._children_dead():
                return True
            self.ib.sleep(0.15)
        return self._children_dead()

    def flatten(self, reason: str = "FLATTEN", timeout_seconds: float = 5.0) -> Trade | None:
        """Cancel children and send a market exit after they are confirmed dead."""
        snapshot = self.snapshot()
        if snapshot.flat or not snapshot.parent_filled:
            self.cancel_order(self.parent_trade)
            self.cancel_children()
            return None
        if self.flatten_trade is not None and self._status(self.flatten_trade) in _ACTIVE_STATUSES:
            return self.flatten_trade
        self.cancel_children()
        if not self._wait_children_dead(timeout_seconds):
            log.error("%s children did not cancel; refusing a second exit", self.profile.symbol)
            return None
        if self.snapshot().flat:
            return None
        self.flatten_reason = reason
        self.flatten_trade = self.ib.placeOrder(
            self.contract,
            MarketOrder(
                self.bracket.side.exit_action,
                self.bracket.quantity,
                orderId=self.next_id(),
                transmit=True,
                tif="DAY",
            ),
        )
        log.warning(
            "%s flattening %s x%d at market (%s)",
            self.profile.symbol,
            self.bracket.side.value,
            self.bracket.quantity,
            reason,
        )
        return self.flatten_trade

    def wait_flat(self, timeout_seconds: float = 10.0) -> PositionSnapshot:
        deadline = datetime.now().timestamp() + timeout_seconds
        while datetime.now().timestamp() < deadline:
            snapshot = self.snapshot()
            if snapshot.flat:
                return snapshot
            self.ib.sleep(0.25)
        return self.snapshot()


def position_from_plan(
    ib: IB,
    contract: Contract,
    profile: Instrument,
    plan: EntryPlan,
    next_id: Callable[[], int] | None = None,
) -> LivePosition:
    """Build and place a bracket for ``plan``."""
    if next_id is None:
        next_id = lambda: ib.client.getReqId()
    bracket = build_bracket(
        profile,
        plan.side,
        plan.quantity,
        plan.entry_price,
        plan.stop_price,
        plan.target_price,
        next_id,
    )
    position = LivePosition(ib, contract, profile, bracket, plan, next_id)
    position.place()
    return position
