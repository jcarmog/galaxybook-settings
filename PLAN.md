# Samsung integration for Linux: plan and next steps

Last updated: 2026-10-24

## Goal

Integrate Samsung devices with Linux (Zorin OS, GNOME 46, Wayland) on a Galaxy Book3 Ultra (960XFH),
using the Windows disk (`/media/jose/680044AC004482D4`) as reference for what worked there.

Devices found on the Windows install:

- The laptop itself (Samsung Settings: performance mode, battery protection, keyboard backlight)
- Galaxy S26 Ultra (paired phone)
- Galaxy Buds Pro and Buds+ (configured in Samsung Settings)
- Multi Control

## Decisions

- **Scope of the first round:** Galaxy Book Settings only. Phone and Buds come later.
- **Form:** a GTK4/libadwaita app plus a GNOME Shell extension, both talking to one root D-Bus service (`galaxybookd`).
- Built on the upstream `samsung-galaxybook` kernel driver rather than porting Windows code.

## Phase 1: Galaxy Book Settings

### Done

- [x] `galaxybookd` D-Bus service (`io.github.galaxybook.Settings1`) with polkit checks and battery-limit restore at boot
- [x] GTK4 app `galaxybook-settings`
- [x] GNOME Shell extension (Quick Settings tile + camera/mic blocked indicator)
- [x] `install.sh` / `uninstall.sh`
- [x] Tested against a fake sysfs tree on the session bus
- [x] Installed on the real system; service enabled and running; installed files match the source
- [x] Write through polkit with no password prompt (no-op `SetKeyboardBacklight 1`)

### To do

- [ ] **After logout/login:** enable and test the extension
  ```bash
  gnome-extensions enable galaxybook-settings@galaxybook.github.io
  gnome-extensions info galaxybook-settings@galaxybook.github.io   # expect Enabled: Yes, State: ACTIVE
  ```
  Then check that:
  - the "Galaxy Book" tile shows in Quick Settings and toggles battery protection (80% / off)
  - the tile menu switches (camera/mic block, USB charging, lid power-on) work
  - the camera-off icon shows in the top bar while camera/mic are blocked
  - errors, if any: `journalctl --user -b -o cat /usr/bin/gnome-shell | grep -i galaxybook`
- [x] **Performance mode through power-profiles-daemon** (tested 2026-10-24): daemon → sysfs + PPD stay in sync for all 3 modes, and `powerprofilesctl set` changes show up in the daemon. Commands kept for re-testing:
  ```bash
  gdbus call --system -d io.github.galaxybook.Settings1 -o /io/github/galaxybook/Settings1 \
      -m io.github.galaxybook.Settings1.SetPlatformProfile quiet
  powerprofilesctl get        # expect power-saver
  gdbus call --system -d io.github.galaxybook.Settings1 -o /io/github/galaxybook/Settings1 \
      -m io.github.galaxybook.Settings1.SetPlatformProfile balanced
  ```
- [x] **Turn on battery protection** (set to 80% on 2026-10-24; saved in `/var/lib/galaxybook-settings/state.json`). To change it:
  ```bash
  gdbus call --system -d io.github.galaxybook.Settings1 -o /io/github/galaxybook/Settings1 \
      -m io.github.galaxybook.Settings1.SetChargeEndThreshold 80
  ```
- [ ] **After a reboot:** confirm the limit is restored
  ```bash
  cat /sys/class/power_supply/BAT1/charge_control_end_threshold   # expect 80
  journalctl -u galaxybookd -b --no-pager
  ```
- [x] Put the project under git (branch `main`, first commit `3500c89`; `core.fileMode=false` because the drive is NTFS)

## Phase 2 options (not started)

- **Galaxy Buds** (suggested next): battery levels, noise cancelling / ambient mode, and EQ over the Buds Bluetooth RFCOMM protocol, added as a page in the app.
- **Galaxy phone (S26 Ultra):** battery and notifications, sending files, screen mirroring via adb/scrcpy and Bluetooth. Quick Share and Multi Control use proprietary protocols, so those would be best-effort.

## Useful references

- Service log: `journalctl -u galaxybookd`
- D-Bus API: `busctl --system introspect io.github.galaxybook.Settings1 /io/github/galaxybook/Settings1`
- Remove everything: `./uninstall.sh` (add `--purge` to delete `/var/lib/galaxybook-settings`)
- Development setup: see README.md
