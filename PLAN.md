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

## Phase 2: Galaxy Buds (working on real hardware, not yet installed)

Decisions (2026-10-24): target the **Buds Pro** (the pair at hand); the Bluetooth link lives in a
**per-user D-Bus service** (`galaxybudsd`, `io.github.galaxybook.Buds1`) shared by the app and the extension.

Update (2026-10-24): the pair actually connected is a **Galaxy Buds3 Pro** ("José do Carmo's Buds3 Pro",
A0:56:2C:B7:C2:E9, firmware R630XXU0AZG2). galaxybudsd now has a model table (Buds3 Pro + Buds Pro).
Buds3 Pro differences: Samsung UUID `2e73a4ad-…`, Adaptive noise mode, bitmask touch lock,
case battery only in STATUS_UPDATED (0x60). The earbuds ACK every command with 0x42 echoing the payload.

### Tested with the real Buds3 Pro

- [x] Control channel opens on the first attempt; full status arrives without a handshake (no MANAGER_INFO needed)
- [x] Battery, placement, case battery, noise mode, EQ and touch lock decode correctly
- [x] Noise canceling / ambient / off switching confirmed by ear; adaptive acknowledged
- [x] Equalizer and touch lock/unlock acknowledged, then restored
- [x] Touch-and-hold on the earbuds updates the service (NOISE_CONTROLS_UPDATE 0x77)
- [x] App Buds page rendered with the real earbuds

Windows reference: Samsung's Bluetooth sync app (Windows user `joaquim`) lists *Galaxy Buds Pro (AAAD)*
and *Galaxy Buds+ (44FA)*; their addresses are DPAPI-encrypted, so the pairing can't be reused.
Neither pair is paired on Linux yet.

### Done

- [x] `daemon/galaxybudsd.py`: BlueZ SPP client profile, frame encoder/decoder, status parsing, set noise control / EQ / touch lock
- [x] Protocol checked against a real Buds Pro capture from GalaxyBudsClient's test data (kept out of the repo, GPL)
- [x] `tests/test_galaxybudsd.py`: 10 protocol unit tests (`python3 -m unittest discover tests`)
- [x] End-to-end test with simulated earbuds over a socketpair: properties, all three setters, invalid input, disconnect reset
- [x] Profile registration with the real BlueZ works as a normal user
- [x] App: "Laptop" / "Galaxy Buds" tabs; Buds page rendered and checked
- [x] Extension: "Galaxy Buds" tile (click = noise canceling on/off, battery in subtitle, mode menu, touch lock); syntax only
- [x] install.sh / uninstall.sh: user unit `galaxybudsd.service` + D-Bus activation file

### To do

- [x] Pair the earbuds (done: Buds3 Pro)
- [ ] **Reinstall** to deploy galaxybudsd and the new app/extension: `./install.sh`, then close and reopen the app
- [ ] Check the installed service picks up the Buds: `systemctl --user status galaxybudsd` and the app's Galaxy Buds tab
- [ ] Disconnect/reconnect the Buds and check the app clears and refills the status
- [ ] **After logout/login**: check the Galaxy Buds tile together with the Galaxy Book tile
- [ ] Commit phase 2

Debugging: `systemctl --user stop galaxybudsd; /usr/libexec/galaxybook-settings/galaxybudsd --debug`

### Possible later additions

- Buds+ / original Buds Pro on hardware (the Buds Pro is in the model table but untested)
- Ambient sound volume, ANC level, find my earbuds, custom 9-band EQ (Buds3 Pro)

## Phase 3 option (not started)

- **Galaxy phone (S26 Ultra):** battery and notifications, sending files, screen mirroring via adb/scrcpy and Bluetooth. Quick Share and Multi Control use proprietary protocols, so those would be best-effort.

## Useful references

- Service log: `journalctl -u galaxybookd`
- D-Bus API: `busctl --system introspect io.github.galaxybook.Settings1 /io/github/galaxybook/Settings1`
- Remove everything: `./uninstall.sh` (add `--purge` to delete `/var/lib/galaxybook-settings`)
- Development setup: see README.md
