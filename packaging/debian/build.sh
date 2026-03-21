#!/usr/bin/env bash
# =============================================================================
# build.sh — NativMix Debian build script
# Version : 1.0.7
#
# Usage   : Run from packaging/debian/
#               bash build.sh
#
# Why build in /tmp?
#   The source tree lives on a mounted volume (/mnt/…) that uses a filesystem
#   (NTFS / exFAT / CIFS / …) which does not support POSIX ownership at the
#   kernel level.  dh_fixperms calls "chown 0:0" on every staged file via
#   xargs; even under fakeroot this syscall is ultimately rejected by the
#   kernel for those filesystem types, causing xargs exit code 123.
#   Building on /tmp (tmpfs / ext4) avoids the problem entirely.
#
# What it does:
#   1. Creates an isolated build tree in /tmp.
#   2. Rsyncs the project source there (excluding .git and build artifacts).
#   3. Copies the debian/ control files into the build tree.
#   4. Runs dpkg-buildpackage (binary-only, unsigned) inside /tmp.
#   5. Moves all artifacts (.deb / .changes / .buildinfo) into
#      packaging/debian/dist/ on the original volume.
#   6. Removes the entire /tmp build tree on exit (success or failure).
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Path resolution — no hardcoded absolute paths
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"

CHANGELOG="$SCRIPT_DIR/changelog"
PKG_NAME="$(dpkg-parsechangelog -l "$CHANGELOG" -S Source)"
PKG_VERSION="$(dpkg-parsechangelog -l "$CHANGELOG" -S Version)"

echo "==> Building  : $PKG_NAME  $PKG_VERSION"
echo "    Source    : $PROJECT_ROOT"
echo "    Output    : $DIST_DIR"

# ---------------------------------------------------------------------------
# Isolated build tree in /tmp
# ---------------------------------------------------------------------------

BUILD_ROOT="$(mktemp -d /tmp/nativmix-build-XXXXXX)"
BUILD_SRC="$BUILD_ROOT/src"

echo "    Build dir : $BUILD_ROOT"
echo ""

# ---------------------------------------------------------------------------
# Cleanup — registered before anything is created so it also fires on error
# ---------------------------------------------------------------------------

cleanup() {
    local exit_code=$?
    echo ""
    echo "==> Cleaning up $BUILD_ROOT ..."
    rm -rf "$BUILD_ROOT"
    echo "==> Cleanup complete."
    exit "$exit_code"
}

trap cleanup EXIT

# ---------------------------------------------------------------------------
# Copy source into the build tree
# ---------------------------------------------------------------------------

echo "==> Copying source to build tree ..."

# rsync is preferred: it skips the same inode tricks that cp -a uses and
# gives us fine-grained exclude control.
# Suggestion: if rsync is not available, replace with:
#   cp -a "$PROJECT_ROOT" "$BUILD_SRC"
#   then manually remove excluded paths afterwards.
rsync -a \
    --exclude='.git' \
    --exclude='.pybuild' \
    --exclude='*.egg-info' \
    --exclude='packaging/debian/dist' \
    --exclude='packaging/debian/build.sh' \
    --exclude='packaging/aur/src' \
    "$PROJECT_ROOT/" "$BUILD_SRC/"

# Place the debian/ control directory at the build-tree root.
# This must be a real copy (not a symlink) because fakeroot does not
# track ownership correctly through directory symlinks.
cp -rp "$SCRIPT_DIR" "$BUILD_SRC/debian"

# ---------------------------------------------------------------------------
# Fix debian/ permissions
# ---------------------------------------------------------------------------
# The source volume uses a filesystem (NTFS / exFAT / CIFS) that assigns
# 0777 to every file.  cp -rp preserves those permissions, which causes
# debhelper to misinterpret plain config files (*.install, control,
# changelog, …) as executable scripts and attempt to run them.
#
# Strategy: normalise everything to 644/755, then restore 755 only for the
# files that debhelper and dpkg genuinely require to be executable.

# All regular files → 644 (read/write owner, read-only everyone else)
find "$BUILD_SRC/debian" -type f -exec chmod 644 {} +

# All directories → 755
find "$BUILD_SRC/debian" -type d -exec chmod 755 {} +

# debian/rules is called directly by dpkg-buildpackage — must be executable
chmod 755 "$BUILD_SRC/debian/rules"

# Maintainer scripts (postinst, preinst, postrm, prerm, config) are executed
# by dpkg during install/remove — must be executable.
for _script in \
    "$BUILD_SRC/debian"/*.postinst \
    "$BUILD_SRC/debian"/*.preinst \
    "$BUILD_SRC/debian"/*.postrm \
    "$BUILD_SRC/debian"/*.prerm \
    "$BUILD_SRC/debian"/*.config
do
    [ -f "$_script" ] && chmod 755 "$_script"
done
unset _script

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

echo "==> Running dpkg-buildpackage in $BUILD_SRC ..."
echo ""
cd "$BUILD_SRC"
dpkg-buildpackage -us -uc -b

# ---------------------------------------------------------------------------
# Collect artifacts
# ---------------------------------------------------------------------------
# dpkg-buildpackage places artifacts one level above the source directory,
# i.e. directly in $BUILD_ROOT.

echo ""
echo "==> Moving artifacts to $DIST_DIR"
mkdir -p "$DIST_DIR"

shopt -s nullglob
deb_files=(      "$BUILD_ROOT"/*"${PKG_VERSION}"*.deb      )
changes_files=(  "$BUILD_ROOT"/*"${PKG_VERSION}"*.changes  )
buildinfo_files=("$BUILD_ROOT"/*"${PKG_VERSION}"*.buildinfo)
shopt -u nullglob

artifact_count=0
for f in "${deb_files[@]}" "${changes_files[@]}" "${buildinfo_files[@]}"; do
    echo "    -> $(basename "$f")"
    mv "$f" "$DIST_DIR/"
    (( artifact_count++ )) || true
done

if [[ $artifact_count -eq 0 ]]; then
    echo "WARNING: No artifacts found in $BUILD_ROOT — the build may have failed silently."
else
    echo "    $artifact_count artifact(s) moved."
fi

# ---------------------------------------------------------------------------
# Install — force-reinstall the freshly built .deb
# ---------------------------------------------------------------------------

shopt -s nullglob
installed_debs=("$DIST_DIR"/*"${PKG_VERSION}"*.deb)
shopt -u nullglob

if [[ ${#installed_debs[@]} -eq 0 ]]; then
    echo "WARNING: No .deb found in $DIST_DIR — skipping install step."
else
    echo "==> Installing: $(basename "${installed_debs[0]}") ..."
    sudo dpkg -i --force-overwrite "${installed_debs[@]}"
    echo "==> Install complete."
fi

# ---------------------------------------------------------------------------
# Done  (cleanup() fires automatically via trap and removes $BUILD_ROOT)
# ---------------------------------------------------------------------------
echo ""
echo "==> Build finished successfully."
echo "    Packages are in: $DIST_DIR"
