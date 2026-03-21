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
Per-source errors (source suspended, unavailable) are counted per source.
After _MAX_SOURCE_ERRORS consecutive failures the source is silenced for
this session (returns 0.0 without calling get_peak_sample) so a temporarily
unavailable monitor source does not cause the process to exit.

Session-level errors (PulseDisconnected — PipeWire restart) cause a clean
exit with code 1 so the parent can restart this worker after a short delay.
The parent limits total restarts to _MAX_CRASHES to avoid tight loops.
"""

from __future__ import annotations

import json
import sys

_MAX_SOURCE_ERRORS = 15


def main() -> None:
    line = sys.stdin.readline().strip()
    if not line:
        return

    cfg = json.loads(line)
    sources: list = cfg.get("sources", [])

    # Defer import so only this subprocess loads pulsectl / libpulse.
    import pulsectl  # noqa: PLC0415

    error_counts: list[int] = [0] * len(sources)

    try:
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
                        # Source temporarily unavailable (suspended, renamed…)
                        # Return 0.0 and keep going — do NOT exit.
                        error_counts[i] += 1
                        levels.append(0.0)
                sys.stdout.write(json.dumps(levels) + "\n")
                sys.stdout.flush()
    except Exception:
        # Session-level error (PulseDisconnected, etc.) — exit cleanly so
        # the parent can restart us after its 1 s retry delay.
        sys.exit(1)


if __name__ == "__main__":
    main()
