"""Selectable month-projection methods ("tools").

Each method turns the complete-day usage series of a billing window into a
daily pace (kWh/day); the projection is actuals so far plus pace applied to
the remaining days (the linear-trend method instead extends its fitted line).
A custom admin formula can also define the pace, evaluated by a small safe
arithmetic evaluator (numbers, the documented variables, +, -, *, /, //, %,
**, and min/max/abs/round calls only).
"""
import ast
import math
import operator
from datetime import timedelta, timezone
from statistics import mean, median

SRI_LANKA_TIME = timezone(timedelta(hours=5, minutes=30), name="Asia/Colombo")

METHODS = [
    {
        "id": "linear_trend",
        "label": "Trend (gradient)",
        "description": "Extends the fitted usage slope; reacts to rising or falling use.",
    },
    {
        "id": "period_average",
        "label": "Period average",
        "description": "Average pace across all complete days so far.",
    },
    {
        "id": "trailing_7d",
        "label": "Last 7 days",
        "description": "Average pace of the most recent 7 complete days.",
    },
    {
        "id": "trailing_14d",
        "label": "Last 14 days",
        "description": "Average pace of the most recent 14 complete days.",
    },
    {
        "id": "weighted_recent",
        "label": "Weighted recent",
        "description": "Recent days count more than older days.",
    },
    {
        "id": "median",
        "label": "Median day",
        "description": "Middle daily value; ignores spike days.",
    },
    {
        "id": "exp_smoothing",
        "label": "Smoothed pace",
        "description": "Exponential smoothing (alpha 0.3) favouring recent days.",
    },
    {
        "id": "best_week",
        "label": "Best week pace",
        "description": "Highest 7-day average so far (upper reference).",
    },
    {
        "id": "low_week",
        "label": "Low week pace",
        "description": "Lowest 7-day average so far (lower reference).",
    },
    {
        "id": "last_period",
        "label": "Previous period",
        "description": "Same-length window just before this one (seasonal reference).",
    },
    {
        "id": "custom",
        "label": "Custom formula",
        "description": "Admin-defined expression for the daily pace.",
    },
]

METHOD_IDS = [method["id"] for method in METHODS]
DEFAULT_METHOD = "linear_trend"

CUSTOM_VARIABLES = frozenset(
    {
        "units_so_far",
        "measured_days",
        "remaining_days",
        "total_days",
        "avg_daily",
        "avg_7d",
        "avg_14d",
        "median_daily",
        "trend_slope",
        "trend_intercept",
        "last_daily",
    }
)
CUSTOM_FUNCTIONS = {"min": min, "max": max, "abs": abs, "round": round}

_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_MAX_FORMULA_LENGTH = 300
_MAX_FORMULA_NODES = 100
_MAX_ABSOLUTE_RESULT = 1e9


class ProjectionMethodError(ValueError):
    """Raised when a projection method or custom formula cannot be applied."""


def method_label(method_id: str) -> str:
    for method in METHODS:
        if method["id"] == method_id:
            return method["label"]
    return method_id


def pace_trailing(daily: list, window: int) -> float:
    recent = daily[-window:] if window < len(daily) else list(daily)
    return float(mean(recent)) if recent else 0.0


def pace_weighted_recent(daily: list) -> float:
    if not daily:
        return 0.0
    weights = range(1, len(daily) + 1)
    return float(sum(v * w for v, w in zip(daily, weights)) / sum(weights))


def pace_median(daily: list) -> float:
    return float(median(daily)) if daily else 0.0


def pace_exp_smoothing(daily: list, alpha: float = 0.3) -> float:
    if not daily:
        return 0.0
    level = float(daily[0])
    for value in daily[1:]:
        level = alpha * float(value) + (1 - alpha) * level
    return level


def _rolling_means(daily: list, window: int) -> list:
    window = min(window, len(daily))
    if window <= 0:
        return []
    return [float(mean(daily[i : i + window])) for i in range(len(daily) - window + 1)]


def pace_best_week(daily: list) -> float:
    windows = _rolling_means(daily, 7)
    return max(windows) if windows else 0.0


def pace_low_week(daily: list) -> float:
    windows = _rolling_means(daily, 7)
    return min(windows) if windows else 0.0


def pace_last_period(by_date: dict, meter_id: str, start, total_days: int, readings: list) -> float | None:
    """Pace of the same-length window just before `start`; None if unusable."""
    if total_days <= 0:
        return None
    window_start = start - timedelta(days=total_days)
    values = [
        float((by_date.get((window_start + timedelta(days=offset)).isoformat()) or {}).get(meter_id) or 0.0)
        for offset in range(total_days)
    ]
    if sum(values) <= 0:
        return None
    window_end = start - timedelta(days=1)
    for doc in readings:
        if doc.get("meter_id") != meter_id:
            continue
        recorded = doc.get("recorded_at")
        if recorded is None:
            continue
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        if window_start <= recorded.astimezone(SRI_LANKA_TIME).date() <= window_end:
            return float(mean(values))
    return None


