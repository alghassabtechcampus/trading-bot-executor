"""Explicit, unit-safe configuration for Backtest Engine V2.

Decimal rates use 0.001 for 0.1%. Basis-point fields use 10 for 10 bps.
No fee, spread, or slippage assumption is supplied implicitly by this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


class ConfigurationError(ValueError):
    """Raised when a V2 configuration is internally inconsistent."""


class ExecutionProfile(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    BASE = "BASE"
    OPTIMISTIC = "OPTIMISTIC"
    CUSTOM = "CUSTOM"


class IntrabarPolicy(str, Enum):
    STOP_FIRST = "STOP_FIRST"
    TAKE_PROFIT_FIRST = "TAKE_PROFIT_FIRST"


def _require_decimal(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise ConfigurationError(f"{name} must be Decimal, not {type(value).__name__}")
    if not value.is_finite():
        raise ConfigurationError(f"{name} must be finite")


def _validate_rate(name: str, value: Decimal) -> None:
    _require_decimal(name, value)
    if value < 0 or value >= 1:
        raise ConfigurationError(f"{name} must be a decimal rate in [0, 1)")


def _validate_bps(name: str, value: Decimal) -> None:
    _require_decimal(name, value)
    if value < 0 or value > Decimal("10000"):
        raise ConfigurationError(f"{name} must be basis points in [0, 10000]")


@dataclass(frozen=True, slots=True)
class FinancialAssumptions:
    """All execution costs, with units encoded in field names.

    Fee fields are decimal rates. Spread and slippage fields are basis points.
    Values must be provided explicitly by the caller; there are no defaults.
    """

    entry_fee_rate: Decimal
    exit_fee_rate: Decimal
    entry_slippage_bps: Decimal
    exit_slippage_bps: Decimal
    spread_bps: Decimal
    stop_slippage_bps: Decimal
    take_profit_slippage_bps: Decimal

    def __post_init__(self) -> None:
        _validate_rate("entry_fee_rate", self.entry_fee_rate)
        _validate_rate("exit_fee_rate", self.exit_fee_rate)
        _validate_bps("entry_slippage_bps", self.entry_slippage_bps)
        _validate_bps("exit_slippage_bps", self.exit_slippage_bps)
        _validate_bps("spread_bps", self.spread_bps)
        _validate_bps("stop_slippage_bps", self.stop_slippage_bps)
        _validate_bps("take_profit_slippage_bps", self.take_profit_slippage_bps)


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Phase-1 run configuration with explicit financial assumptions."""

    execution_profile: ExecutionProfile
    financial_assumptions: FinancialAssumptions
    initial_capital: Decimal
    base_currency: str
    max_concurrent_positions: int
    intrabar_policy: IntrabarPolicy = IntrabarPolicy.STOP_FIRST

    def __post_init__(self) -> None:
        if not isinstance(self.execution_profile, ExecutionProfile):
            raise ConfigurationError("execution_profile must be an ExecutionProfile")
        if not isinstance(self.intrabar_policy, IntrabarPolicy):
            raise ConfigurationError("intrabar_policy must be an IntrabarPolicy")
        _require_decimal("initial_capital", self.initial_capital)
        if self.initial_capital <= 0:
            raise ConfigurationError("initial_capital must be positive")
        if not self.base_currency or not self.base_currency.strip():
            raise ConfigurationError("base_currency must be non-empty")
        if isinstance(self.max_concurrent_positions, bool) or self.max_concurrent_positions < 1:
            raise ConfigurationError("max_concurrent_positions must be a positive integer")

    def manifest_values(self) -> dict[str, Any]:
        """Return JSON-friendly values, preserving explicit units in keys."""

        values = asdict(self)
        values["execution_profile"] = self.execution_profile.value
        values["intrabar_policy"] = self.intrabar_policy.value
        values["initial_capital"] = str(self.initial_capital)
        values["financial_assumptions"] = {
            key: str(value) for key, value in values["financial_assumptions"].items()
        }
        return values
