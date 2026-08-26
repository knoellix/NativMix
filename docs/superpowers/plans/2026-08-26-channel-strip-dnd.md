# Channel Strip DnD Reorder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persistable drag-and-drop reorder of mixer strips via profile `channel_order`, without changing stable channel IDs or V-Sink names (#28).

**Architecture:** Keep `channels[].index` as the stable id for audio/MIDI/V-Sink. Add `channel_order: list[int]` on each profile. GUI builds strips in that order. Drag handle = channel label + separator; disabled in compact mode. After drop, save order into the active profile.

**Tech Stack:** Python 3.10+, PyQt6, existing ConfigManager / ProfileManager, pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-channel-strip-dnd-design.md`

## Global Constraints

- Communicate with user in German; code/comments in English.
- No version bump until Task 5 (explicit user request for v1.0.19).
- Do not push remotes.
- Prefer fish-safe `git commit -m "..."` (no bash heredoc).
- Run `pytest -q` before calling the feature done.
- Never use `except Exception: pass`.

## File map

| File | Role |
| --- | --- |
| `lib/nativmix/utils/channel_order.py` | Pure helpers: normalize/repair order |
| `lib/nativmix/utils/config_manager.py` | Hold order in memory; get/set; sync on ensure/remove |
| `lib/nativmix/utils/profile_manager.py` | Persist `channel_order` on create/save |
| `lib/nativmix/gui/main_window.py` | DnD UI; rebuild by order; fix `_channels` id lookup |
| `lib/nativmix/main.py` | Pass order when saving profiles if needed |
| `tests/test_channel_order.py` | Unit tests for helpers + config/profile persistence |
| `CHANGELOG.md` + packaging | v1.0.19 release notes and version sync |

---

### Task 1: `normalize_channel_order` helper + tests

**Files:**
- Create: `lib/nativmix/utils/channel_order.py`
- Create: `tests/test_channel_order.py`

**Interfaces:**
- Produces: `normalize_channel_order(order: list[int] | None, channel_ids: list[int]) -> list[int]`
  - Keeps known ids in given relative order; drops unknowns; appends missing ids in `channel_ids` order.

- [ ] **Step 1: Write failing tests** in `tests/test_channel_order.py`:

```python
from nativmix.utils.channel_order import normalize_channel_order

def test_none_order_is_natural():
    assert normalize_channel_order(None, [0, 1, 2]) == [0, 1, 2]

def test_keeps_permutation():
    assert normalize_channel_order([2, 0, 1], [0, 1, 2]) == [2, 0, 1]

def test_drops_unknown_and_appends_missing():
    assert normalize_channel_order([9, 1, 0], [0, 1, 2]) == [1, 0, 2]

def test_empty_order_is_natural():
    assert normalize_channel_order([], [0, 1]) == [0, 1]
```

- [ ] **Step 2:** `pytest tests/test_channel_order.py -q` → FAIL (import)

- [ ] **Step 3: Implement** `lib/nativmix/utils/channel_order.py`:

```python
from __future__ import annotations

def normalize_channel_order(order: list[int] | None, channel_ids: list[int]) -> list[int]:
    known = set(channel_ids)
    if not order:
        return list(channel_ids)
    seen: set[int] = set()
    result: list[int] = []
    for cid in order:
        try:
            cid_i = int(cid)
        except (TypeError, ValueError):
            continue
        if cid_i in known and cid_i not in seen:
            result.append(cid_i)
            seen.add(cid_i)
    for cid in channel_ids:
        if cid not in seen:
            result.append(cid)
    return result
```

- [ ] **Step 4:** `pytest tests/test_channel_order.py -q` → PASS

- [ ] **Step 5: Commit** `feat: add normalize_channel_order helper for strip DnD.`

---

### Task 2: Config + profile persistence

**Files:**
- Modify: `lib/nativmix/utils/config_manager.py` (`apply_profile`, `_ensure_channels`, `remove_midi_channel`, new get/set)
- Modify: `lib/nativmix/utils/profile_manager.py` (`create`, `save_current`)
- Modify: `tests/conftest.py` (`make_profile` optional `channel_order`)
- Modify: `tests/test_channel_order.py` (config/profile round-trip tests)
- Modify: `lib/nativmix/main.py` and any `save_current` callers if signature changes

**Interfaces:**
- Consumes: `normalize_channel_order`
- Produces:
  - `ConfigManager.get_channel_order() -> list[int]`
  - `ConfigManager.set_channel_order(order: list[int]) -> None` (normalize, store in `self._data["channel_order"]`, emit `settings_changed` or a dedicated signal if one exists; prefer reusing `settings_changed` only if GUI already rebuilds on it — otherwise call GUI rebuild explicitly from DnD slot)
  - `apply_profile`: load `profile.get("channel_order")` via normalize against current channel indices
  - `ProfileManager.save_current(channels, channel_order: list[int] | None = None)` — when order given, write to profile
  - `ProfileManager.create(...)`: default `channel_order` = `list(range(channel_count))`
  - `_ensure_channels` / `remove_midi_channel` / `add_midi_channel`: keep order in sync (append new ids; drop removed; re-normalize after re-index)

**Critical:** After `remove_midi_channel`, channel indices are renumbered. Rebuild `channel_order` as natural order for remaining length (or map old→new carefully). Simplest correct approach: after re-index, `set_channel_order(list(range(len(channels))))` only if the removed id was in the middle and indices shifted — better: remap order by filtering removed index and decrementing ids `> removed`. Implement remap:

```python
def order_after_remove(order: list[int], removed: int) -> list[int]:
    out = []
    for cid in order:
        if cid == removed:
            continue
        out.append(cid - 1 if cid > removed else cid)
    return out
