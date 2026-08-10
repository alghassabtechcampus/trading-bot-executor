"""Pure, Decimal-only execution cost calculations for V2.

``spread_bps`` represents the full bid/ask spread, so one half is applied on
each side of the mid/reference price. Slippage bps are always adverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import FinancialAssumptions


BPS_DENOMINATOR = Decimal("10000")
TWO = Decimal("2")


@dataclass(frozen=True, slots=True)
class CostedPrice:
    reference_price: Decimal
    price_after_spread: Decimal
    fill_price: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal


def _validate_inputs(reference_price: Decimal, quantity: Decimal) -> None:
    for name, value in (("reference_price", reference_price), ("quantity", quantity)):
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError(f"{name} must be a positive finite Decimal")


def buy_costed_price(
    reference_price: Decimal,
    quantity: Decimal,
    assumptions: FinancialAssumptions,
) -> CostedPrice:
    _validate_inputs(reference_price, quantity)
    half_spread_rate = assumptions.spread_bps / (BPS_DENOMINATOR * TWO)
    slippage_rate = assumptions.entry_slippage_bps / BPS_DENOMINATOR
    price_after_spread = reference_price * (Decimal(1) + half_spread_rate)
    fill_price = price_after_spread * (Decimal(1) + slippage_rate)
    return CostedPrice(
        reference_price=reference_price,
        price_after_spread=price_after_spread,
        fill_price=fill_price,
        spread_cost=(price_after_spread - reference_price) * quantity,
        slippage_cost=(fill_price - price_after_spread) * quantity,
    )


def sell_costed_price(
    reference_price: Decimal,
    quantity: Decimal,
    assumptions: FinancialAssumptions,
    *,
    slippage_bps: Decimal | None = None,
) -> CostedPrice:
    _validate_inputs(reference_price, quantity)
    selected_slippage_bps = (
        assumptions.exit_slippage_bps if slippage_bps is None else slippage_bps
    )
    if (
        not isinstance(selected_slippage_bps, Decimal)
        or not selected_slippage_bps.is_finite()
        or selected_slippage_bps < 0
        or selected_slippage_bps > BPS_DENOMINATOR
    ):
        raise ValueError("slippage_bps must be a finite Decimal in [0, 10000]")
    half_spread_rate = assumptions.spread_bps / (BPS_DENOMINATOR * TWO)
    slippage_rate = selected_slippage_bps / BPS_DENOMINATOR
    price_after_spread = reference_price * (Decimal(1) - half_spread_rate)
    fill_price = price_after_spread * (Decimal(1) - slippage_rate)
    return CostedPrice(
        reference_price=reference_price,
        price_after_spread=price_after_spread,
        fill_price=fill_price,
        spread_cost=(reference_price - price_after_spread) * quantity,
        slippage_cost=(price_after_spread - fill_price) * quantity,
    )
