"""The verification interface shared by Phase 1 and Phase 2."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.config import Settings
    from src.database import Database


class VerificationOutcome(str, Enum):
    """What happened when we tried to verify a user."""

    #: Passed. ``was_technically_verified`` says whether that meant anything.
    VERIFIED = "verified"

    #: We genuinely checked and they are NOT subscribed.
    NOT_SUBSCRIBED = "not_subscribed"

    #: The user must go somewhere (a Google consent screen) before we can tell.
    NEEDS_USER_ACTION = "needs_user_action"

    #: Something broke (network, API quota). Ask them to retry.
    ERROR = "error"


@dataclass(frozen=True)
class VerificationResult:
    outcome: VerificationOutcome

    #: ``True`` only when a third party (YouTube) actually confirmed the
    #: subscription. The honour-system verifier ALWAYS sets this to ``False``
    #: so the bot can never accidentally claim a check that did not happen.
    was_technically_verified: bool = False

    #: Stored in the database as ``verification_method``.
    method: str = "unknown"

    #: A URL the user must visit (only for NEEDS_USER_ACTION).
    action_url: str | None = None

    #: Internal detail for logs. Never shown verbatim to the user.
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is VerificationOutcome.VERIFIED


class Verifier(abc.ABC):
    """Strategy interface. Implementations must be safe to call concurrently."""

    #: Stored in the DB and shown in /stats.
    method_name: str = "unknown"

    #: When ``False`` the bot MUST disclose that nothing was really checked.
    provides_real_verification: bool = False

    def __init__(self, settings: "Settings", database: "Database") -> None:
        self.settings = settings
        self.db = database

    @abc.abstractmethod
    async def verify(self, telegram_user_id: int) -> VerificationResult:
        """Decide whether ``telegram_user_id`` may pass the gate."""

    async def start(self) -> None:
        """Optional async setup (open HTTP clients, start web server, ...)."""

    async def stop(self) -> None:
        """Optional async teardown."""

    def describe(self) -> str:
        """One-line human description used in startup logs and /status."""
        if self.provides_real_verification:
            return "real verification via the official YouTube Data API"
        return "honour-system confirmation (nothing is technically checked)"


def build_verifier(settings: "Settings", database: "Database") -> Verifier:
    """Return the verifier selected by ``VERIFICATION_MODE``."""
    from src.config import VerificationMode  # local import avoids a cycle

    from .honor import HonorVerifier
    from .youtube_oauth import YouTubeOAuthVerifier

    if settings.verification_mode is VerificationMode.YOUTUBE_OAUTH:
        return YouTubeOAuthVerifier(settings, database)
    return HonorVerifier(settings, database)
