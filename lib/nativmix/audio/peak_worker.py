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

The worker loops continuously, sampling each source with a short timeout
so each cycle takes roughly len(non-null sources) * timeout seconds.

Exit codes
----------
0 — clean stop (stdin closed by parent)
1 — unrecoverable error (PulseDisconnected, bad config, …)
"""

from __future__ import annotations

import json
import sys

# Consecutive per-source errors before that source is skipped for the rest
# of the session.  Avoids tight loops when a monitor source disappears.
_MAX_SOURCE_ERRORS = 10


def main() -> None:
    line = sys.stdin.readline().strip()
    if not line:
        return

    cfg = json.loads(line)
    sources: list = cfg.get("sources", [])

    # Defer import: only this subprocess should ever import pulsectl
    import pulsectl  # noqa: PLC0415

    # Per-source consecutive error counter.  When a source exceeds the
    # threshold it is treated as None (returns 0.0) until the process restarts.
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
                    except pulsectl.PulseOperationFailed:
                        # Source temporarily unavailable (e.g. suspended when
                        # system volume is 0).  Return 0.0 and keep trying.
                        error_counts[i] += 1
                        levels.append(0.0)
                    except Exception:
                        error_counts[i] += 1
                        levels.append(0.0)
                sys.stdout.write(json.dumps(levels) + "\n")
                sys.stdout.flush()
    except pulsectl.PulseDisconnected:
        # PipeWire restarted or the connection was lost — exit cleanly so the
        # parent can restart us after its 2 s retry delay.
        sys.exit(1)
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
