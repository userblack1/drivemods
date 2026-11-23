"""Owner-approved limits for DriveMods license purchases, in USD cents."""
from .models import OrderError


def purchase_limit(plan: str) -> int:
    try:
        return {"full": 9000, "eco": 6000}[plan]
    except (KeyError, TypeError) as exc:
        raise OrderError("Неизвестный пакет лицензии.") from exc


def check_purchase_budget(plan: str, cost: int) -> None:
    limit = purchase_limit(plan)
    if type(cost) is not int or not 0 < cost <= limit:
        raise OrderError(f"Закупка {plan.upper()} превышает разрешённый предел {limit // 100} USD или содержит неверную сумму.")
