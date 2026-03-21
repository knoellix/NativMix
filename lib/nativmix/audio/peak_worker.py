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
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    line = sys.stdin.readline().strip()
    if not line:
        return

    cfg = json.loads(line)
    sources: list = cfg.get("sources", [])

    # Defer import: only this subprocess should ever import pulsectl
    import pulsectl  # noqa: PLC0415

    try:
        with pulsectl.Pulse("nativmix-peak-worker") as pulse:
            while True:
                levels: list[float] = []
                for src in sources:
                    if src is None:
                        levels.append(0.0)
                    else:
                        try:
                            v = pulse.get_peak_sample(src, 0.05)
                            levels.append(float(v or 0.0))
                        except Exception:
                            levels.append(0.0)
                sys.stdout.write(json.dumps(levels) + "\n")
                sys.stdout.flush()
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    main()
