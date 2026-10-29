"""Exception hierarchy for AiBtc-Nexus."""


class NexusError(Exception):
    """Base class for every error raised by this package."""


class ProtocolError(NexusError):
    """A message is malformed, badly signed, replayed or out of sequence."""


class NegotiationFailed(NexusError):
    """The counterparties could not agree on a price."""


class PolicyViolation(NexusError):
    """A payment would break the agent's spending policy."""


class PaymentError(NexusError):
    """A Lightning payment failed or could not be verified."""


class InvoiceMismatch(PaymentError):
    """An invoice does not match what was agreed (amount, hash, expiry)."""


class DuplicatePayment(PaymentError):
    """The same payment hash was already paid or is being paid."""


class BackendError(NexusError):
    """The Lightning backend returned an error or was unreachable."""


class ServiceError(NexusError):
    """The provider failed to deliver the purchased service."""


class DeliveryFailed(ServiceError):
    """Paid, but the provider did not deliver. Carries the proof of payment and
    everything needed to retry with ``BuyerAgent.retry_delivery`` at no extra cost."""

    def __init__(self, message: str, *, payment_hash: str, preimage: str, context: dict):
        super().__init__(message)
        self.payment_hash = payment_hash
        self.preimage = preimage
        self.context = context
