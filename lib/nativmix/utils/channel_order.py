from __future__ import annotations


def normalize_channel_order(order: list[int] | None, channel_ids: list[int]) -> list[int]:
    """Return a valid left-to-right display order for *channel_ids*.

    Keeps known ids from *order* in relative order, drops unknowns / duplicates,
    then appends any missing ids in *channel_ids* order. ``None`` or empty
    *order* yields the natural *channel_ids* sequence.
    """
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


def order_after_remove(order: list[int], removed: int) -> list[int]:
    """Remap display order after channel *removed* is deleted and higher ids shift down."""
    out: list[int] = []
    for cid in order:
        if cid == removed:
            continue
        out.append(cid - 1 if cid > removed else cid)
    return out
