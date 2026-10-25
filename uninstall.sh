#!/bin/bash
# Remove everything install.sh put in place. Saved state in /var/lib is kept unless --purge.
set -euo pipefail

UUID=galaxybook-settings@jcarmog.github.io

gnome-extensions disable "$UUID" 2>/dev/null || true
rm -rf "$HOME/.local/share/gnome-shell/extensions/$UUID"

systemctl --user disable --now galaxybudsd.service 2>/dev/null || true
systemctl --user disable --now galaxyphoned.service 2>/dev/null || true
rm -rf "$HOME/.config/systemd/user/galaxyphoned.service.d"
rm -f "$HOME/.local/share/nautilus/scripts/Send to Galaxy phone"
sudo systemctl disable --now galaxybookd.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/galaxybookd.service \
    /usr/libexec/galaxybook-settings/galaxybookd \
    /usr/libexec/galaxybook-settings/galaxybudsd \
    /usr/lib/systemd/user/galaxybudsd.service \
    /usr/share/dbus-1/services/io.github.jcarmog.GalaxyBook.Buds1.service \
    /usr/libexec/galaxybook-settings/galaxyphoned \
    /usr/lib/systemd/user/galaxyphoned.service \
    /usr/share/dbus-1/services/io.github.jcarmog.GalaxyBook.Phone1.service \
    /usr/share/dbus-1/system.d/io.github.jcarmog.GalaxyBook.Settings1.conf \
    /usr/share/dbus-1/system-services/io.github.jcarmog.GalaxyBook.Settings1.service \
    /usr/share/polkit-1/actions/io.github.jcarmog.galaxybook.settings.policy \
    /usr/local/bin/galaxybook-settings \
    /usr/local/share/applications/io.github.jcarmog.GalaxyBook.Settings.desktop
sudo rmdir /usr/libexec/galaxybook-settings 2>/dev/null || true
[[ "${1:-}" == "--purge" ]] && sudo rm -rf /var/lib/galaxybook-settings
sudo systemctl daemon-reload
systemctl --user daemon-reload
echo "Galaxy Book Settings removed."
