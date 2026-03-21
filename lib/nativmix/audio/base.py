"""
Abstract base class for audio backends.

Defines the common interface that all platform-specific backends (Linux PipeWire,
Windows WASAPI) must implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, pyqtSignal


@dataclass
class StreamInfo:
    """Represents a single audio sink input (playback stream)."""

    # PulseAudio / PipeWire sink input index
    index: int

    # Resolved human-readable application name (e.g. "Spotify", "Firefox")
    # May be a generic fallback until PID resolution is complete.
    app_name: str

    # Process ID of the owning application (0 if unknown)
    pid: int = 0

    # Current volume as a linear float [0.0 – 1.0]
    volume: float = 1.0

    # Whether the stream is currently software-muted
    muted: bool = False

    # Raw props from the audio server for debugging / further resolution
    props: dict[str, str] = field(default_factory=dict)


class AudioBackendBase(QObject):
    """
    Platform-agnostic interface for audio stream management.

    Subclasses implement this for each supported platform:
        - Linux  → PipeWireManager  (pulsectl)
        - Windows → WasapiManager   (pycaw, future)
    """

    peaks_updated = pyqtSignal(list)  # list[float], one per channel

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

    def start(self) -> None:
        """Start the background listener / polling thread."""
        raise NotImplementedError

    def stop(self) -> None:
        """Stop the background listener and clean up resources."""
        raise NotImplementedError

    def set_volume(self, stream_index: int, volume: float) -> None:
        """
        Set the volume of a stream.

        Args:
            stream_index: Backend-specific stream identifier.
            volume: Linear volume in the range [0.0, 1.0].
        """
        raise NotImplementedError

    def set_mute(self, stream_index: int, muted: bool) -> None:
        """
        Set the mute state of a stream.

        Args:
            stream_index: Backend-specific stream identifier.
            muted: True to mute, False to unmute.
        """
        raise NotImplementedError
