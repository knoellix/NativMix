#!/usr/bin/env python3
"""Build / merge GitHub Release notes for NativMix tag workflows.

Goal: the CHANGELOG section appears once. Each channel (AUR, Windows, Flatpak)
upserts only its own marked section so parallel jobs do not triple the notes.

Usage:
  release_notes.py extract VERSION          # print ## vVERSION bullets
  release_notes.py section VERSION CHANNEL  # print one channel's markdown
  release_notes.py merge VERSION CHANNEL [BODY_FILE|-]
      Read existing release body (file or stdin/- for empty), upsert Changelog
      + the channel section, print the merged body to stdout.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

MARKER = {
    "changelog": "<!-- nativmix:section:changelog -->",
    "aur": "<!-- nativmix:section:aur -->",
    "windows": "<!-- nativmix:section:windows -->",
    "flatpak": "<!-- nativmix:section:flatpak -->",
}

WIN_RE = re.compile(r"(?i)\b(windows|wasapi|win32|inno)\b")
FLATPAK_RE = re.compile(r"(?i)\bflatpak\b")


def _version_heading(version: str) -> str:
    v = version.lstrip("v")
    return f"## v{v}"


def extract_changelog_bullets(version: str) -> list[str]:
    """Return bullet lines under ``## vX.Y.Z`` (without the heading)."""
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = _version_heading(version)
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == heading)
    except StopIteration as exc:
        raise SystemExit(f"CHANGELOG.md has no section {heading!r}") from exc

    bullets: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.startswith("## "):
            break
        if ln.startswith("- "):
            bullets.append(ln)
    return bullets


def classify(bullets: list[str]) -> dict[str, list[str]]:
    linux: list[str] = []
    windows: list[str] = []
    flatpak: list[str] = []
    for b in bullets:
        is_win = bool(WIN_RE.search(b))
        is_fp = bool(FLATPAK_RE.search(b))
        if is_win and not is_fp:
            windows.append(b)
        elif is_fp and not is_win:
            flatpak.append(b)
        elif is_win and is_fp:
            windows.append(b)
            flatpak.append(b)
        else:
            linux.append(b)
    return {"linux": linux, "windows": windows, "flatpak": flatpak}


def _bullets_or_fallback(items: list[str], fallback: str) -> str:
    if items:
        return "\n".join(items)
    return fallback


def section_changelog(version: str, bullets: list[str]) -> str:
    v = version.lstrip("v")
    body = "\n".join(bullets) if bullets else "_No entries in CHANGELOG.md for this version._"
    return (
        f"{MARKER['changelog']}\n"
        f"## Changelog (v{v})\n\n"
        f"{body}\n"
        f"\nFull history: [CHANGELOG.md](https://github.com/knoellix/NativMix/blob/v{v}/CHANGELOG.md)\n"
    )


def section_aur(version: str, classified: dict[str, list[str]]) -> str:
    v = version.lstrip("v")
    linux = _bullets_or_fallback(
        classified["linux"],
        "_No Linux-only bullets tagged in this release; see Changelog above._",
    )
    return (
        f"{MARKER['aur']}\n"
        f"## Arch Linux (AUR)\n\n"
        f"```bash\n"
        f"paru -S nativmix\n"
        f"# or: yay -S nativmix\n"
        f"```\n\n"
        f"Package version tracks tag `v{v}`. After sync:\n\n"
        f"```bash\n"
        f"paru -Syu nativmix\n"
        f"```\n\n"
        f"### Linux highlights\n\n"
        f"{linux}\n"
    )


def section_windows(version: str, classified: dict[str, list[str]]) -> str:
    v = version.lstrip("v")
    win = _bullets_or_fallback(
        classified["windows"],
        "_No Windows-specific bullets in this release; "
        "general fixes in Changelog still apply on WASAPI where relevant._",
    )
    return (
        f"{MARKER['windows']}\n"
        f"## Windows\n\n"
        f"Download **`NativMix-{v}-Setup.exe`** from the assets below "
        f"(per-user install; optional system-wide).\n\n"
        f"### Windows notes\n\n"
        f"{win}\n"
    )


def section_flatpak(version: str, classified: dict[str, list[str]]) -> str:
    v = version.lstrip("v")
    fp = _bullets_or_fallback(
        classified["flatpak"],
        "_No Flatpak-specific bullets in this release; use the bundle below as a portable Linux fallback._",
    )
    return (
        f"{MARKER['flatpak']}\n"
        f"## Flatpak\n\n"
        f"Single-file bundle (not a Flathub remote — no `flatpak update` from GitHub):\n\n"
        f"```bash\n"
        f"flatpak install --user ./NativMix-{v}.flatpak\n"
        f"flatpak run net.knoellix.NativMix\n"
        f"```\n\n"
        f"See [packaging/FLATPAK.md](https://github.com/knoellix/NativMix/blob/v{v}/packaging/FLATPAK.md).\n\n"
        f"### Flatpak notes\n\n"
        f"{fp}\n"
    )


