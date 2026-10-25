#!/bin/bash
# Install Galaxy Book Settings: the galaxybookd system service (needs sudo), the
# galaxybudsd and galaxyphoned user services, the GTK app, and the GNOME Shell
# extension for the current user.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/usr/local
UUID=galaxybook-settings@jcarmog.github.io

if [[ $EUID -eq 0 ]]; then
    echo "Run as your normal user; the script calls sudo for system files." >&2
    exit 1
fi

if [[ ! -d /sys/bus/platform/drivers/samsung-galaxybook ]]; then
    echo "warning: samsung-galaxybook kernel driver is not loaded; the service will not start." >&2
fi

# Remove files from before the ID moved from io.github.galaxybook to io.github.jcarmog.GalaxyBook
OLD_UUID=galaxybook-settings@galaxybook.github.io
gnome-extensions disable "$OLD_UUID" 2>/dev/null || true
rm -rf "$HOME/.local/share/gnome-shell/extensions/$OLD_UUID"
sudo rm -f /usr/share/dbus-1/services/io.github.galaxybook.Buds1.service \
    /usr/share/dbus-1/services/io.github.galaxybook.Phone1.service \
    /usr/share/dbus-1/system.d/io.github.galaxybook.Settings1.conf \
    /usr/share/dbus-1/system-services/io.github.galaxybook.Settings1.service \
    /usr/share/polkit-1/actions/io.github.galaxybook.settings.policy \
    "$PREFIX/share/applications/io.github.galaxybook.Settings.desktop"

echo "==> Installing galaxybookd service (sudo)"
sudo install -Dm755 "$HERE/daemon/galaxybookd.py" /usr/libexec/galaxybook-settings/galaxybookd
sudo install -Dm644 "$HERE/data/io.github.jcarmog.GalaxyBook.Settings1.conf" /usr/share/dbus-1/system.d/io.github.jcarmog.GalaxyBook.Settings1.conf
sudo install -Dm644 "$HERE/data/io.github.jcarmog.GalaxyBook.Settings1.service" /usr/share/dbus-1/system-services/io.github.jcarmog.GalaxyBook.Settings1.service
sudo install -Dm644 "$HERE/data/io.github.jcarmog.galaxybook.settings.policy" /usr/share/polkit-1/actions/io.github.jcarmog.galaxybook.settings.policy
sudo install -Dm644 "$HERE/data/galaxybookd.service" /etc/systemd/system/galaxybookd.service
sudo systemctl daemon-reload
sudo systemctl reload dbus 2>/dev/null || true
sudo systemctl enable --now galaxybookd.service

echo "==> Installing galaxybudsd user service (sudo)"
sudo install -Dm755 "$HERE/daemon/galaxybudsd.py" /usr/libexec/galaxybook-settings/galaxybudsd
sudo install -Dm644 "$HERE/data/io.github.jcarmog.GalaxyBook.Buds1.service" /usr/share/dbus-1/services/io.github.jcarmog.GalaxyBook.Buds1.service
sudo install -Dm644 "$HERE/data/galaxybudsd.service" /usr/lib/systemd/user/galaxybudsd.service
systemctl --user daemon-reload
systemctl --user enable galaxybudsd.service
systemctl --user restart galaxybudsd.service

echo "==> Installing galaxyphoned user service (sudo)"
sudo install -Dm755 "$HERE/daemon/galaxyphoned.py" /usr/libexec/galaxybook-settings/galaxyphoned
sudo install -Dm644 "$HERE/data/io.github.jcarmog.GalaxyBook.Phone1.service" /usr/share/dbus-1/services/io.github.jcarmog.GalaxyBook.Phone1.service
sudo install -Dm644 "$HERE/data/galaxyphoned.service" /usr/lib/systemd/user/galaxyphoned.service
# The user service doesn't see the shell's PATH, so point it at an SDK adb/scrcpy outside the system dirs
DROPIN="$HOME/.config/systemd/user/galaxyphoned.service.d/tools.conf"
rm -f "$DROPIN"
for tool in adb scrcpy; do
    path="$(command -v "$tool" || true)"
    if [[ -n "$path" && "$path" != /usr/bin/* && "$path" != /bin/* ]]; then
        mkdir -p "$(dirname "$DROPIN")"
        [[ -f "$DROPIN" ]] || echo "[Service]" > "$DROPIN"
        echo "Environment=${tool^^}=$(readlink -f "$path")" >> "$DROPIN"
    fi
done
systemctl --user daemon-reload
systemctl --user enable galaxyphoned.service
systemctl --user restart galaxyphoned.service
install -Dm755 "$HERE/data/send-to-galaxy-phone" "$HOME/.local/share/nautilus/scripts/Send to Galaxy phone"

echo "==> Installing Galaxy Book Settings app (sudo)"
sudo install -Dm755 "$HERE/app/galaxybook-settings" "$PREFIX/bin/galaxybook-settings"
sudo install -Dm644 "$HERE/data/io.github.jcarmog.GalaxyBook.Settings.desktop" "$PREFIX/share/applications/io.github.jcarmog.GalaxyBook.Settings.desktop"
sudo update-desktop-database "$PREFIX/share/applications" 2>/dev/null || true

echo "==> Installing GNOME Shell extension for $USER"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
rm -rf "$EXT_DIR"
mkdir -p "$EXT_DIR"
cp "$HERE/extension/$UUID/"{metadata.json,extension.js} "$EXT_DIR/"
gnome-extensions enable "$UUID" 2>/dev/null || true

echo
systemctl --no-pager --lines=3 status galaxybookd.service || true
systemctl --user --no-pager --lines=3 status galaxybudsd.service || true
systemctl --user --no-pager --lines=3 status galaxyphoned.service || true
echo
echo "Done. Open 'Galaxy Book Settings' from the app grid."
echo "The Quick Settings tile appears after you log out and back in (Wayland can't reload GNOME Shell)."
