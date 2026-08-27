"""
Easy Effects coexistence helpers (Phase 1).

Detect when a playback stream sits on an EE processing sink so NativMix can
pause auto-routing while still applying volume/mute on the stream itself.
"""

from __future__ import annotations

from typing import Literal

VolumeMode = Literal["stream", "vsink"]


def is_easyeffects_sink(sink_name: str | None) -> bool:
    """True when *sink_name* is an Easy Effects playback (output) processing sink."""
    if not sink_name:
        return False
    name = sink_name.strip().lower()
    if not name:
        return False
    # Capture / input side — not a playback hold target.
    if name == "easyeffects_source" or name.endswith("_source"):
        return False
    return name == "easyeffects_sink" or name.startswith("easyeffects_")


def resolve_auto_route_target(
    *,
    current_sink: str | None,
    vsink_enabled: bool,
    vsink_name: str,
    default_sink: str | None,
) -> str | None:
    """
    Return the sink name to move a mapped stream to, or None for no move.

    None means: held by Easy Effects, already on the intended target, or
    missing a usable default sink when V-Sink is off.
    """
    if is_easyeffects_sink(current_sink):
        return None

    if vsink_enabled:
        if current_sink == vsink_name:
            return None
        return vsink_name

    if not default_sink or default_sink.startswith("NativMix_"):
        return None
    if current_sink == default_sink:
        return None
    return default_sink


def volume_apply_mode(
    *,
    current_sink: str | None,
    vsink_enabled: bool,
    vsink_name: str,
) -> VolumeMode:
    """
    Where channel fader volume should be written for this stream.

    EE-held streams always use stream gain (never the shared easyeffects_sink).
    Streams actually on the channel V-Sink use null-sink gain.
    """
    if is_easyeffects_sink(current_sink):
        return "stream"
    if vsink_enabled and current_sink == vsink_name:
        return "vsink"
    return "stream"
