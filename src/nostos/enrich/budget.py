from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from nostos.enrich.base import CostModel, Enricher
from nostos.model import Listing

ConfirmBudget = Callable[[Decimal], bool]


class BudgetError(RuntimeError):
    """Base budget failure."""


class UncappedSpendError(BudgetError):
    """Raised when a paid enricher runs without an explicit cap."""


class BudgetDeclinedError(BudgetError):
    """Raised when a paid estimate was not approved."""


class BudgetExceededError(BudgetError):
    """Raised when a paid run would exceed the spend cap."""


@dataclass
class BudgetState:
    cap: Decimal | None
    confirm: ConfirmBudget | None = None
    spent: Decimal = Decimal("0")
    confirmed: bool = False


def estimate_cost(enricher: Enricher, listing: Listing, context: object) -> Decimal:
    if enricher.cost is CostModel.FREE:
        return Decimal("0")
    estimate = enricher.estimate_cost(listing, context)
    if estimate < 0:
        raise ValueError("Enricher cost estimate must be non-negative")
    return estimate


def confirm_cost(state: BudgetState, estimate: Decimal) -> None:
    if estimate == 0 or state.confirmed:
        return
    if state.confirm is None:
        raise BudgetDeclinedError("Paid enricher requires explicit spend confirmation")
    if not state.confirm(estimate):
        raise BudgetDeclinedError("Estimated spend was not approved")
    state.confirmed = True


def cap_cost(state: BudgetState, estimate: Decimal) -> None:
    if state.cap is None:
        raise UncappedSpendError("Paid enricher cannot run without a spend cap")
    projected = state.spent + estimate
    if projected > state.cap:
        raise BudgetExceededError(
            f"Estimated spend {projected} exceeds cap {state.cap}",
        )
    state.spent = projected


def estimate_confirm_cap(
    enricher: Enricher,
    listing: Listing,
    context: object,
    state: BudgetState | None,
) -> Decimal:
    if enricher.cost is CostModel.FREE:
        return Decimal("0")
    if state is None or state.cap is None:
        raise UncappedSpendError("Paid enricher cannot run without a spend cap")

    estimate = estimate_cost(enricher, listing, context)
    confirm_cost(state, estimate)
    cap_cost(state, estimate)
    return estimate
