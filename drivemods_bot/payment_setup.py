"""Payment readiness and an offline USD/KGS calculation; never accepts money."""
import argparse
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json

from .config import Settings


def amount_kgs_minor(usd_cents: int, rate: str) -> int:
    """Convert using an explicitly supplied KGS-per-USD rate, in tyiyn."""
    if type(usd_cents) is not int or not 0 < usd_cents <= 999_999_999_999:
        raise ValueError("USD amount must be positive integer cents")
    if not isinstance(rate, str):
        raise ValueError("Курс задаётся десятичной строкой")
    try:
        value = Decimal(rate)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Укажите положительный курс KGS за 1 USD") from exc
    if not value.is_finite() or not 0 < value <= Decimal("999999999999") or value.as_tuple().exponent < -6:
        raise ValueError("Курс должен быть положительным, не более 6 знаков после запятой")
    amount = Decimal(usd_cents) * value
    if amount > Decimal("999999999999"):
        raise ValueError("Сумма превышает допустимый размер расчёта")
    result = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if result <= 0:
        raise ValueError("Сумма в сомах после округления должна быть положительной")
    return result


def readiness(settings: Settings, rate: str | None = None) -> dict:
    plans = {}
    for plan in ("full", "eco"):
        cost = settings.cost(plan)
        retail = cost + settings.markup
        plans[plan] = dict(retail_usd_cents=retail, supplier_usd_cents=cost,
                           gross_margin_usd_cents=retail - cost,
                           preview_kgs_minor=amount_kgs_minor(retail, rate) if rate is not None else None)
    return dict(plans=plans, kgs_per_usd=rate, bank_acceptance="not_connected",
                banks=["MBANK", "O!Bank"], preview_only=True,
                receipt_images_confirm_payment=False,
                required=["Официальный доступ к статусам операций банка или эквайринга",
                          "Документация интеграции и проверка подлинности уведомлений",
                          "Реквизиты получателя и QR/платёжная ссылка от банка",
                          "Правило курса для фактического счёта в сомах",
                          "Проверенная покупка и выдача настоящей лицензии DriveMods"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Подготовка оплаты: только расчёт, без QR-счёта и банковских операций")
    parser.add_argument("--rate", help="Согласованный курс KGS за 1 USD; без него сумма в сомах не рассчитывается")
    args = parser.parse_args()
    print(json.dumps(readiness(Settings.load(), args.rate), ensure_ascii=False, indent=2))
