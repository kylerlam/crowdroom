"""Generate the permanent public account identifier used for room verification."""

import secrets


def generate_public_id() -> str:
    """Return eight random digits in a readable CrowdRoom ID format."""
    digits = f"{secrets.randbelow(100_000_000):08d}"
    return f"CR-{digits[:4]}-{digits[4:]}"
