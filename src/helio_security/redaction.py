"""Shared text hygiene before evidence, assessments, or trace values persist."""
import os
import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
SECRET = re.compile(r"(?:\b(?:sk-|xox[baprs]-|gh[pousr]_)[A-Za-z0-9_-]+|\bAKIA[A-Z0-9]{16}\b|\bbearer\s+[^\s,;]+)", re.I)
IDENTIFIER = re.compile(r"[a-zA-Z0-9_.:-]{1,2048}\Z")


def scrub_text(value):
    text = str(value)
    # Also catch opaque configured credentials without relying on their prefix.
    for name, secret in os.environ.items():
        if name.endswith(("_TOKEN", "_API_KEY", "_PASSWORD", "_SECRET")) and len(secret) >= 8:
            text = text.replace(secret, "[REDACTED_SECRET]")
    text = EMAIL.sub("[REDACTED_EMAIL]", text)
    text = SSN.sub("[REDACTED_SSN]", text)
    return SECRET.sub("[REDACTED_SECRET]", text)


def safe_identifier(value, limit=2048):
    return (isinstance(value, str) and len(value) <= limit and
            IDENTIFIER.fullmatch(value) is not None and scrub_text(value) == value)
