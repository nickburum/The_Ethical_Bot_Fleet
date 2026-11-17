"""The Ethical Bot Fleet: crawlers and AI agents that behave themselves."""

__version__ = "0.1.0"

from .audit import AuditLog, verify as verify_audit_log  # noqa: E402
from .fleet import (  # noqa: E402
    Blocked,
    Bot,
    Decision,
    FetchError,
    Fleet,
    FleetError,
    IdentityError,
    Policy,
    Response,
    Result,
)
from .robots import RobotsFile  # noqa: E402
from .signals import PURPOSES  # noqa: E402

__all__ = [
    "AuditLog", "Blocked", "Bot", "Decision", "FetchError", "Fleet", "FleetError", "IdentityError",
    "Policy", "PURPOSES", "Response", "Result", "RobotsFile", "verify_audit_log", "__version__",
]
