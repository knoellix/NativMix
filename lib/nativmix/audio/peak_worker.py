"""
Isolated subprocess worker for pulsectl peak sampling.

Runs in a separate process to shield the main app from SIGSEGV in
PipeWire's libpulse compatibility layer when creating recording streams.

Protocol
--------
stdin  → one JSON line on startup: {"sources": ["source_name", null, ...]}
         null entries are skipped and return 0.0 (channel has no peak source).
stdout → one JSON line per sample cycle: [float, ...]
         Each float is a peak level in [0.0, 1.0].
stderr → ignored by parent; errors go to /dev/null effectively.

Resilience model
----------------
- Per-source errors (source unavailable, suspended, etc.) are counted.
  After _MAX_SOURCE_ERRORS consecutive failures the source is silenced
  for this session (returns 0.0) rather than crashing the process.
  This makes the VU meter immune to system-volume changes: moving the
  system master volume may briefly suspend PipeWire nodes, but the
  monitor source of a V-Sink null-sink is measured independently of the
  system output chain, so peaks recover as soon as the source is active.
- PulseError at the session level (including PulseDisconnected when
  PipeWire restarts) triggers an internal reconnect with a short delay
  rather than exiting.  The parent process only needs to restart this
  worker for genuine, unrecoverable failures.
"""

from __future__ import annotations

import json
import sys
import time

_MAX_SOURCE_ERRORS = 15   # per-source failures before that source is silenced
_MAX_RECONNECTS    = 10   # reconnect attempts before giving up and exiting


def _run_session(pulsectl, sources: list) -> None:
    """One pulsectl connection lifetime. Raises PulseError on disconnect."""
    error_counts: list[int] = [0] * len(sources)

    with pulsectl.Pulse("nativmix-peak-worker") as pulse:
        while True:
            levels: list[float] = []
            for i, src in enumerate(sources):
                if src is None or error_counts[i] >= _MAX_SOURCE_ERRORS:
                    levels.append(0.0)
                    continue
                try:
                    v = pulse.get_peak_sample(src, 0.05)
                    levels.append(float(v or 0.0))
                    error_counts[i] = 0  # reset on success
                except Exception:
                    # Source temporarily unavailable (suspended, renamed, etc.)
                    # — return 0.0 and keep going.
                    error_counts[i] += 1
                    levels.append(0.0)
            sys.stdout.write(json.dumps(levels) + "\n")
            sys.stdout.flush()


def main() -> None:
    line = sys.stdin.readline().strip()
    if not line:
        return

    cfg = json.loads(line)
    sources: list = cfg.get("sources", [])

    # Defer import so only this subprocess loads pulsectl/libpulse.
    import pulsectl  # noqa: PLC0415

    reconnects = 0
    while reconnects < _MAX_RECONNECTS:
        try:
            _run_session(pulsectl, sources)
            return  # clean exit — parent closed stdin
        except pulsectl.PulseError:
            # Transient disconnect (PipeWire restart, graph change, etc.).
            # Wait briefly and reconnect; do NOT exit so the parent keeps
            # receiving 0.0 peaks instead of treating this as a crash.
            reconnects += 1
            time.sleep(0.3)
        except Exception:
            break

    sys.exit(1)


if __name__ == "__main__":
    main()
