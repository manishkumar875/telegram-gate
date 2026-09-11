"""HTTP server used by Phase 2 (Google OAuth callback) and health checks."""

from .server import WebServer, build_app

__all__ = ["WebServer", "build_app"]
