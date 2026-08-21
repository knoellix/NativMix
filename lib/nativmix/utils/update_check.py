"""Helpers for GitHub release update notifications (Windows / Flatpak)."""

from __future__ import annotations

from packaging.version import InvalidVersion, Version


def normalize_version(tag_or_version: str) -> str:
    """Strip a leading ``v``/``V`` from a Git tag or version string."""
    text = (tag_or_version or "").strip()
    if len(text) >= 2 and text[0] in "vV" and text[1].isdigit():
        return text[1:]
    return text


def is_newer(remote: str, local: str) -> bool:
    """Return True if *remote* is a newer release than *local*."""
    try:
        return Version(normalize_version(remote)) > Version(normalize_version(local))
    except InvalidVersion:
        return False


def should_notify(
    *,
    remote: str,
    local: str,
    dismissed: str | None,
    checks_enabled: bool,
) -> bool:
    """Decide whether to show an update hint for *remote*.

    - Master switch off → never.
    - Remote not newer than installed → never.
    - User dismissed *this* remote version → never (next release notifies again).
    - Plain dismiss/close without silencing → notify again on next start.
    """
    if not checks_enabled:
        return False
    if not remote or not is_newer(remote, local):
        return False
    if dismissed and normalize_version(dismissed) == normalize_version(remote):
        return False
    return True
