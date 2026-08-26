# Design: Channel strip drag-and-drop reorder (#28)

Date: 2026-08-26  
Status: approved for planning  
Issue: https://github.com/knoelliX/NativMix/issues/28

## Goal

Users can reorder mixer channel strips in the GUI so on-screen order matches physical MIDI/Arduino layout, without deleting and recreating mappings.

## Decisions

| Topic | Choice |
| --- | --- |
| What moves with a strip | Entire strip: apps, label, V-Sink flag, mute, volume, USB/`hardware_id`, MIDI bindings |
| Persistence | Saved in the active **profile** |
| Drag handle | Channel designation (`_ch_label`) + separator (`_sep`) under it |
| Compact mode | **No** drag-and-drop (handle inactive / unavailable) |
| Data model | Stable channel IDs + separate `channel_order` (not permuting V-Sink names) |

## Non-goals (this feature)

- Adjacent-channel volume link (later; uses visual neighbours after order exists)
- MIDI Multi-Learn / Easy Effects ownership changes
- Reordering only for the current session without save

## Architecture

### Stable identity

- Each channel keeps a stable integer id (`channels[].index`, as today: `0…n-1`).
- Audio, config APIs, V-Sink names (`NativMix_CH_<id>`), MIDI routing, mute, and volume keep using this id.
- Reorder never renames V-Sinks and never remaps stream volume ownership by display slot.

### Display order

- Profile gains `channel_order: list[int]` — permutation of channel ids for GUI left→right.
- Missing / empty / invalid list → treat as `[0, 1, …, n-1]`.
- On drop: write the new list into the profile and persist.
- Default channel labels (`CH 3`, `MIDI 3`) stay tied to **id**. Custom labels move with the strip because they live on the channel record.

### Channel add/remove

- Add channel: append new id to `channel_order` (end of strip row).
- Remove channel: drop that id from `channel_order`.
- Repair: if order contains unknown ids or misses existing ids, rebuild a valid list (known ids in previous relative order, then any missing ids appended).

### Profile switch

- Load the target profile’s `channel_order` (or default) and rebuild / re-layout the mixer strips accordingly.

## GUI behaviour

- Drag source: only the label + horizontal separator region on `ChannelWidget` (not the fader, mute, app list, or toggles).
- Cursor over handle: size/move affordance; rename via double-click on the label remains.
- Start drag after a small movement threshold so rename and drag do not fight.
- Drop target: horizontal strip row with a clear insert indicator between columns.
- After drop: update `channel_order`, persist profile, re-layout widgets (reparent or short rebuild). No PipeWire remount required because ids are unchanged.
- Applies equally to USB, hybrid, and MIDI strips in the normal (non-compact) mixer.
- Compact mode: DnD disabled; stored order still applies when leaving compact mode.

## Backend / audio

- `PipeWireManager` and workers continue to key by channel id.
- No volume or mute change on reorder.
- No V-Sink recreate on reorder.

## Config / profiles

- Store `channel_order` on the profile JSON next to `channels`.
- Config/profile helpers: get/set order, normalize/repair, keep order in sync on channel count changes.
- Migration: no schema version bump required if absence means natural order; document the field for new writes.

## Testing

- Unit: read/write `channel_order`; default when absent; repair corrupt lists; add/remove channel updates order.
- Unit/GUI-light: display order vs stable id (label id vs position).
- No hardware required for CI.

## Documentation / release notes

- `CHANGELOG.md` under Unreleased when implemented.
- Close #28 after the feature ships in a release (or when merged if that is the project habit).

## Out of scope follow-ups

- Adjacent-channel link UI between visual neighbours (depends on this order model).
- Optional: show physical slot hints in UI (not required for v1).
