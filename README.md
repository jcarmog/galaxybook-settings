# Galaxy Book Settings for Linux

A Linux replacement for the hardware parts of Samsung Settings on Windows, built for
Samsung Galaxy Book laptops on GNOME. It includes:

- **galaxybookd**, a small root D-Bus service (`io.github.galaxybook.Settings1`) that wraps the
  upstream `samsung-galaxybook` kernel driver. It checks polkit before each change and restores the battery limit at boot.
- **Galaxy Book Settings**, a GTK4/libadwaita app.
- **A GNOME Shell extension** that adds a Quick Settings tile and shows a panel icon while the camera and microphone are blocked.

| Feature | Windows (Samsung Settings) | Linux sysfs used |
|---|---|---|
| Performance mode (Silent / Optimized / High performance) | Performance mode | `/sys/firmware/acpi/platform_profile` (through power-profiles-daemon when it is running) |
| Battery protection (stop charging at 80%) | Battery protection | `/sys/class/power_supply/BAT1/charge_control_end_threshold` |
| Keyboard backlight | Keyboard backlight | `/sys/class/leds/samsung-galaxybook::kbd_backlight` |
| Block camera and microphone | Block camera and microphone | firmware attribute `block_recording` |
| USB charging while off | USB charging | firmware attribute `usb_charging` |
| Power on when lid opens | Start up when lid opens | firmware attribute `power_on_lid_open` |

## Requirements

- Linux kernel 6.15 or later with `samsung-galaxybook` loaded (`lsmod | grep samsung_galaxybook`)
- GNOME 45–48, Python 3, PyGObject, GTK 4, libadwaita ≥ 1.4 (all installed by default on Zorin OS 18 and Ubuntu 24.04)

## Install / remove

```bash
./install.sh            # asks for sudo for the system service
./uninstall.sh          # add --purge to also delete /var/lib/galaxybook-settings
```

After installing, log out and back in to load the Quick Settings tile (Wayland cannot restart GNOME Shell in place).

## CLI usage

```bash
busctl --system introspect io.github.galaxybook.Settings1 /io/github/galaxybook/Settings1
gdbus call --system -d io.github.galaxybook.Settings1 -o /io/github/galaxybook/Settings1 \
    -m io.github.galaxybook.Settings1.SetChargeEndThreshold 80
```

## Development

Run the daemon against a fake sysfs tree on the session bus, then point the app at it:

```bash
python3 daemon/galaxybookd.py --session --sysfs-root /path/to/fake --state-file /tmp/state.json --no-polkit
GALAXYBOOK_BUS=session ./app/galaxybook-settings
```

## Permissions

Writes require the polkit action `io.github.galaxybook.settings.configure`. Active local users are
allowed without a password, the same policy power-profiles-daemon uses. Remote and inactive sessions need admin authentication.
