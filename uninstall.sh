#!/bin/bash
# Remove everything install.sh put in place. Saved state in /var/lib is kept unless --purge.
set -euo pipefail

UUID=galaxybook-settings@galaxybook.github.io

gnome-extensions disable "$UUID" 2>/dev/null || true
rm -rf "$HOME/.local/share/gnome-shell/extensions/$UUID"

sudo systemctl disable --now galaxybookd.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/galaxybookd.service \
    /usr/libexec/galaxybook-settings/galaxybookd \
    /usr/share/dbus-1/system.d/io.github.galaxybook.Settings1.conf \
    /usr/share/dbus-1/system-services/io.github.galaxybook.Settings1.service \
    /usr/share/polkit-1/actions/io.github.galaxybook.settings.policy \
    /usr/local/bin/galaxybook-settings \
    /usr/local/share/applications/io.github.galaxybook.Settings.desktop
sudo rmdir /usr/libexec/galaxybook-settings 2>/dev/null || true
[[ "${1:-}" == "--purge" ]] && sudo rm -rf /var/lib/galaxybook-settings
sudo systemctl daemon-reload
echo "Galaxy Book Settings removed."
