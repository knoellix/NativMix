"""
Asset path resolver for NativMix.

Provides a single function to locate the application icon regardless of
whether NativMix was installed system-wide (AUR / PKGBUILD → /usr/share/)
or is running from a local development checkout.

Search order:
    1. /usr/share/nativmix/assets/icon.png   (AUR system install)
    2. <project_root>/assets/icon.png         (local dev via install.sh)
    3. None  → caller should fall back to QIcon.fromTheme("nativmix")
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# System-wide install path (set by PKGBUILD → install -Dm644 … /usr/share/nativmix/assets/)
_SYSTEM_ASSETS = Path("/usr/share/nativmix/assets")

# Local development path (relative to this file: src/nativmix/utils/paths.py → ../../.. → project root)
_LOCAL_ASSETS = Path(__file__).resolve().parent.parent.parent.parent / "assets"


def get_icon_path() -> Path | None:
    """
    Return the absolute path to the NativMix application icon.

    Checks the system install location first, then falls back to the local
    development tree.  Returns None if neither location has the icon file,
    in which case the caller should use QIcon.fromTheme("nativmix").
    """
    for assets_dir in (_SYSTEM_ASSETS, _LOCAL_ASSETS):
        candidate = assets_dir / "icon.png"
        if candidate.exists():
            logger.debug("Icon found: %s", candidate)
            return candidate

    logger.debug("No icon file found; caller should use QIcon.fromTheme fallback")
    return None
