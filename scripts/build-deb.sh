#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-0.1.1}"
ARCHITECTURE="${2:-amd64}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build"
PKG_DIR="${BUILD_DIR}/mint-dictate_${VERSION}_${ARCHITECTURE}"
DEB_PATH="${ROOT_DIR}/dist/mint-dictate_${VERSION}_${ARCHITECTURE}.deb"

rm -rf "${PKG_DIR}"
mkdir -p "${PKG_DIR}/DEBIAN"
mkdir -p "${PKG_DIR}/opt/mint-dictate"
mkdir -p "${PKG_DIR}/usr/bin"
mkdir -p "${PKG_DIR}/usr/lib/mint-dictate"
mkdir -p "${PKG_DIR}/usr/lib/systemd/user"
mkdir -p "${PKG_DIR}/usr/share/applications"
mkdir -p "${ROOT_DIR}/dist"

install -m 0644 "${ROOT_DIR}/mint_dictate.py" "${PKG_DIR}/opt/mint-dictate/mint_dictate.py"
install -m 0644 "${ROOT_DIR}/requirements.txt" "${PKG_DIR}/opt/mint-dictate/requirements.txt"
install -m 0644 "${ROOT_DIR}/config.example.json" "${PKG_DIR}/opt/mint-dictate/config.example.json"
install -m 0644 "${ROOT_DIR}/LICENSE" "${PKG_DIR}/opt/mint-dictate/LICENSE"

install -m 0755 "${ROOT_DIR}/packaging/mint-dictate" "${PKG_DIR}/usr/bin/mint-dictate"
install -m 0755 "${ROOT_DIR}/packaging/mint-dictate-setup" "${PKG_DIR}/usr/bin/mint-dictate-setup"
install -m 0755 "${ROOT_DIR}/packaging/mint-dictate-repair-local-model" "${PKG_DIR}/usr/bin/mint-dictate-repair-local-model"
install -m 0755 "${ROOT_DIR}/packaging/install-python-deps" "${PKG_DIR}/usr/lib/mint-dictate/install-python-deps"
install -m 0644 "${ROOT_DIR}/packaging/mint-dictate.service" "${PKG_DIR}/usr/lib/systemd/user/mint-dictate.service"
install -m 0644 "${ROOT_DIR}/packaging/mint-dictate.desktop" "${PKG_DIR}/usr/share/applications/mint-dictate.desktop"
install -m 0755 "${ROOT_DIR}/packaging/postinst" "${PKG_DIR}/DEBIAN/postinst"

cat > "${PKG_DIR}/DEBIAN/control" <<CONTROL
Package: mint-dictate
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCHITECTURE}
Maintainer: Olaf Weller
Depends: python3, python3-venv, python3-dev, build-essential, python3-gi, gir1.2-ayatanaappindicator3-0.1, xdotool, libnotify-bin, xclip, playerctl, libevdev-dev, libportaudio2, libsndfile1, ca-certificates
Homepage: https://github.com/olafweller/mint-dictate
Description: Desktop dictation for Linux Mint
 Mint Dictate is a Linux Mint X11 tray app for desktop dictation using
 OpenAI speech-to-text or local Parakeet speech-to-text.
CONTROL

dpkg-deb --root-owner-group --build "${PKG_DIR}" "${DEB_PATH}"
echo "${DEB_PATH}"
