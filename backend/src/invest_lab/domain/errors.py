class VersionConflict(ValueError):
    """The caller edited an older version; its draft must be preserved."""


class TradingBlocked(ValueError):
    """A command cannot run with the current account or data state."""


class UnknownOutcome(RuntimeError):
    """An external side effect may have happened. Reconcile, never blindly retry."""
