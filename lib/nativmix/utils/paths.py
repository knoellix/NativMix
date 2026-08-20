"""
Platform detection and path management for NativMix.

Provides OS-specific detection (Arch/CachyOS, Debian/Ubuntu, SteamOS, Windows)
and the canonical paths for config, data, logs, and binaries on each platform.

All paths follow XDG conventions on Linux and the Windows standard AppData layout.
SteamOS is treated as a special Arch-based system with an immutable root
filesystem, so only user-space paths are used there.

Usage
-----
    from nativmix.utils.paths import get_platform, get_config_dir, get_log_dir
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path Resolution (Dynamic & Robust)
# ---------------------------------------------------------------------------


def get_app_root() -> Path:
    """
    Determine the application root directory dynamically.
    Works for local development, installed environments, and PyInstaller bundles.
    """
    import sys

    # PyInstaller sets sys.frozen and sys._MEIPASS to the extraction dir.
    # Assets are bundled relative to _MEIPASS, so that IS the root.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)

    # Start from the location of this file: lib/nativmix/utils/paths.py
    current_file = Path(__file__).resolve()
    # If in 'lib/nativmix/utils/', project root is 4 levels up
    if "nativmix/utils" in str(current_file):
        return current_file.parent.parent.parent.parent
    # Fallback to current working directory if resolution fails
    return Path(os.getcwd())


def get_assets_dir() -> Path:
    """
    Find the assets directory by checking prioritized locations.
    1. Project root (local development)
    2. ../../share/nativmix/assets (relative to lib/ during system install)
    3. /usr/share/nativmix/assets (fallback system path)
    """
    root = get_app_root()

    # Priority 1: Local assets folder (top-level)
    local_assets = root / "assets"
    if local_assets.is_dir():
        return local_assets

    # Priority 2: System installation (relative to prefix)
    # If app is in /usr/lib/python3.X/site-packages/nativmix,
    # assets are often in /usr/share/nativmix/assets
    system_rel = root.parent.parent / "share" / "nativmix" / "assets"
    if system_rel.is_dir():
        return system_rel

    # Priority 3: Platform-appropriate generic fallback (no hardcoded /usr/share on Windows)
    if platform.system() == "Windows":
        prog = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        return prog / "NativMix" / "assets"
    return Path("/usr/share/nativmix/assets")


# On Windows there is no /usr/share — set to None so get_icon_path() skips it.
_SYSTEM_ASSETS: Path | None = None if platform.system() == "Windows" else Path("/usr/share/nativmix/assets")
_LOCAL_ASSETS: Path = get_assets_dir()


# ---------------------------------------------------------------------------
# OS Detection
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _read_os_release() -> dict[str, str]:
    """
    Parse /etc/os-release into a key=value dict.

    Returns an empty dict on Windows or when the file is missing.
    """
    result: dict[str, str] = {}
    for path in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, _, value = line.partition("=")
                        result[key.strip()] = value.strip().strip('"')
                return result
            except OSError:
                pass
    return result


@lru_cache(maxsize=1)
def get_platform() -> str:
    """
    Return a canonical platform string for the current environment.

    Possible values:
        "arch"    – Arch Linux, CachyOS, Manjaro, etc.
        "debian"  – Debian, Ubuntu, Linux Mint, Pop!_OS, etc.
        "steamos" – SteamOS (Arch-based, immutable root filesystem)
        "windows" – Microsoft Windows
        "linux"   – Any other Linux distribution
        "other"   – macOS, BSDs, unknown

    The value is cached after the first call.
    """
    if platform.system() == "Windows":
        return "windows"

    if platform.system() != "Linux":
        return "other"

    os_release = _read_os_release()
    os_id = os_release.get("ID", "").lower()
    id_like = os_release.get("ID_LIKE", "").lower()
    name = os_release.get("NAME", "").lower()

    # SteamOS check first (it's Arch-based but needs special treatment)
    if "steamos" in name or "steam" in os_id:
        return "steamos"

    # Arch-family detection
    if os_id == "arch" or "arch" in id_like.split():
        return "arch"

    # Debian-family detection
    debian_ids = {"debian", "ubuntu", "linuxmint", "pop", "raspbian", "kali", "elementary", "zorin", "mx", "neon"}
    if os_id in debian_ids or "debian" in id_like.split() or "ubuntu" in id_like.split():
        return "debian"

    return "linux"


def is_arch() -> bool:
    return get_platform() == "arch"


def is_debian() -> bool:
    return get_platform() == "debian"


def is_steamos() -> bool:
    return get_platform() == "steamos"


def is_windows() -> bool:
    return get_platform() == "windows"


SERVICE_UNIT = "app-nativmix.service"


def is_systemd_service() -> bool:
    """Return True when running as the app-nativmix.service systemd unit.

    INVOCATION_ID and JOURNAL_STREAM are inherited by all KDE session children
    (terminals, launchers, etc.) and cannot be used alone.  The only reliable
    check is the cgroup path, which contains 'app-nativmix.service' only when
    the process was started by that specific unit.
    """
    try:
        cgroup = Path("/proc/self/cgroup").read_text()
        return SERVICE_UNIT in cgroup
    except OSError:
        return False


def is_linux() -> bool:
    return get_platform() in ("arch", "debian", "steamos", "linux")


def is_flatpak() -> bool:
    """Return True when running inside a Flatpak sandbox.

    Prefers the Flatpak-provided ``FLATPAK_ID`` environment variable and falls
    back to the sandbox marker file ``/.flatpak-info``.
    """
    if os.environ.get("FLATPAK_ID"):
        return True
    return Path("/.flatpak-info").is_file()


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def get_config_dir() -> Path:
    """
    Return the user config directory for NativMix.

    Linux / SteamOS  →  $XDG_CONFIG_HOME/nativmix/     (default: ~/.config/nativmix/)
    Windows          →  %APPDATA%\\nativmix\\
    """
    if is_windows():
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "nativmix"

    # Linux (Arch, Debian, SteamOS, generic)
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "nativmix"


def get_data_dir() -> Path:
    """
    Return the user data directory for NativMix.

    Linux / SteamOS  →  $XDG_DATA_HOME/nativmix/     (default: ~/.local/share/nativmix/)
    Windows          →  %APPDATA%\\nativmix\\data\\
    """
    if is_windows():
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "nativmix" / "data"

    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "nativmix"


def get_log_dir() -> Path:
    """
    Return the platform-appropriate log directory.

    Linux / SteamOS  →  $XDG_CACHE_HOME/nativmix/logs  (default: ~/.cache/nativmix/logs)
    Windows          →  %LOCALAPPDATA%\\nativmix\\logs\\
    """
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "NativMix" / "Logs"

    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "nativmix" / "logs"


def get_binary_dir() -> Path:
    """
    Return the expected binary installation directory.

    Arch / CachyOS   →  /usr/bin/
    Debian / Ubuntu  →  /usr/local/bin/
    SteamOS          →  ~/.local/bin/   (immutable root filesystem)
    Windows          →  %LOCALAPPDATA%\\nativmix\\
    """
    plat = get_platform()
    if plat == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "nativmix"
    if plat == "steamos":
        return Path.home() / ".local" / "bin"
    if plat == "debian":
        return Path("/usr/local/bin")
    # arch / generic linux
    return Path("/usr/bin")


def get_autostart_dir() -> Path:
    """
    Return the XDG autostart directory (always user-space, never system-wide).

    SteamOS, Arch, Debian  →  ~/.config/autostart/
    Windows                →  not applicable (returns empty Path)
    """
    if is_windows():
        return Path()  # autostart is handled via the Windows registry elsewhere
    return Path.home() / ".config" / "autostart"


def get_ipc_socket_path() -> str:
    """
    Return a user-specific, fully qualified socket path for IPC.

    On Linux, this is $XDG_RUNTIME_DIR/nativmix_ipc.sock.
    """
    if is_windows():
        # Named pipe name (no path prefix — QLocalServer adds \\.\pipe\ automatically).
        # Include the username so multiple Windows users on the same machine get separate pipes.
        import getpass

        try:
            _user = getpass.getuser().replace(" ", "_")
        except Exception:
            _user = "user"
        return f"nativmix_ipc_{_user}"

    uid = os.getuid()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/nativmix_{uid}"
    return os.path.join(runtime_dir, "nativmix_ipc.sock")


# ---------------------------------------------------------------------------
# Asset / icon resolution
# ---------------------------------------------------------------------------


def get_asset_path(filename: str) -> Path:
    """Return the absolute path to a named asset file."""
    return get_assets_dir() / filename


def get_icon_path() -> Path | None:
    """
    Return the absolute path to the NativMix application icon.

    Search order:
      1. /usr/share/nativmix/assets/icon.png  (AUR / PKGBUILD system install)
      2. <project_root>/assets/icon.png        (local development checkout)
      3. None → caller should use QIcon.fromTheme("nativmix")
    """
    for assets_dir in filter(None, (_LOCAL_ASSETS, _SYSTEM_ASSETS)):
        candidate = assets_dir / "icon.png"
        if candidate.exists():
            logger.debug("Icon found: %s", candidate)
            return candidate

    logger.debug("No icon file found; caller should use QIcon.fromTheme fallback")
    return None


# ---------------------------------------------------------------------------
# One-time config directory migration
# ---------------------------------------------------------------------------


def migrate_legacy_config_dir() -> None:
    """
    One-time migration: move ~/.config/NativMix → ~/.config/nativmix.

    Runs only when the legacy capitalised directory exists and differs from
    the canonical lowercase path.  Each file is overwritten only when the
    source is newer than an existing target (mtime comparison).  The legacy
    directory is removed after a successful migration.

    Safe on case-insensitive filesystems (Windows/macOS): the resolved-path
    equality check ensures we never copy a directory onto itself.
    """
    canonical = get_config_dir()  # e.g. ~/.config/nativmix
    legacy = canonical.parent / "NativMix"  # e.g. ~/.config/NativMix

    # Nothing to do when the legacy directory is absent or is the same
    # filesystem object as the canonical one (case-insensitive FS).
    if not legacy.exists() or legacy.resolve() == canonical.resolve():
        return

    logger.info("Migrating config directory: %s → %s", legacy, canonical)
    canonical.mkdir(parents=True, exist_ok=True)

    for src in legacy.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(legacy)
        dst = canonical / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Overwrite only when source is strictly newer (or target is absent).
        if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
            shutil.copy2(src, dst)
            logger.debug("Migrated: %s", rel)

    try:
        shutil.rmtree(legacy)
        logger.info("Removed legacy config directory: %s", legacy)
    except OSError as exc:
        logger.warning("Could not remove legacy config directory %s: %s", legacy, exc)


# ---------------------------------------------------------------------------
# Startup diagnostics
# ---------------------------------------------------------------------------


def log_platform_info() -> None:
    """Log detected platform and all resolved paths at INFO level."""
    plat = get_platform()
    os_release = _read_os_release()
    logger.debug(
        "Platform detected: %s (%s %s)",
        plat,
        os_release.get("NAME", platform.system()),
        os_release.get("VERSION_ID", platform.release()),
    )
    logger.debug("Config dir : %s", get_config_dir())
    logger.debug("Data dir   : %s", get_data_dir())
    logger.debug("Log dir    : %s", get_log_dir())
    logger.debug("Binary dir : %s", get_binary_dir())
