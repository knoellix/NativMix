"""
Audio backend package for NativMix.

Provides a platform-agnostic interface (AudioBackendBase) with implementations for:
- Linux: PipeWireManager (via pulsectl)
- Windows: WasapiManager (via pycaw, future)
"""

from nativmix.audio.base import AudioBackendBase, StreamInfo

__all__ = ["AudioBackendBase", "StreamInfo"]
