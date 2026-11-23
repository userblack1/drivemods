from dataclasses import asdict, dataclass
from enum import StrEnum
import json


class State(StrEnum):
    UPLOADED = "uploaded"
    QUOTED = "quoted"
    INVOICE = "invoice"
    CHECKOUT = "checkout"
    PAID = "paid"
    BUYING = "buying"
    WAITING = "waiting_supplier"
    REVIEW = "review"
    READY = "ready"
    SENDING = "sending"
    DELIVERED = "delivered"
    FAILED = "failed"
    REFUNDING = "refunding"
    REFUNDED = "refunded"
    OPERATOR_PAYMENT = "operator_payment"
    OPERATOR_PAID = "operator_paid"
    OPERATOR_FILE = "operator_file"
    CANCELLED = "cancelled"


class OrderError(ValueError):
    pass


class DeliveryRejected(OrderError):
    pass


def is_demo_order(order: dict) -> bool:
    return order.get("workflow") == "demo" or str(order.get("charge_id") or "").startswith("demo-")


@dataclass(frozen=True)
class Offer:
    plan: str
    supplier_plan_id: str
    cost_cents: int
    retail_cents: int
    stars: int


@dataclass(frozen=True)
class Quote:
    reference: str
    expires_at: float
    offers: tuple[Offer, ...]

    def dumps(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def loads(cls, value: str) -> "Quote":
        data = json.loads(value)
        return cls(data["reference"], data["expires_at"], tuple(Offer(**o) for o in data["offers"]))


@dataclass(frozen=True)
class SupplierOrder:
    reference: str
    status: str  # pending, action_required, ready, failed
    action_url: str | None = None

