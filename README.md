# Galaxy Book Settings for Linux

A Linux replacement for the hardware parts of Samsung Settings on Windows, built for
Samsung Galaxy Book laptops on GNOME. It includes:

- **galaxybookd**, a small root D-Bus service (`io.github.jcarmog.GalaxyBook.Settings1`) that wraps the
  upstream `samsung-galaxybook` kernel driver. It checks polkit before each change and restores the battery limit at boot.
- **Galaxy Book Settings**, a GTK4/libadwaita app.
- **galaxybudsd**, a per-user D-Bus service (`io.github.jcarmog.GalaxyBook.Buds1`) that talks to Galaxy Buds3 Pro
  and Buds Pro over their Bluetooth control channel: battery, wear state, noise controls, equalizer and touch lock.
- **A GNOME Shell extension** that adds Quick Settings tiles for the laptop and the Buds, and shows a panel icon while the camera and microphone are blocked.

| Feature | Windows (Samsung Settings) | Linux sysfs used |
|---|---|---|
| Performance mode (Silent / Optimized / High performance) | Performance mode | `/sys/firmware/acpi/platform_profile` (through power-profiles-daemon when it is running) |
| Battery protection (stop charging at 80%) | Battery protection | `/sys/class/power_supply/BAT1/charge_control_end_threshold` |
| Keyboard backlight | Keyboard backlight | `/sys/class/leds/samsung-galaxybook::kbd_backlight` |
| Block camera and microphone | Block camera and microphone | firmware attribute `block_recording` |
| USB charging while off | USB charging | firmware attribute `usb_charging` |
| Power on when lid opens | Start up when lid opens | firmware attribute `power_on_lid_open` |

### Galaxy Buds3 Pro and Buds Pro

Pair and connect the earbuds in GNOME's Bluetooth settings as usual. When their audio connection comes up,
galaxybudsd opens the Samsung control channel (an RFCOMM serial channel, through BlueZ's profile API)
and exposes:

| Feature | D-Bus |
|---|---|
| Battery of each earbud and the case, in-ear / in-case state | `BatteryLeft`, `BatteryRight`, `BatteryCase`, `PlacementLeft`, `PlacementRight` |
| Model and the noise modes it supports | `Model`, `NoiseControlModes` |
| Noise controls: noise canceling, adaptive (Buds3 Pro), ambient sound, off | `NoiseControl`, `SetNoiseControl("anc" \| "adaptive" \| "ambient" \| "off")` |
| Equalizer presets | `Equalizer`, `SetEqualizer("off" \| "bass-boost" \| "soft" \| "dynamic" \| "clear" \| "treble-boost")` |
| Lock touch controls | `TouchpadLocked`, `SetTouchpadLocked(b)` |

Tested on a Galaxy Buds3 Pro (firmware R630XXU0AZG2). Buds Pro support follows the documented protocol
but hasn't been tried on hardware. The message format follows the protocol documented by the [GalaxyBudsClient](https://github.com/timschneeb/GalaxyBudsClient) project.
Run `galaxybudsd --debug` to log every message to and from the earbuds.

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
busctl --system introspect io.github.jcarmog.GalaxyBook.Settings1 /io/github/jcarmog/GalaxyBook/Settings1
gdbus call --system -d io.github.jcarmog.GalaxyBook.Settings1 -o /io/github/jcarmog/GalaxyBook/Settings1 \
    -m io.github.jcarmog.GalaxyBook.Settings1.SetChargeEndThreshold 80
```

## Development

Run the daemon against a fake sysfs tree on the session bus, then point the app at it:

```bash
python3 daemon/galaxybookd.py --session --sysfs-root /path/to/fake --state-file /tmp/state.json --no-polkit
GALAXYBOOK_BUS=session ./app/galaxybook-settings
```

Protocol tests for the Buds service:

```bash
python3 -m unittest discover tests
```

## Permissions

Writes require the polkit action `io.github.jcarmog.galaxybook.settings.configure`. Active local users are
allowed without a password, the same policy power-profiles-daemon uses. Remote and inactive sessions need admin authentication.