def _parse_projection_formula(expression: str) -> ast.Expression:
    normalized = expression.strip()
    if not normalized:
        raise ProjectionMethodError("Enter a custom projection formula.")
    if len(normalized) > _MAX_FORMULA_LENGTH:
        raise ProjectionMethodError(
            f"Formula must be {_MAX_FORMULA_LENGTH} characters or fewer."
        )
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise ProjectionMethodError("Formula syntax is invalid.") from exc

    nodes = list(ast.walk(tree))
    if len(nodes) > _MAX_FORMULA_NODES:
        raise ProjectionMethodError("Formula is too complex.")

    call_funcs = {
        node.func.id
        for node in nodes
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    for node in nodes:
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in CUSTOM_FUNCTIONS:
                raise ProjectionMethodError("Only min, max, abs and round calls are allowed.")
            if node.keywords or getattr(node, "starargs", None) or getattr(node, "kwargs", None):
                raise ProjectionMethodError("Formula calls take plain values only.")
            continue
        if isinstance(node, ast.Name) and node.id not in CUSTOM_VARIABLES and node.id not in call_funcs:
            raise ProjectionMethodError(f"Unknown formula variable: {node.id}")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ProjectionMethodError("Only numeric constants are allowed.")
        if isinstance(node, ast.BinOp) and type(node.op) not in _BINARY_OPERATORS:
            raise ProjectionMethodError("Only arithmetic operators are allowed.")
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Pow)
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, (int, float))
            and abs(node.right.value) > 10
        ):
            raise ProjectionMethodError("Formula exponents must be between -10 and 10.")
        if isinstance(node, ast.UnaryOp) and type(node.op) not in _UNARY_OPERATORS:
            raise ProjectionMethodError("Only unary plus and minus are allowed.")
        if not isinstance(
            node,
            (
                ast.Expression,
                ast.Load,
                ast.BinOp,
                ast.UnaryOp,
                ast.Constant,
                ast.Name,
                ast.Call,
                ast.Add,
                ast.Sub,
                ast.Mult,
                ast.Div,
                ast.FloorDiv,
                ast.Mod,
                ast.Pow,
                ast.UAdd,
                ast.USub,
            ),
        ):
            raise ProjectionMethodError(
                "Formula may contain variables, numbers, parentheses, arithmetic, and min/max/abs/round only."
            )
    return tree


def validate_projection_formula(expression: str) -> str:
    """Validate and return the trimmed pace expression."""
    _parse_projection_formula(expression)
    return expression.strip()


def _evaluate_node(node: ast.AST, variables: dict) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body, variables)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ProjectionMethodError(f"Unknown formula variable: {node.id}")
        return float(variables[node.id])
    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPERATORS[type(node.op)](_evaluate_node(node.operand, variables))
    if isinstance(node, ast.Call):
        func = CUSTOM_FUNCTIONS[node.func.id]
        args = [_evaluate_node(arg, variables) for arg in node.args]
        if node.func.id == "round" and len(args) == 2:
            if not float(args[1]).is_integer():
                raise ProjectionMethodError("Formula round() needs a whole number of digits.")
            args[1] = int(args[1])
        try:
            return float(func(*args))
        except (ArithmeticError, OverflowError, TypeError, ValueError) as exc:
            raise ProjectionMethodError(
                "Formula cannot be evaluated with the current usage data."
            ) from exc
    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, variables)
        right = _evaluate_node(node.right, variables)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ProjectionMethodError("Formula exponents must be between -10 and 10.")
        try:
            result = _BINARY_OPERATORS[type(node.op)](left, right)
        except (ArithmeticError, OverflowError) as exc:
            raise ProjectionMethodError(
                "Formula cannot be evaluated with the current usage data."
            ) from exc
        if isinstance(result, complex) or not math.isfinite(result) or abs(result) > _MAX_ABSOLUTE_RESULT:
            raise ProjectionMethodError("Formula result is outside the supported range.")
        return float(result)
    raise ProjectionMethodError("Formula contains an unsupported expression.")


def evaluate_projection_formula(expression: str, variables: dict) -> float:
    """Evaluate a validated pace expression; negative paces floor at zero."""
    tree = _parse_projection_formula(expression)
    missing = [name for name in CUSTOM_VARIABLES if name not in variables]
    if missing:
        raise ProjectionMethodError(f"Formula is missing values: {', '.join(sorted(missing))}.")
    return max(0.0, _evaluate_node(tree, variables))
