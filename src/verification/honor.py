"""Phase 1: honour-system "verification".

This verifier checks NOTHING about YouTube. It exists so the free version of
the product works with zero Google setup, and it is written to make that
impossible to misrepresent:

* ``provides_real_verification`` is hard-coded to ``False``
* every result sets ``was_technically_verified=False``

The bot reads those flags to decide whether to show the honesty disclosure,
so there is no code path where the user is told "verified" when they were not.

The only real friction available here is *time*: optionally require a few
seconds between tapping "Subscribe on YouTube" and tapping "I've Subscribed"
(``MIN_SECONDS_BEFORE_CONFIRM``). That is a nudge, not a check.
"""

from __future__ import annotations

import logging
from datetime import datetime

from src.database.db import utcnow

from .base import VerificationOutcome, VerificationResult, Verifier

logger = logging.getLogger(__name__)


class HonorVerifier(Verifier):
    method_name = "honor"
    provides_real_verification = False  # never change this to True

    async def verify(self, telegram_user_id: int) -> VerificationResult:
        record = await self.db.get_user(telegram_user_id)
        remaining = self.remaining_seconds(record.first_seen_at if record else None)
        if remaining > 0:
            return VerificationResult(
                outcome=VerificationOutcome.NEEDS_USER_ACTION,
                was_technically_verified=False,
                method=self.method_name,
                detail=f"wait {remaining}s",
            )

        logger.info(
            "User %s passed the honour-system gate (nothing was verified)",
            telegram_user_id,
        )
        return VerificationResult(
            outcome=VerificationOutcome.VERIFIED,
            was_technically_verified=False,  # deliberately always False
            method=self.method_name,
            detail="honour system - no technical check performed",
        )

    def remaining_seconds(self, gate_shown_at: datetime | None) -> int:
        """Seconds the user still has to wait before confirming, or 0.

        Measured from the moment the gate (with the YouTube link on it) was
        first shown to them, which is the only meaningful "did you have time
        to actually go and subscribe?" window we can observe.
        """
        minimum = self.settings.min_seconds_before_confirm
        if gate_shown_at is None or minimum <= 0:
            return 0
        elapsed = (utcnow() - gate_shown_at).total_seconds()
        return max(0, int(round(minimum - elapsed)))
