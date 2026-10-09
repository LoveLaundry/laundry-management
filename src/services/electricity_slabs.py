import math


class ElectricitySlabError(ValueError):
    """Raised when the unit-slab tariff is invalid or cannot be applied."""


def _slab_number(raw, label: str, *, allow_none: bool, positive: bool):
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if allow_none:
            return None
        raise ElectricitySlabError(f"{label} is required.")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise ElectricitySlabError(f"{label} must be a number.") from None
    if not math.isfinite(number):
        raise ElectricitySlabError(f"{label} must be a finite number.")
    if positive and number <= 0:
        raise ElectricitySlabError(f"{label} must be greater than 0.")
    if not positive and number < 0:
        raise ElectricitySlabError(f"{label} cannot be negative.")
    return number


def normalize_unit_slabs(value) -> list:
    """Validate raw settings data into an ordered slab table.

    Returns a list of {"up_to": float | None, "rate_lkr": float,
    "fixed_charge_lkr": float}. Only the last slab may leave ``up_to``
    empty, meaning it covers every unit above the previous slab.
    """
    if not isinstance(value, list) or not value:
        raise ElectricitySlabError(
            "Configure at least one tariff slab before calculating LKR amounts."
        )
    slabs = []
    for index, raw in enumerate(value):
        label = f"Slab {index + 1}"
        if not isinstance(raw, dict):
            raise ElectricitySlabError(f"{label} is invalid.")
        up_to = _slab_number(
            raw.get("up_to"), f"{label}: 'up to' units", allow_none=True, positive=True
        )
        rate = _slab_number(
            raw.get("rate_lkr"), f"{label}: rate", allow_none=False, positive=False
        )
        fixed = _slab_number(
            raw.get("fixed_charge_lkr"),
            f"{label}: fixed charge",
            allow_none=False,
            positive=False,
        )
        slabs.append(
            {"up_to": up_to, "rate_lkr": rate, "fixed_charge_lkr": fixed}
        )
    for index, slab in enumerate(slabs):
        if slab["up_to"] is None and index != len(slabs) - 1:
            raise ElectricitySlabError(
                "Only the last slab may have no upper limit."
            )
    tops = [slab["up_to"] for slab in slabs if slab["up_to"] is not None]
    for lower, upper in zip(tops, tops[1:]):
        if upper <= lower:
            raise ElectricitySlabError(
                "Slab limits must increase from the first row to the last."
            )
    return slabs


def calculate_slab_cost(units: float, slabs: list, tax_rate: float) -> dict:
    """Apply the slab table to one meter's monthly units (CEB-style breakdown).

    Each unit block is charged at its slab rate, and the fixed charge of the
    slab that the month's total units fall into is added on top. Tax is
    applied to the subtotal.
    """
    total = max(float(units or 0.0), 0.0)
    remaining = total
    lower = 0.0
    energy = 0.0
    fixed = slabs[-1]["fixed_charge_lkr"]
    applied_index = len(slabs) - 1
    fixed_locked = False
    breakdown = []
    for index, slab in enumerate(slabs):
        top = slab["up_to"]
        cap = math.inf if top is None else top
        portion = min(remaining, max(cap - lower, 0.0))
        charge = portion * slab["rate_lkr"]
        energy += charge
        breakdown.append(
            {
                "from_units": lower,
                "up_to": top,
                "rate_lkr": slab["rate_lkr"],
                "units": round(portion, 3),
                "charge_lkr": round(charge, 2),
            }
        )
        if not fixed_locked and total <= cap:
            fixed = slab["fixed_charge_lkr"]
            applied_index = index
            fixed_locked = True
        remaining -= portion
        lower = cap
    subtotal = energy + fixed
    amount = subtotal * (1 + tax_rate)
    return {
        "units": round(total, 3),
        "energy_lkr": round(energy, 2),
        "fixed_charge_lkr": round(fixed, 2),
        "subtotal_lkr": round(subtotal, 2),
        "tax_rate": tax_rate,
        "amount_lkr": round(amount, 2),
        "applied_slab": applied_index,
        "breakdown": breakdown,
    }