SECTION_BUILDERS = {
    "aur": section_aur,
    "windows": section_windows,
    "flatpak": section_flatpak,
}


def upsert(body: str, version: str, channel: str) -> str:
    """Rebuild Changelog + known channel sections from CHANGELOG.md.

    Any prior ``generate_release_notes`` / unmarked text is discarded. Channel
    sections that already existed (or the one being written now) are regenerated
    cleanly so parallel jobs never accumulate duplicate changelogs.
    """
    bullets = extract_changelog_bullets(version)
    classified = classify(bullets)
    order = ("aur", "windows", "flatpak")

    keep = {ch for ch in order if MARKER[ch] in body}
    keep.add(channel)

    parts = [section_changelog(version, bullets).rstrip()]
    for ch in order:
        if ch in keep:
            parts.append(SECTION_BUILDERS[ch](version, classified).rstrip())

    return "\n\n".join(parts).rstrip() + "\n"


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nativmix-release-notes",
    }


def apply_to_github(tag: str, version: str, channel: str, repo: str, token: str) -> None:
    """Create/update the GitHub Release body via API (no ``gh`` CLI required)."""
    import json
    import urllib.error
    import urllib.request

    api = f"https://api.github.com/repos/{repo}/releases"
    headers = _github_headers(token)

    def _req(method: str, url: str, payload: dict | None = None) -> tuple[int, dict | list | None]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = {"message": raw}
            return exc.code, body

    status, release = _req("GET", f"{api}/tags/{tag}")
    existing_body = ""
    release_id: int | None = None
    if status == 200 and isinstance(release, dict):
        existing_body = str(release.get("body") or "")
        release_id = int(release["id"])
    elif status == 404:
        # Parallel jobs may race create — retry GET on conflict.
        c_status, created = _req(
            "POST",
            api,
            {
                "tag_name": tag,
                "name": f"NativMix v{version.lstrip('v')}",
                "body": "Preparing release notes…",
                "draft": False,
                "prerelease": False,
            },
        )
        if c_status in (201, 200) and isinstance(created, dict):
            release_id = int(created["id"])
            existing_body = str(created.get("body") or "")
        else:
            status2, release2 = _req("GET", f"{api}/tags/{tag}")
            if status2 != 200 or not isinstance(release2, dict):
                raise SystemExit(f"Could not create/find release {tag}: {c_status} {created}")
            release_id = int(release2["id"])
            existing_body = str(release2.get("body") or "")
    else:
        raise SystemExit(f"Unexpected GET release status {status}: {release}")

    assert release_id is not None
    new_body = upsert(existing_body, version, channel)
    p_status, patched = _req(
        "PATCH",
        f"{api}/{release_id}",
        {"name": f"NativMix v{version.lstrip('v')}", "body": new_body},
    )
    if p_status not in (200, 201):
        raise SystemExit(f"Failed to patch release {tag}: {p_status} {patched}")
    print(f"Updated release {tag} section={channel}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "extract":
        if len(argv) != 3:
            print("usage: release_notes.py extract VERSION", file=sys.stderr)
            return 2
        for line in extract_changelog_bullets(argv[2]):
            print(line)
        return 0
    if cmd == "section":
        if len(argv) != 4 or argv[3] not in SECTION_BUILDERS:
            print("usage: release_notes.py section VERSION aur|windows|flatpak", file=sys.stderr)
            return 2
        version, channel = argv[2], argv[3]
        bullets = extract_changelog_bullets(version)
        print(SECTION_BUILDERS[channel](version, classify(bullets)), end="")
        return 0
    if cmd == "merge":
        if len(argv) != 5 or argv[3] not in SECTION_BUILDERS:
            print(
                "usage: release_notes.py merge VERSION aur|windows|flatpak BODY_FILE|-",
                file=sys.stderr,
            )
            return 2
        version, channel, src = argv[2], argv[3], argv[4]
        if src == "-":
            existing = sys.stdin.read()
        else:
            p = Path(src)
            existing = p.read_text(encoding="utf-8") if p.exists() else ""
        sys.stdout.write(upsert(existing, version, channel))
        return 0
    if cmd == "apply":
        if len(argv) != 5 or argv[4] not in SECTION_BUILDERS:
            print(
                "usage: release_notes.py apply TAG VERSION aur|windows|flatpak",
                file=sys.stderr,
            )
            return 2
        import os

        tag, version, channel = argv[2], argv[3], argv[4]
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        repo = os.environ.get("GITHUB_REPOSITORY", "knoellix/NativMix")
        if not token:
            print("GITHUB_TOKEN / GH_TOKEN required", file=sys.stderr)
            return 2
        apply_to_github(tag, version, channel, repo, token)
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