```

- [ ] **Step 1: Tests** for get/set + apply_profile + save_current round-trip (use tmp config + ProfileManager).

- [ ] **Step 2:** Implement config/profile wiring; update all `save_current(...)` call sites to pass `config.get_channel_order()`.

- [ ] **Step 3:** `pytest tests/test_channel_order.py tests/test_profile_manager.py -q` → PASS

- [ ] **Step 4: Commit** `feat: persist profile channel_order for strip reorder.`

---

### Task 3: GUI rebuild by order + fix id-keyed widget lookup

**Files:**
- Modify: `lib/nativmix/gui/main_window.py`

**Problem:** `_channels[i]` is used as if list index == channel id (`on_volumes_changed`, `on_channel_volume_changed`, `on_mute_state_changed`, `sync_sliders_from_config`). After display reorder this breaks.

**Interfaces:**
- Produces: `_channel_widget(channel_index: int) -> ChannelWidget | None`
- `_rebuild_channels` iterates `for i in self._config.get_channel_order():` (skip MIDI-in-USB as today)
- Keep `self._channels` as list in **display** order for layout iteration; lookups go through `_channel_widget`.

- [ ] **Step 1:** Add `_channel_widget` and replace id-indexed uses.

- [ ] **Step 2:** Change `_rebuild_channels` to follow `get_channel_order()`.

- [ ] **Step 3:** Manual sanity via existing tests + `pytest -q`.

- [ ] **Step 4: Commit** `fix: look up channel widgets by stable id; rebuild by channel_order.`

---

### Task 4: Drag-and-drop on label + separator

**Files:**
- Modify: `lib/nativmix/gui/main_window.py` (`ChannelWidget` + `MainWindow`)

**Behaviour:**
- Mime type e.g. `application/x-nativmix-channel-id` with channel id as text.
- Enable drag from `_ch_label` and `_sep` only when **not** compact.
- Double-click rename on label unchanged; start drag after Qt drag distance threshold.
- Accept drops on `ChannelWidget` or the channels row container; compute insert index from drop x; reorder list; `config.set_channel_order`; `profile_manager.save_current(...)`; `_rebuild_channels` (or reorder widgets in layout without full delete if easy — full rebuild is OK and simpler).
- Compact: `setAcceptDrops(False)` on handle / ignore mouse press for drag; stored order still used when leaving compact.
- Optional thin drop indicator: vertical line between strips (nice-to-have; skip if time — at least correct insert index).

- [ ] **Step 1: Implement DnD**

- [ ] **Step 2:** `pytest -q` → PASS

- [ ] **Step 3: Commit** `feat: drag-and-drop reorder channel strips via label/separator (#28).`

---

### Task 5: Changelog + version bump to 1.0.19 (no push)

**Files (all concrete `1.0.18` → `1.0.19` where current release is embedded):**
- `lib/nativmix/metadata.py`
- `pyproject.toml`
- `packaging/aur/PKGBUILD` + `packaging/aur/.SRCINFO`
- Root `.SRCINFO` (currently stale `1.0.4` → must become `1.0.19`)
- `packaging/OSC/nativmix.spec` + `packaging/OSC/_service` + `packaging/OSC/debian.changelog`
- `packaging/debian/changelog` (add 1.0.19 entry)
- `packaging/FLATPAK.md` example bundle name
- `README.md` / `README_DE.md` version badge line
- `CHANGELOG.md`: move Unreleased (V-Sink fix + DnD) under `## v1.0.19`
- Fedora/Suse specs stay `Version: 0` (OBS placeholder only)

- [ ] **Step 1:** Bump + changelog

- [ ] **Step 2:** `rg '1\.0\.18' --glob '!CHANGELOG.md' --glob '!tests/test_update_check.py'` → only historical/intentional leftovers

- [ ] **Step 3:** `pytest -q` → PASS

- [ ] **Step 4: Commit** `Release v1.0.19: channel strip DnD and V-Sink double-volume fix.`

- [ ] **Step 5:** Do **not** push. Report `git status` / ahead commits for the user.

---

## Spec coverage checklist

| Spec item | Task |
| --- | --- |
| Stable ids / V-Sink unchanged | 2–4 |
| `channel_order` in profile | 2 |
| Drag = label + sep | 4 |
| Compact = no DnD | 4 |
| Persist on drop | 2 + 4 |
| Add/remove sync | 2 |
| Profile switch loads order | 2 + 3 (`apply_profile` + rebuild) |
| Unit tests | 1–2 |
| Changelog / version | 5 |

## Self-review notes

- No adjacent-link in this plan.
- `save_current` signature change must update every caller in `main.py` / `main_window.py` / settings wiring.
- Widget lookup by id is mandatory before DnD ships.
