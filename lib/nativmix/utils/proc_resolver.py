"""
Process name resolver for NativMix.

Implements Rule 2: The Electron/Chromium PID Hack (extended with Flatpak support).

Many applications (Discord, Spotify, VS Code, Chrome) register their audio
streams under generic names like "Chromium", "WEBRTC Voice Engine" or
"chromium". This module resolves the *real* application name by reading
/proc/<PID>/cmdline and walking up the process tree (PPID).

For Flatpak-packaged apps (Spotify from Flathub, etc.) the cmdline often
contains a generic bwrap/flatpak launcher path. In those cases we read
/proc/<PID>/root/.flatpak-info to extract the application ID.

Resolution order (per PID, then repeated for PPID):
-------------------------------------------------
1. Direct match against known binary names (fast path).
2. Parse --user-data-dir from cmdline → match against known profile dirs.
3. Parse --app-id from cmdline → direct Electron app ID lookup.
4. Flatpak: read /proc/<PID>/root/.flatpak-info → [Application] name= field.
5. Walk the parent process tree (PPID) and repeat steps 1–4 for each ancestor.
6. Return the raw cmdline binary basename as a last resort.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known application profiles
# ---------------------------------------------------------------------------

#: Maps known --user-data-dir path fragments to human-readable app names.
#: Keys are lowercase substrings; first match wins.
_USER_DATA_DIR_MAP: dict[str, str] = {
    "spotify": "Spotify",
    "discord": "Discord",
    "discordcanary": "Discord Canary",
    "discordptb": "Discord PTB",
    "chrome": "Google Chrome",
    "chromium": "Chromium",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
    "edge": "Microsoft Edge",
    "vscode": "VS Code",
    "code": "VS Code",
    "slack": "Slack",
    "teams": "Microsoft Teams",
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "element": "Element",
    "signal": "Signal",
    "zoom": "Zoom",
    "vesktop": "Vesktop",
    "rekord": "RE-KORD",
    "re-kord": "RE-KORD",
}

#: Maps --app-id values (Electron) to human-readable names.
_APP_ID_MAP: dict[str, str] = {
    "com.spotify.client": "Spotify",
    "com.discordapp.Discord": "Discord",
    "com.microsoft.teams": "Microsoft Teams",
    "com.slack.Slack": "Slack",
    "com.github.atom": "Atom",
    "com.rekord.app": "RE-KORD",
}

#: Maps Flatpak application IDs (from .flatpak-info → [Application] name=)
#: to human-readable names.  Keys are case-insensitive exact matches.
_FLATPAK_APP_MAP: dict[str, str] = {
    "com.spotify.client": "Spotify",
    "com.discordapp.discord": "Discord",
    "com.discordapp.discordcanary": "Discord Canary",
    "com.discordapp.discordptb": "Discord PTB",
    "com.valvesoftware.steam": "Steam",
    "com.slack.slack": "Slack",
    "us.zoom.zoom": "Zoom",
    "org.signal.signal": "Signal",
    "org.telegram.desktop": "Telegram",
    "im.riot.riot": "Element",
    "im.fluffychat.fluffychat": "FluffyChat",
    "com.microsoft.teams": "Microsoft Teams",
    "com.github.atom": "Atom",
    "com.visualstudio.code": "VS Code",
    "com.vscodium.codium": "VSCodium",
    "com.brave.browser": "Brave",
    "org.chromium.chromium": "Chromium",
    "com.google.chrome": "Google Chrome",
    "org.mozilla.firefox": "Firefox",
    "tv.kodi.kodi": "Kodi",
    "io.mpv.mpv": "mpv",
    "org.videolan.vlc": "VLC",
    "org.gnome.rhythmbox": "Rhythmbox",
    "io.bassi.amberol": "Amberol",
    "com.spotify.spotify": "Spotify",  # alternative bundle ID
    "dev.vencord.vesktop": "Vesktop",
}

#: Maps known binary basenames directly to human-readable names.
_BINARY_MAP: dict[str, str] = {
    "spotify": "Spotify",
    "spotify-bin": "Spotify",  # AUR package name
    "discord": "Discord",
    "discordcanary": "Discord Canary",
    "discordptb": "Discord PTB",
    "slack": "Slack",
    "zoom": "Zoom",
    "signal-desktop": "Signal",
    "telegram-desktop": "Telegram",
    "element-desktop": "Element",
    "teams": "Microsoft Teams",
    "code": "VS Code",
    "brave": "Brave Browser",
    "brave-bin": "Brave Browser",  # AUR
    "opera": "Opera",
    "vivaldi-stable": "Vivaldi",
    "chrome": "Google Chrome",
    "google-chrome-stable": "Google Chrome",
    "chromium": "Chromium",
    "firefox": "Firefox",
    "mpv": "mpv",
    "vlc": "VLC",
    "rhythmbox": "Rhythmbox",
    "clementine": "Clementine",
    "audacious": "Audacious",
    "strawberry": "Strawberry",
    "rekord": "RE-KORD",
    "re-kord": "RE-KORD",
}

#: PulseAudio/PipeWire stream names that are never the real application name.
#: When a stream reports one of these as its application.name or media.name,
#: the proc_resolver must identify the real app via PID — the PA name alone
#: is not usable for channel mapping.
#: NOTE: All entries must be lowercase for case-insensitive comparison.
GENERIC_PA_NAMES: frozenset[str] = frozenset(
    {
        # Chromium/Electron renderer process names
        "chromium",
        "chrome",
        "google chrome",
        # WebRTC generic names (Discord, Meet, Teams, ...)
        "webrtc voice engine",
        "webrtc audio device",
        "audio client",
        # PipeWire / PulseAudio internal
        "pipewire",
        "pipewire-media-session",
        "pulseaudio",
        "pulseaudio volume control",
        "pavucontrol",
        # Generic virtual/virtual-source names
        "audio-src",
        "audio source",
        "playback",
        "capture",
        "sink-input",
        "output",
        # Misc unidentifiable
        "unknown",
        "",
    }
)

# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------


def _read_cmdline(pid: int) -> list[str] | None:
    """
    Read /proc/<pid>/cmdline and split it on null bytes.

    Returns a list of argument strings, or None if the file cannot be read
    (process already gone, permission denied, etc.).
    """
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
        # cmdline is null-byte separated; strip trailing null
        return data.rstrip(b"\x00").split(b"\x00")
    except OSError:
        return None


def _read_environ(pid: int) -> dict[str, str]:
    """
    Read /proc/<pid>/environ and return it as a dictionary.
    """
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
        env_vars = {}
        for item in data.split(b"\x00"):
            if item:
                try:
                    k, v = item.decode("utf-8", errors="replace").split("=", 1)
                    env_vars[k] = v
                except ValueError:
                    pass
        return env_vars
    except OSError:
        return {}


def _check_fd_for_path(pid: int, target_substring: str) -> bool:
    """
    Check if the process has any open file descriptors pointing to a path
    containing the target_substring.
    """
    fd_dir = Path(f"/proc/{pid}/fd")
    if not fd_dir.exists() or not fd_dir.is_dir():
        return False

    try:
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
                if target_substring in target.lower():
                    return True
            except OSError:
                continue
    except OSError:
        pass

    return False


def _read_status_field(pid: int, field: str) -> str | None:
    """
    Read a single field from /proc/<pid>/status.

    Args:
        pid:   Process ID.
        field: Field name without colon, e.g. "PPid" or "Name".

    Returns:
        The trimmed field value, or None if not found / unreadable.
    """
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None

    for line in text.splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
    return None


def _get_ppid(pid: int) -> int | None:
    """Return the Parent PID of *pid*, or None on failure."""
    value = _read_status_field(pid, "PPid")
    if value is None:
        return None
    try:
        ppid = int(value)
        return ppid if ppid > 1 else None  # PID 1 (systemd/init) is the end
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Flatpak helper
# ---------------------------------------------------------------------------

_FLATPAK_INFO_PATH = "/proc/{pid}/root/.flatpak-info"
_FLATPAK_APP_SECTION = "[Application]"
_FLATPAK_NAME_KEY = "name="


def _read_flatpak_info(pid: int) -> str | None:
    """
    Try to read the Flatpak application ID from /proc/<pid>/root/.flatpak-info.

    This file exists only inside a Flatpak sandbox. It is an INI-style file;
    we look for the [Application] section and extract the ``name=`` value.

    Example content::

        [Application]
        name=com.spotify.Client
        runtime=...

    Args:
        pid: Process ID to inspect.

    Returns:
        Lower-cased Flatpak application ID string, or None if not a Flatpak
        process or if the file cannot be read.
    """
    path = Path(_FLATPAK_INFO_PATH.format(pid=pid))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # not a Flatpak process or cannot access the sandbox root

    in_application_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _FLATPAK_APP_SECTION:
            in_application_section = True
            continue
        if in_application_section:
            if stripped.startswith("["):  # entered a different section
                break
            if stripped.lower().startswith(_FLATPAK_NAME_KEY):
                app_id = stripped[len(_FLATPAK_NAME_KEY) :].strip().lower()
                logger.debug("Flatpak app ID for pid=%d: %s", pid, app_id)
                return app_id

    return None


# ---------------------------------------------------------------------------
# Process info provider — platform abstraction layer
# ---------------------------------------------------------------------------


class _LinuxProcProvider:
    """
    Provides process metadata by reading the Linux ``/proc`` filesystem.

    All methods return safe empty values when the process is gone or when
    access is denied — callers never need to handle ``OSError``.

    To add a Windows implementation: create a ``_WindowsProcProvider`` class
    with the same public interface (using ``psutil`` or Win32 API calls) and
    assign it to :data:`_proc_provider` when ``sys.platform == "win32"``.
    """

    @staticmethod
    def get_cmdline(pid: int) -> list[bytes] | None:
        """Return raw argument bytes from ``/proc/<pid>/cmdline``, or ``None``."""
        return _read_cmdline(pid)

    @staticmethod
    def get_environ(pid: int) -> dict[str, str]:
        """Return the process environment from ``/proc/<pid>/environ``."""
        return _read_environ(pid)

    @staticmethod
    def get_ppid(pid: int) -> int | None:
        """Return the parent PID, or ``None`` on failure or if PID is init."""
        return _get_ppid(pid)

    @staticmethod
    def get_flatpak_app_id(pid: int) -> str | None:
        """Return the lower-cased Flatpak application ID, or ``None``."""
        return _read_flatpak_info(pid)

    @staticmethod
    def check_fd_for_path(pid: int, target_substring: str) -> bool:
        """Return ``True`` if the process has an open fd containing *target_substring*."""
        return _check_fd_for_path(pid, target_substring)


#: Active process-info provider.
#: ``None`` on non-Linux platforms — :func:`resolve_app_name` returns *fallback* immediately.
#: Swap for a ``_WindowsProcProvider`` instance once the Windows backend is ready.
_proc_provider: _LinuxProcProvider | None = _LinuxProcProvider() if sys.platform == "linux" else None


def _resolve_flatpak(pid: int) -> str | None:
    """
    Attempt to resolve the app name via the Flatpak sandbox info file.

    Returns the human-readable name or None.
    """
    if _proc_provider is None:
        return None
    app_id = _proc_provider.get_flatpak_app_id(pid)
    if app_id is None:
        return None
    # Exact match in the Flatpak map (case-insensitive; IDs were lower-cased above)
    name = _FLATPAK_APP_MAP.get(app_id)
    if name:
        return name
    # Fallback: derive a best-effort display name from the last ID component
    # e.g. "org.mozilla.firefox" → "firefox" (better than nothing)
    parts = app_id.rsplit(".", 1)
    if len(parts) == 2:
        return parts[-1].capitalize()
    return None


# ---------------------------------------------------------------------------
# Cmdline argument parsing
# ---------------------------------------------------------------------------

_USER_DATA_DIR_RE = re.compile(r"--user-data-dir=(.+)$")
_APP_ID_RE = re.compile(r"--app-id=(.+)$")


def _extract_flag(args: list[str], pattern: re.Pattern[str]) -> str | None:
    """Return the first capture group of *pattern* matched against any arg."""
    for arg in args:
        m = pattern.match(arg.strip())
        if m:
            return m.group(1).strip()
    return None


def _match_user_data_dir(user_data_dir: str) -> str | None:
    """
    Try to match *user_data_dir* against the known profile directory map.

    The path is lower-cased and each key is tested as a substring.
    """
    lower = user_data_dir.lower()
    for fragment, name in _USER_DATA_DIR_MAP.items():
        if fragment in lower:
            return name
    return None


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------


def _resolve_pid(pid: int) -> str | None:
    """
    Attempt to resolve the real application name for a single PID.

    Returns the name string or None if unresolvable from this PID alone.
    """
    if _proc_provider is None:
        return None

    args_bytes = _proc_provider.get_cmdline(pid)
    if not args_bytes:
        return None

    # Decode each argument, ignoring errors
    args = [a.decode("utf-8", errors="replace") for a in args_bytes if a]
    if not args:
        return None

    env = _proc_provider.get_environ(pid)

    # Vencord/Vesktop usually set generic names but their config path gives them away.
    # Check if this process uses a vesktop config path via env
    xdg_config = env.get("XDG_CONFIG_HOME", "")
    home = env.get("HOME", "")

    # 0. Deep Path Inspection for Vesktop
    if "vesktop" in xdg_config.lower() or (home and os.path.isdir(os.path.join(home, ".config", "vesktop"))):
        # We need to make sure this is actually the vesktop process and not just some user shell.
        # Check command line for electron/chrome/chromium and vesktop markers.
        cmd_str = " ".join(args).lower()
        a0 = args[0].lower()
        if "vesktop" in cmd_str or "electron" in a0 or "chrome" in a0 or "chromium" in a0:
            if (
                "vesktop" in cmd_str
                or "vesktop" in env.get("PWD", "").lower()
                or (home and Path(home, ".config", "vesktop").exists())
            ):
                # Confirm Vesktop specifically — not a generic Chrome instance that shares the config dir.
                # Check binary path, --app-path flag, or open fd pointing to vesktop paths.
                if (
                    "vesktop" in cmd_str
                    or _proc_provider.check_fd_for_path(pid, ".config/vesktop")
                    or _proc_provider.check_fd_for_path(pid, "/opt/vesktop")
                ):
                    return "Vesktop"

    binary = os.path.basename(args[0]).lower()

    # Step 1: Direct binary name match
    if binary in _BINARY_MAP:
        return _BINARY_MAP[binary]

    # Step 2: --user-data-dir
    user_data_dir = _extract_flag(args, _USER_DATA_DIR_RE)
    if user_data_dir:
        name = _match_user_data_dir(user_data_dir)
        if name:
            return name

    # Step 3: --app-id
    app_id = _extract_flag(args, _APP_ID_RE)
    if app_id and app_id in _APP_ID_MAP:
        return _APP_ID_MAP[app_id]

    # Step 4: Flatpak sandbox info
    flatpak_name = _resolve_flatpak(pid)
    if flatpak_name:
        return flatpak_name

    return None


@lru_cache(maxsize=256)
def resolve_app_name(pid: int, fallback: str = "Unknown") -> str:
    """
    Resolve the human-readable application name for the given PID.

    Implements the full Electron/Chromium hack with parent-process traversal:
    - Reads /proc/<pid>/cmdline and /proc/<pid>/status.
    - Walks up the process tree (via PPid in /proc/<pid>/status) until a
      known application is found or PID 1 (init/systemd) is reached.

    Results are cached per PID (LRU, 256 entries) to avoid repeated /proc
    reads for the same long-running stream.

    Args:
        pid:      Process ID from pulsectl's application.process.id property.
        fallback: Name returned when resolution fails entirely.

    Returns:
        Human-readable application name, e.g. "Spotify" or "Discord".
    """
    if pid <= 0 or _proc_provider is None:
        return fallback

    visited: set[int] = set()
    current_pid = pid

    while current_pid and current_pid not in visited:
        visited.add(current_pid)

        name = _resolve_pid(current_pid)
        if name:
            logger.debug(
                "resolve_app_name: pid=%d → '%s' (resolved via pid=%d)",
                pid,
                name,
                current_pid,
            )
            return name

        parent = _proc_provider.get_ppid(current_pid)
        if parent is None or parent in visited:
            break
        current_pid = parent

    logger.debug("resolve_app_name: pid=%d → fallback '%s'", pid, fallback)
    return fallback


def invalidate_cache() -> None:
    """Clear the LRU cache, e.g. when streams are removed."""
    resolve_app_name.cache_clear()
    if sys.platform == "win32":
        resolve_app_name_windows.cache_clear()


# ---------------------------------------------------------------------------
# Windows resolver (psutil-based, mirrors resolve_app_name for Windows)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    try:
        import psutil as _psutil

        _PSUTIL_AVAILABLE = True
    except ImportError:
        _psutil = None  # type: ignore[assignment]
        _PSUTIL_AVAILABLE = False

    def _resolve_pid_windows(pid: int) -> str | None:
        """
        Attempt to resolve the real application name for a single PID on Windows.

        Steps mirror _resolve_pid() on Linux:
        1. Binary name (.exe stripped) → _BINARY_MAP
        2. --user-data-dir fragment    → _USER_DATA_DIR_MAP
        3. --app-id                    → _APP_ID_MAP
        """
        if not _PSUTIL_AVAILABLE:
            return None
        try:
            proc = _psutil.Process(pid)
            # Step 1: binary name
            binary = proc.name().lower()
            if binary.endswith(".exe"):
                binary = binary[:-4]
            if binary in _BINARY_MAP:
                return _BINARY_MAP[binary]
            # Steps 2 & 3: cmdline args
            try:
                args = proc.cmdline()
            except (_psutil.AccessDenied, _psutil.NoSuchProcess, OSError):
                args = []
            if args:
                user_data_dir = _extract_flag(args, _USER_DATA_DIR_RE)
                if user_data_dir:
                    name = _match_user_data_dir(user_data_dir)
                    if name:
                        return name
                app_id = _extract_flag(args, _APP_ID_RE)
                if app_id and app_id in _APP_ID_MAP:
                    return _APP_ID_MAP[app_id]
        except (_psutil.NoSuchProcess, _psutil.AccessDenied, OSError):
            pass
        return None

    @lru_cache(maxsize=256)
    def resolve_app_name_windows(pid: int, fallback: str = "Unknown") -> str:
        """
        Resolve the human-readable application name for the given PID on Windows.

        Uses psutil instead of /proc.  Resolution order:
        1. Binary name (.exe stripped) → _BINARY_MAP
        2. --user-data-dir cmdline fragment → _USER_DATA_DIR_MAP
        3. --app-id cmdline flag → _APP_ID_MAP
        4. Walk PPID chain (steps 1–3 for each ancestor)
        5. Return cleaned exe name as last resort

        Flatpak and /proc/fd checks are skipped (not applicable on Windows).
        """
        if not _PSUTIL_AVAILABLE or pid <= 0:
            return fallback

        visited: set[int] = set()
        current_pid = pid

        while current_pid and current_pid not in visited:
            visited.add(current_pid)

            name = _resolve_pid_windows(current_pid)
            if name:
                logger.debug(
                    "resolve_app_name_windows: pid=%d → '%s' (via pid=%d)",
                    pid,
                    name,
                    current_pid,
                )
                return name

            try:
                parent = _psutil.Process(current_pid).ppid()
            except (_psutil.NoSuchProcess, _psutil.AccessDenied, OSError):
                break
            if not parent or parent in visited:
                break
            current_pid = parent

        # Last resort: return the cleaned exe name
        try:
            raw = _psutil.Process(pid).name()
            if raw.lower().endswith(".exe"):
                raw = raw[:-4]
            if raw:
                logger.debug("resolve_app_name_windows: pid=%d → exe fallback '%s'", pid, raw)
                return raw
        except Exception:
            pass

        logger.debug("resolve_app_name_windows: pid=%d → fallback '%s'", pid, fallback)
        return fallback

else:
    # Stub so imports on Linux don't fail
    def resolve_app_name_windows(pid: int, fallback: str = "Unknown") -> str:  # type: ignore[misc]
        """Not applicable on non-Windows platforms."""
        return fallback
