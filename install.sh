#!/bin/bash
# Install Galaxy Book Settings: the galaxybookd system service (needs sudo),
# the GTK app, and the GNOME Shell extension for the current user.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/usr/local
UUID=galaxybook-settings@galaxybook.github.io

if [[ $EUID -eq 0 ]]; then
    echo "Run as your normal user; the script calls sudo for system files." >&2
    exit 1
fi

if [[ ! -d /sys/bus/platform/drivers/samsung-galaxybook ]]; then
    echo "warning: samsung-galaxybook kernel driver is not loaded; the service will not start." >&2
fi

echo "==> Installing galaxybookd service (sudo)"
sudo install -Dm755 "$HERE/daemon/galaxybookd.py" /usr/libexec/galaxybook-settings/galaxybookd
sudo install -Dm644 "$HERE/data/io.github.galaxybook.Settings1.conf" /usr/share/dbus-1/system.d/io.github.galaxybook.Settings1.conf
sudo install -Dm644 "$HERE/data/io.github.galaxybook.Settings1.service" /usr/share/dbus-1/system-services/io.github.galaxybook.Settings1.service
sudo install -Dm644 "$HERE/data/io.github.galaxybook.settings.policy" /usr/share/polkit-1/actions/io.github.galaxybook.settings.policy
sudo install -Dm644 "$HERE/data/galaxybookd.service" /etc/systemd/system/galaxybookd.service
sudo systemctl daemon-reload
sudo systemctl reload dbus 2>/dev/null || true
sudo systemctl enable --now galaxybookd.service

echo "==> Installing Galaxy Book Settings app (sudo)"
sudo install -Dm755 "$HERE/app/galaxybook-settings" "$PREFIX/bin/galaxybook-settings"
sudo install -Dm644 "$HERE/data/io.github.galaxybook.Settings.desktop" "$PREFIX/share/applications/io.github.galaxybook.Settings.desktop"
sudo update-desktop-database "$PREFIX/share/applications" 2>/dev/null || true

echo "==> Installing GNOME Shell extension for $USER"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"
cp "$HERE/extension/$UUID/"{metadata.json,extension.js} "$EXT_DIR/"
gnome-extensions enable "$UUID" 2>/dev/null || true

echo
systemctl --no-pager --lines=3 status galaxybookd.service || true
echo
echo "Done. Open 'Galaxy Book Settings' from the app grid."
echo "The Quick Settings tile appears after you log out and back in (Wayland can't reload GNOME Shell)."
