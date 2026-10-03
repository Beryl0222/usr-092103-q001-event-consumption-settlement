"""金额工具：系统内一律使用整数「分」，杜绝浮点误差。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def yuan_to_cents(value: str | int | Decimal) -> int:
    """元（可带两位小数）转分。"""
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"非法金额：{value!r}") from exc
    if d != d.quantize(Decimal("0.01")):
        raise ValueError(f"金额最小单位为分：{value!r}")
    cents = int(d * 100)
    return cents


def cents_to_yuan(cents: int) -> str:
    """分转元字符串，例如 12345 -> '123.45'。"""
    sign, n = "-" if cents < 0 else "", abs(cents)
    return f"{sign}{n // 100}.{n % 100:02d}"


def require_nonnegative(cents: int, name: str = "金额") -> int:
    if cents < 0:
        raise ValueError(f"{name}不能为负")
    return cents


def require_positive(cents: int, name: str = "金额") -> int:
    if cents <= 0:
        raise ValueError(f"{name}必须为正")
    return cents
