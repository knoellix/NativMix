# Design: Easy Effects coexistence — automatic stream hold (Phase 1)

Date: 2026-08-27  
Status: **Phase 1 Automatik implemented** (2026-08-27) — GUI / manual off later  
Related: `.cursor/plans/easy-effects-study.md`  
Code: `lib/nativmix/audio/easyeffects_hold.py`, wired in `lib/nativmix/audio/manager.py`

---

## Problem

NativMix **watches mapped apps** and wants to own their **destination**:

| Channel V-Sink | Intended destination |
|---|---|
| **on** | `NativMix_CH_*` |
| **off** | system **default / standard** sink (not left on EE or a random device) |

Easy Effects often moves the same apps onto `easyeffects_sink` → both reclaim → **ping-pong**.

Today without V-Sink, NM mostly sets stream volume/mute and does not always force default-sink routing; Phase 1 aligns reclaim rules for **both** modes under one EE-hold exception.

---

## Phase 1 — Automatik (no new GUI)

### Detection (per stream)

A playback stream is **held by Easy Effects** when its current sink matches known EE processing sinks, e.g.:

- `easyeffects_sink` / `easyeffects_*` playback sinks  
- Optional: discover EE virtual sinks via PipeWire (Arthur-style)

Decision key: *where does this sink-input live right now?*  
No EE socket/API required.

### Routing policy (per app / stream, not whole channel)

For each mapped playback stream:

1. **If held by EE** → **do not** move (neither to V-Sink nor to default sink).  
2. **Else if channel V-Sink on** → (re)route to `NativMix_CH_*` as today.  
3. **Else (V-Sink off)** → (re)route to the current **default/standard** hardware sink (same idea as evacuate-on-unmap; never into another `NativMix_*` sink).  
4. **Siblings** on the same channel are independent: one app can sit on EE while others stay on V-Sink or default.  
5. **Reclaim backoff:** if NM moves a stream and it reappears on an EE sink shortly after → treat as EE hold, stop fighting that stream (log). Avoid loops.

### Volume / mute while held by EE

Routing paused ≠ controls paused:

- **Volume** and **mute** still apply to the **stream** (sink-input) — control “before” EE on that playback stream.  
- Never write volume/mute to the shared `easyeffects_sink` device.  
- Apps on V-Sink: volume on null-sink, unity on stream (current rule); mute per-stream as today.  
- Apps on default sink (no V-Sink, not EE): stream volume/mute as today.

One channel fader/mute may update V-Sink gain **and/or** stream gain/mute for mixed membership on that channel.

### Out of scope (Phase 1)

- Per-channel / global “disable NM routing” UI (Phase 2)  
- Arthur-style global Routing Owner combo (optional later)  
- EE presets / plugin parameters / hardware→FX  
- Editing EE blocklist files  

---

## Phase 2 — Manual abschalten (later)

Because Phase 1 means NM **actively claims** destinations (V-Sink *or* default), a clean GUI switch is desirable:

- Per channel (or per app): **NM auto-routing off** — only volume/mute, never `move-sink-input`  
- Optional status: “held by Easy Effects” / “routing paused”  
- Optional global override  

Phase 2 is intentional follow-up, not blocked by Phase 1.

---

## Success criteria

- EE-enabled mapped app stays on EE; NM does not yank to V-Sink **or** default sink.  
- Same channel: non-EE apps still go to V-Sink or default per channel setting.  
- EE-held app still follows strip **volume + mute** via stream controls.  
- No volume/mute writes to shared `easyeffects_sink`.  
- Hold/reclaim decisions visible in debug logs.  
- Manual routing-off UI deferred to Phase 2.
