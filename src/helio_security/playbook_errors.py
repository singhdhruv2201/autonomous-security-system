"""Shared action errors, separate from the playbook registry."""

class PlaybookError(Exception):
    """Raised when a playbook execution or verification fails."""
    pass


class UnknownOutcome(PlaybookError):
    """A dispatched request could not be reconciled; never retry blindly."""
