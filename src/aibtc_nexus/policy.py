"""Spending policy: hard limits checked before every outgoing payment."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .errors import PolicyViolation
from .ledger import Ledger

DAY = 86_400


@dataclass
class SpendingPolicy:
    max_payment_sats: int = 10_000
    daily_limit_sats: int = 100_000
    per_counterparty_daily_sats: int | None = None
    max_routing_fee_sats: int = 50
    max_routing_fee_ppm: int = 10_000  # 1%
    allowed_counterparties: set[str] | None = None  # None = anyone not blocked
    blocked_counterparties: set[str] = field(default_factory=set)
    frozen: bool = False  # kill switch

    def routing_fee_limit(self, amount_sats: int) -> int:
        ppm_limit = amount_sats * self.max_routing_fee_ppm // 1_000_000
        return max(1, min(self.max_routing_fee_sats, ppm_limit))


class PolicyEngine:
    def __init__(self, policy: SpendingPolicy, ledger: Ledger):
        self.policy = policy
        self.ledger = ledger

    def freeze(self) -> None:
        self.policy.frozen = True

    def unfreeze(self) -> None:
        self.policy.frozen = False

    def check_counterparty(self, counterparty: str) -> None:
        p = self.policy
        if p.frozen:
            raise PolicyViolation("spending is frozen (kill switch active)")
        if counterparty in p.blocked_counterparties:
            raise PolicyViolation("counterparty is blocked")
        if p.allowed_counterparties is not None and counterparty not in p.allowed_counterparties:
            raise PolicyViolation("counterparty is not on the allowlist")

    def check(self, amount_sats: int, counterparty: str, routing_fee_sats: int = 0) -> None:
        p = self.policy
        self.check_counterparty(counterparty)
        if amount_sats <= 0:
            raise PolicyViolation("amount must be positive")
        if amount_sats > p.max_payment_sats:
            raise PolicyViolation(
                f"{amount_sats} sats exceeds per-payment limit {p.max_payment_sats}"
            )
        total = amount_sats + routing_fee_sats
        since = time.time() - DAY
        spent = self.ledger.spent_since(since)
        if spent + total > p.daily_limit_sats:
            raise PolicyViolation(
                f"daily limit {p.daily_limit_sats} sats would be exceeded (spent {spent})"
            )
        if p.per_counterparty_daily_sats is not None:
            spent_cp = self.ledger.spent_since(since, counterparty)
            if spent_cp + total > p.per_counterparty_daily_sats:
                raise PolicyViolation(
                    f"daily limit for this counterparty ({p.per_counterparty_daily_sats} sats) "
                    "would be exceeded"
                )

    def report(self) -> dict:
        since = time.time() - DAY
        spent = self.ledger.spent_since(since)
        return {
            "frozen": self.policy.frozen,
            "spent_last_24h_sats": spent,
            "daily_limit_sats": self.policy.daily_limit_sats,
            "remaining_today_sats": max(0, self.policy.daily_limit_sats - spent),
            "max_payment_sats": self.policy.max_payment_sats,
        }
