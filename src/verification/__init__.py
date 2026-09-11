"""Pluggable subscription verification.

The bot never checks a YouTube subscription directly. It asks a
:class:`~src.verification.base.Verifier` instead. That indirection is what
makes Phase 2 a drop-in upgrade rather than a rewrite:

    VERIFICATION_MODE=honor          -> HonorVerifier      (Phase 1)
    VERIFICATION_MODE=youtube_oauth  -> YouTubeOAuthVerifier (Phase 2)
"""

from .base import (
    VerificationOutcome,
    VerificationResult,
    Verifier,
    build_verifier,
)
from .honor import HonorVerifier
from .youtube_oauth import YouTubeOAuthVerifier

__all__ = [
    "HonorVerifier",
    "VerificationOutcome",
    "VerificationResult",
    "Verifier",
    "YouTubeOAuthVerifier",
    "build_verifier",
]
