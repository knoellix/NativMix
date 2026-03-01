# Maintainer: knoellix
pkgname=nativmix-git
pkgver=1.0.0
pkgrel=1
pkgdesc="Hardware-assisted volume mixer for PipeWire/PulseAudio with Arduino support"
arch=('any')
url="https://github.com/knoellix/nativmix"
license=('GPL-3.0-or-later')
depends=(
    'python'
    'python-pyqt6'
    'python-pulsectl'
    'python-pyserial'
    'python-setproctitle'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-setuptools'
    'python-wheel'
)
optdepends=(
    'kvantum: Plasma transparency and blur engine support'
    'arduino: Flash firmware to the hardware controller'
)
install=nativmix.install
source=()
sha256sums=()

prepare() {
    cd "$startdir"
    # Remove stale build artifacts to prevent installing an outdated wheel
    rm -rf build dist src/*.egg-info
}

build() {
    cd "$startdir"
    /usr/bin/python -m build --wheel --no-isolation
}

package() {
    cd "$startdir"

    # Install the Python wheel system-wide
    /usr/bin/python -m installer --destdir="$pkgdir" dist/*.whl

    # Application assets (icons used at runtime via paths.py)
    install -Dm644 assets/icon.png \
        "$pkgdir/usr/share/nativmix/assets/icon.png"
    install -Dm644 assets/icon.svg \
        "$pkgdir/usr/share/nativmix/assets/icon.svg"

    # Desktop entry
    install -Dm644 data/nativmix.desktop \
        "$pkgdir/usr/share/applications/nativmix.desktop"

    # Scalable icon (SVG) for icon themes
    install -Dm644 assets/icon.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/nativmix.svg"

    # Pixel icon (48x48 PNG fallback) for icon themes
    install -Dm644 assets/icon.png \
        "$pkgdir/usr/share/icons/hicolor/48x48/apps/nativmix.png"

    # License
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
