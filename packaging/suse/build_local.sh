#!/bin/bash

# Always run from the repo root regardless of where this script is called from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/../.." || { echo "Error: Could not cd to repo root"; exit 1; }

# Configuration - read version from pyproject.toml to avoid hardcoding
APP_VERSION="$(grep -m1 '^version' "${SCRIPT_DIR}/../../pyproject.toml" | sed 's/.*"\(.*\)".*/\1/')"
APP_RELEASE="1"
SPEC_FILE="packaging/suse/nativmix.spec"
RPM_ROOT="$HOME/rpmbuild"
SOURCES_DIR="${RPM_ROOT}/SOURCES"
SPECS_DIR="${RPM_ROOT}/SPECS"
RPMS_DIR="${RPM_ROOT}/RPMS"
MIDO_URL="https://files.pythonhosted.org/packages/source/m/mido/mido-1.3.2.tar.gz"

echo "Starting automated RPM build for NativMix v${APP_VERSION}-${APP_RELEASE}..."

# 0. Cleanup build artifacts to avoid cross-contamination
echo "--> Cleaning up old build artifacts..."
rm -rf build/ dist/ *.egg-info
rm -rf "${RPM_ROOT}/BUILD/"*
rm -rf "${RPM_ROOT}/BUILDROOT/"*

# 1. Create the required rpmbuild directory structure
mkdir -p "${SOURCES_DIR}" "${SPECS_DIR}" "${RPM_ROOT}/BUILD" "${RPMS_DIR}" "${RPM_ROOT}/SRPMS"

# 2. Anti-Hardcoding: Normalize Shebangs
echo "--> Normalizing Python shebangs in lib/nativmix/..."
# This ensures no local .venv paths from the development environment leak into the RPM.
find lib/nativmix/ -type f -name "*.py" -exec sed -i '1s|#!.*python.*|#!/usr/bin/env python3|' {} +

# 3. Export the current directory into a tarball (including uncommitted changes)
echo "--> Generating source tarball from current directory..."
# We use tar instead of git archive to ensure uncommitted changes (like version bumps) are included.
# We explicitly exclude .venv to prevent local environment leakage.
tar --exclude='.git' --exclude='.venv' --exclude='rpmbuild' --exclude='pkg' --exclude='dist' --exclude='build' \
    -czf "${SOURCES_DIR}/nativmix-${APP_VERSION}.tar.gz" \
    --transform "s|^|NativMix-${APP_VERSION}/|" .

# 4. Fetch external dependencies (mido)
echo "--> Fetching external dependency: mido..."
curl -L --silent --show-error "${MIDO_URL}" -o "${SOURCES_DIR}/mido-1.3.2.tar.gz"

# 5. Copy auxiliary source files (udev rules)
echo "--> Copying auxiliary source files..."
UDEV_RULE="data/udev/99-nativmix-arduino.rules"
if [ -f "$UDEV_RULE" ]; then
    cp "$UDEV_RULE" "${SOURCES_DIR}/"
else
    echo "Warning: $UDEV_RULE not found. Continuing anyway..."
fi

# 6. Inject version/release into spec and deploy (spec uses Version: 0 for OBS)
echo "--> Deploying spec file with version ${APP_VERSION}-${APP_RELEASE}..."
sed -e "s/^Version:.*/Version:        ${APP_VERSION}/" \
    -e "s/^Release:.*/Release:        ${APP_RELEASE}/" \
    "${SPEC_FILE}" > "${SPECS_DIR}/nativmix.spec"

# 7. Execute the RPM build process
echo "--> Building the RPM package..."
rpmbuild -ba "${SPECS_DIR}/nativmix.spec"

# 8. Check if the build was successful and provide installation option
if [ $? -eq 0 ]; then
    RPM_FILE="${RPMS_DIR}/noarch/nativmix-${APP_VERSION}-${APP_RELEASE}.noarch.rpm"
    echo "========================================="
    echo "Success! The RPM package has been built."
    echo "Location: ${RPM_FILE}"
    echo "========================================="
    
    read -p "Do you want to install the new version now? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # Use refined zypper command for local unsigned packages
        sudo zypper --no-gpg-checks install --allow-unsigned-rpm "${RPM_FILE}"
    fi
else
    echo "Error: RPM build failed. Please check the output above."
    exit 1
fi