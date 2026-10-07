import ast
import math
import operator
from typing import Mapping


VARIABLE_NAMES = frozenset(
    {
        "meter_1_current",
        "meter_1_previous",
        "meter_1_units",
        "meter_2_current",
        "meter_2_previous",
        "meter_2_units",
        "unit_rate_lkr",
        "fixed_charge_lkr",
        "tax_rate",
    }
)

DEFAULT_ELECTRICITY_COST_FORMULA = (
    "((meter_1_units + meter_2_units) * unit_rate_lkr + fixed_charge_lkr) "
    "* (1 + tax_rate)"
)

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
_MAX_ABSOLUTE_RESULT = 1e15


class ElectricityFormulaError(ValueError):
    """Raised when an electricity-cost formula is invalid or cannot be evaluated."""


def _parse_formula(expression: str) -> ast.Expression:
    normalized = expression.strip()
    if not normalized:
        raise ElectricityFormulaError("Enter an electricity-cost formula.")
    if len(normalized) > _MAX_FORMULA_LENGTH:
        raise ElectricityFormulaError(
            f"Formula must be {_MAX_FORMULA_LENGTH} characters or fewer."
        )

    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise ElectricityFormulaError("Formula syntax is invalid.") from exc

    nodes = list(ast.walk(tree))
    if len(nodes) > _MAX_FORMULA_NODES:
        raise ElectricityFormulaError("Formula is too complex.")

    for node in nodes:
        if isinstance(node, ast.Name) and node.id not in VARIABLE_NAMES:
            raise ElectricityFormulaError(f"Unknown formula variable: {node.id}")
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ElectricityFormulaError("Only numeric constants are allowed.")
        if isinstance(node, ast.BinOp) and type(node.op) not in _BINARY_OPERATORS:
            raise ElectricityFormulaError("Only arithmetic operators are allowed.")
        if isinstance(node, ast.UnaryOp) and type(node.op) not in _UNARY_OPERATORS:
            raise ElectricityFormulaError("Only unary plus and minus are allowed.")
        if not isinstance(
            node,
            (
                ast.Expression,
                ast.Load,
                ast.BinOp,
                ast.UnaryOp,
                ast.Constant,
                ast.Name,
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
            raise ElectricityFormulaError(
                "Formula may contain variables, numbers, parentheses, and arithmetic only."
            )

    return tree


def validate_electricity_formula(expression: str) -> str:
    """Validate and return the trimmed arithmetic expression."""
    _parse_formula(expression)
    return expression.strip()


def electricity_formula_variables(expression: str) -> frozenset[str]:
    """Return the allowed variable names referenced by a valid formula."""
    tree = _parse_formula(expression)
    return frozenset(
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    )


def _evaluate_node(node: ast.AST, variables: Mapping[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body, variables)
    if isinstance(node, ast.Constant):
        return _checked_result(float(node.value))
    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise ElectricityFormulaError(f"Missing formula value: {node.id}")
        return _checked_result(float(variables[node.id]))
    if isinstance(node, ast.UnaryOp):
        value = _evaluate_node(node.operand, variables)
        return _checked_result(_UNARY_OPERATORS[type(node.op)](value))
    if isinstance(node, ast.BinOp):
        left = _evaluate_node(node.left, variables)
        right = _evaluate_node(node.right, variables)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ElectricityFormulaError("Formula exponents must be between -10 and 10.")
        try:
            result = _BINARY_OPERATORS[type(node.op)](left, right)
        except (ArithmeticError, OverflowError) as exc:
            raise ElectricityFormulaError(
                "Formula cannot be evaluated with the current meter readings and rates."
            ) from exc
        return _checked_result(result)
    raise ElectricityFormulaError("Formula contains an unsupported expression.")


def _checked_result(value: float | complex) -> float:
    if isinstance(value, complex):
        raise ElectricityFormulaError("Formula must produce a real numeric result.")
    if not math.isfinite(value) or abs(value) > _MAX_ABSOLUTE_RESULT:
        raise ElectricityFormulaError("Formula result is outside the supported range.")
    return float(value)


def evaluate_electricity_formula(
    expression: str, variables: Mapping[str, float]
) -> float:
    tree = _parse_formula(expression)
    return _evaluate_node(tree, variables)
