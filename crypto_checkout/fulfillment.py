"""One purchase per confirmed invoice; store and Telegram are explicit adapters."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Protocol

from drivemods_bot.config import Settings
from drivemods_bot.supplier import validate_license
from .ledger import Ledger


@dataclass(frozen=True)
class StoreOrder:
    reference: str
    external_reference: str
    device_sha256: str
    plan: str
    currency: str
    total_cents: int
    status: str  # unpaid, paid, action_required, pending
    payment_reference: str = ''


class Store(Protocol):
    simulation: bool
    async def prepare(self, invoice: dict) -> StoreOrder: ...
    async def inspect(self, reference: str) -> StoreOrder: ...
    async def pay(self, reference: str, idempotency_key: str) -> StoreOrder: ...
    async def download(self, reference: str) -> bytes: ...


class Delivery(Protocol):
    simulation: bool
    async def send(self, invoice: dict, path: Path, filename: str) -> str: ...


class Engine:
    def __init__(self, ledger: Ledger, store: Store, delivery: Delivery, *, live_purchase_enabled=False):
        self.ledger, self.store, self.delivery = ledger, store, delivery
        self.live_purchase_enabled = live_purchase_enabled

    def _check_order(self, invoice, order):
        if (not order.reference or order.external_reference != invoice['id']
                or order.device_sha256 != invoice['source_sha'] or order.plan != invoice['plan']
                or order.currency != 'USD' or type(order.total_cents) is not int
                or order.total_cents != invoice['cost_cents']
                or order.total_cents > {'full':9000,'eco':6000}[invoice['plan']]
                or order.status not in {'unpaid','paid','action_required','pending'}):
            raise ValueError('Store order does not match device, plan, price or reference')
        if invoice['supplier_ref'] and invoice['supplier_ref'] != order.reference:
            raise ValueError('Store reference changed')
        if order.status == 'paid' and not order.payment_reference:
            raise ValueError('Paid status lacks a store payment reference')

    async def step(self, invoice_id):
        ledger = self.ledger
        invoice = ledger.get(invoice_id)
        if self.store.simulation != ledger.simulation or self.delivery.simulation != ledger.simulation:
            raise ValueError('Real and simulated components cannot be mixed')
        if not ledger.simulation and not self.live_purchase_enabled:
            return False
        state = invoice['state']
        if state == 'paid':
            if not ledger.transition(invoice_id,'paid','preparing'):
                return False
            try:
                source = Path(invoice['source_path'])
                if hashlib.sha256(source.read_bytes()).hexdigest() != invoice['source_sha']:
                    raise ValueError('Device ID changed')
                order = await self.store.prepare(invoice)
                self._check_order(invoice,order)
                ledger.transition(invoice_id,'preparing','prepared',supplier_ref=order.reference)
            except Exception as exc:
                ledger.transition(invoice_id,'preparing','purchase_review',error=type(exc).__name__)
            return True
        if state in {'prepared','waiting_supplier','action_required'}:
            # Reserve the external operation across concurrent workers before I/O.
            if not ledger.transition(invoice_id,state,'inspecting',inspection_origin=state):
                return False
            try:
                order = await self.store.inspect(invoice['supplier_ref'])
                self._check_order(invoice,order)
                if order.status == 'paid':
                    ledger.transition(invoice_id,'inspecting','download_pending',payment_ref=order.payment_reference,inspection_origin=None)
                elif order.status == 'action_required':
                    ledger.transition(invoice_id,'inspecting','action_required',error='Visa authentication required',inspection_origin=None)
                elif order.status == 'pending':
                    ledger.transition(invoice_id,'inspecting','waiting_supplier',inspection_origin=None)
                elif state != 'prepared':
                    # A return to unpaid after an attempted charge is not permission to charge again.
                    ledger.transition(invoice_id,'inspecting','purchase_review',error='Uncertain store payment',inspection_origin=None)
                else:
                    if not ledger.transition(invoice_id,'inspecting','charging',inspection_origin=None):
                        return False
                    try:
                        paid = await self.store.pay(order.reference,'license:'+invoice_id)
                        self._check_order(invoice,paid)
                        next_state = {'paid':'download_pending','action_required':'action_required',
                                      'pending':'waiting_supplier','unpaid':'purchase_review'}[paid.status]
                        ledger.transition(invoice_id,'charging',next_state,payment_ref=paid.payment_reference or None)
                    except Exception as exc:
                        ledger.transition(invoice_id,'charging','purchase_review',error='Uncertain charge: '+type(exc).__name__)
            except Exception as exc:
                ledger.transition(invoice_id,'inspecting',state,error='Read failed: '+type(exc).__name__,inspection_origin=None)
            return True
        if state == 'download_pending':
            if not ledger.transition(invoice_id,state,'downloading'):
                return False
            try:
                # Recheck paid status before using a download button/file.
                order = await self.store.inspect(invoice['supplier_ref'])
                self._check_order(invoice,order)
                if order.status != 'paid' or order.payment_reference != invoice['payment_ref']:
                    raise ValueError('Store payment not confirmed')
                content = await self.store.download(order.reference)
                validate_license(content,invoice['source_sha'],Settings(mode='demo' if ledger.simulation else 'live'))
                # Output belongs to the invoice, even when multiple invoices
                # intentionally refer to the same immutable uploaded source.
                folder = Path(invoice['source_path']).parent / ('license-' + invoice_id)
                folder.mkdir(exist_ok=True)
                name = 'DEMO-NOT-A-LICENSE.txt' if ledger.simulation else 'device.license.bin'
                output = folder/name
                part = output.with_suffix(output.suffix+'.part')
                part.write_bytes(content)
                part.replace(output)
                ledger.transition(invoice_id,'downloading','ready',output_path=str(output),
                                  output_sha=hashlib.sha256(content).hexdigest())
            except Exception as exc:
                ledger.transition(invoice_id,'downloading','download_pending',error=type(exc).__name__)
            return True
        if state == 'ready':
            if not ledger.transition(invoice_id,'ready','sending'):
                return False
            try:
                output = Path(invoice['output_path'])
                if hashlib.sha256(output.read_bytes()).hexdigest() != invoice['output_sha']:
                    raise ValueError('License file changed')
                name = 'DEMO-NOT-A-LICENSE.txt' if ledger.simulation else 'device.license.bin'
                message_ref = await self.delivery.send(invoice,output,name)
                ledger.mark_delivered(invoice_id, message_ref)
            except Exception as exc:
                # A lost Telegram response can cause redelivery, never another purchase.
                ledger.transition(invoice_id,'sending','ready',error=type(exc).__name__)
            return True
        return False

    async def reconcile(self, invoice_id):
        invoice = self.ledger.get(invoice_id)
        if invoice['state'] != 'purchase_review' or not invoice['supplier_ref']:
            raise ValueError('Store reference is needed for read-only reconciliation')
        order = await self.store.inspect(invoice['supplier_ref'])
        self._check_order(invoice,order)
        if order.status == 'paid':
            self.ledger.transition(invoice_id,'purchase_review','download_pending',payment_ref=order.payment_reference)
            return True
        return False  # Never create an order or charge a card in reconciliation.


class UnconfiguredDriveMods:
    """No guessed URLs, browser selectors, card details or fake purchases."""
    simulation = False

    async def prepare(self, invoice):
        raise RuntimeError('DriveMods checkout adapter is not connected')

    async def inspect(self, reference):
        raise RuntimeError('DriveMods checkout adapter is not connected')

    async def pay(self, reference, idempotency_key):
        raise RuntimeError('DriveMods checkout adapter is not connected')

    async def download(self, reference):
        raise RuntimeError('DriveMods checkout adapter is not connected')
