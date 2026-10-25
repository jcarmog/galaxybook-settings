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

- [x] `galaxybookd` D-Bus service (`io.github.jcarmog.GalaxyBook.Settings1`) with polkit checks and battery-limit restore at boot
- [x] GTK4 app `galaxybook-settings`
- [x] GNOME Shell extension (Quick Settings tile + camera/mic blocked indicator)
- [x] `install.sh` / `uninstall.sh`
- [x] Tested against a fake sysfs tree on the session bus
- [x] Installed on the real system; service enabled and running; installed files match the source
- [x] Write through polkit with no password prompt (no-op `SetKeyboardBacklight 1`)

### To do

- [ ] **After logout/login:** enable and test the extension
  ```bash
  gnome-extensions enable galaxybook-settings@jcarmog.github.io
  gnome-extensions info galaxybook-settings@jcarmog.github.io   # expect Enabled: Yes, State: ACTIVE
  ```
  Then check that:
  - the "Galaxy Book" tile shows in Quick Settings and toggles battery protection (80% / off)
  - the tile menu switches (camera/mic block, USB charging, lid power-on) work
  - the camera-off icon shows in the top bar while camera/mic are blocked
  - errors, if any: `journalctl --user -b -o cat /usr/bin/gnome-shell | grep -i galaxybook`
- [x] **Performance mode through power-profiles-daemon** (tested 2026-10-24): daemon → sysfs + PPD stay in sync for all 3 modes, and `powerprofilesctl set` changes show up in the daemon. Commands kept for re-testing:
  ```bash
  gdbus call --system -d io.github.jcarmog.GalaxyBook.Settings1 -o /io/github/jcarmog/GalaxyBook/Settings1 \
      -m io.github.jcarmog.GalaxyBook.Settings1.SetPlatformProfile quiet
  powerprofilesctl get        # expect power-saver
  gdbus call --system -d io.github.jcarmog.GalaxyBook.Settings1 -o /io/github/jcarmog/GalaxyBook/Settings1 \
      -m io.github.jcarmog.GalaxyBook.Settings1.SetPlatformProfile balanced
  ```
- [x] **Turn on battery protection** (set to 80% on 2026-10-24; saved in `/var/lib/galaxybook-settings/state.json`). To change it:
  ```bash
  gdbus call --system -d io.github.jcarmog.GalaxyBook.Settings1 -o /io/github/jcarmog/GalaxyBook/Settings1 \
      -m io.github.jcarmog.GalaxyBook.Settings1.SetChargeEndThreshold 80
  ```
- [x] **After a reboot:** confirm the limit is restored (2026-10-24: 80 after the 20:46 boot, galaxybookd started clean)
  ```bash
  cat /sys/class/power_supply/BAT1/charge_control_end_threshold   # expect 80
  journalctl -u galaxybookd -b --no-pager
  ```
- [x] Put the project under git (branch `main`, first commit `3500c89`; `core.fileMode=false` because the drive is NTFS)

## Phase 2: Galaxy Buds (installed 2026-10-24)

Decisions (2026-10-24): target the **Buds Pro** (the pair at hand); the Bluetooth link lives in a
**per-user D-Bus service** (`galaxybudsd`, `io.github.jcarmog.GalaxyBook.Buds1`) shared by the app and the extension.

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
- [x] **Reinstall** to deploy galaxybudsd and the new app/extension: `./install.sh`, then close and reopen the app
- [x] Check the installed service picks up the Buds: `systemctl --user status galaxybudsd` and the app's Galaxy Buds tab
- [x] Disconnect/reconnect the Buds: service clears status (battery -1, mode "") and refills it on reconnect (tested 2026-10-24 via bluetoothctl)
- [ ] **After logout/login**: check the Galaxy Buds tile together with the Galaxy Book tile
- [x] Commit phase 2 (`5995765`)

Debugging: `systemctl --user stop galaxybudsd; /usr/libexec/galaxybook-settings/galaxybudsd --debug`

### Possible later additions

- Buds+ / original Buds Pro on hardware (the Buds Pro is in the model table but untested)
- Ambient sound volume, ANC level, find my earbuds, custom 9-band EQ (Buds3 Pro)

## Phase 3: Galaxy phone (S26 Ultra), installed 2026-10-24

Decisions (2026-10-24):
- Build our own **per-user service `galaxyphoned`** (`io.github.jcarmog.GalaxyBook.Phone1`) rather than use the
  preinstalled Zorin Connect, so the phone sits in the same app and Quick Settings as the laptop and Buds.
- **Transport: Bluetooth first, adb optional.** Bluetooth needs no phone setup: OBEX push (files),
  MAP (SMS), HFP battery indicator (0-5). When adb is reachable (USB or wireless debugging) it adds
  exact battery, all-app notifications and scrcpy mirroring.
- First round: battery + connection, send files, SMS / notifications, screen mirroring.

Phone facts: "S26 Ultra de José do Carmo", EC:B5:50:3D:4A:BC, already paired/bonded/trusted in BlueZ;
offers OPP, MAP server, PBAP server, HFP/HSP AG, PANU/NAP. adb is at
`/media/jose/arquivos/Projetos/ambiente/Android/Sdk/platform-tools/adb` (no device attached yet);
scrcpy is not installed; obexd is at `/usr/libexec/bluetooth/obexd` (user `obex.service`, inactive).

Probed on the real phone (2026-10-24):
- **Battery over Bluetooth comes for free:** once the phone connects, PipeWire's HFP-HF backend reports the
  AG battery indicator to BlueZ, so `org.bluez.Battery1.Percentage` appears on the phone (0-100 in 20% steps).
  galaxyphoned reads that instead of opening HFP itself (PipeWire owns it).
- **MAP works** through obexd (`obex.service`, user): after allowing message access on the phone, a
  `map` session lists `/telecom/msg/{inbox,outbox,sent,deleted,draft}` with sender, timestamp, read flag and text.
  The first attempt timed out while the phone's permission prompt was pending.
- adb 37 (platform-tools) has built-in mDNS, so a phone paired once with `adb pair` reconnects by itself while
  wireless debugging is on.

Design:
- `io.github.jcarmog.GalaxyBook.Phone1` on the session bus, object `/io/github/jcarmog/GalaxyBook/Phone1`.
  Properties: `Connected`, `Name`, `Address`, `Battery` (-1 unknown), `BatteryCharging`, `BatterySource`
  (`bluetooth`/`adb`), `AdbConnected`, `AdbSerial`, `CanMirror`.
  Methods: `SendFiles(as)` (adb push to Download when adb is up, else OBEX push), `StartMirroring()` (scrcpy),
  `AdbPair(s address, s code)` (wireless debugging pairing).
- SMS: keep a MAP session open while the phone is connected; new inbox messages (obexd's MNS events,
  surfaced as new `Message1` objects) become desktop notifications.
- adb: `adb track-devices` for presence; `dumpsys battery` for exact battery; poll
  `dumpsys notification --noredact` and post new app notifications on the desktop.
- Nautilus script "Send to Galaxy phone" calls `SendFiles`.

### To do

- [x] Design the D-Bus API and pick the battery source over Bluetooth
- [x] `daemon/galaxyphoned.py` + `tests/test_galaxyphoned.py` (12 parser tests; 26 total pass)
- [x] Ran from source against the real phone: Bluetooth connection, name, battery 40% (bluetooth source),
      MAP session opened automatically; method argument checks return clear errors
- [x] App "Phone" tab (status, Send files, Mirror screen, wireless-debugging pairing); rendered and checked.
      App takes `--page laptop|buds|phone` and `--send-files`; desktop action "Send files to phone"
- [x] Extension "Galaxy phone" tile (battery subtitle, Mirror screen / Send files… / Phone settings); syntax only
- [x] install.sh / uninstall.sh: user unit `galaxyphoned.service`, D-Bus activation file, drop-in
      `~/.config/systemd/user/galaxyphoned.service.d/tools.conf` with `ADB=`/`SCRCPY=` when they live outside
      /usr/bin, Nautilus script "Send to Galaxy phone"
- [x] **Install** (2026-10-24): all installed files match the source; galaxyphoned active and enabled, sees the phone
      (battery 40%, bluetooth source), MAP session open; adb drop-in written; extension ACTIVE after re-login, no shell errors
- [x] Send a file over Bluetooth (accept on the phone) from the app and from Nautilus (right-click › Scripts)
      (2026-10-24: received on the phone via D-Bus `SendFiles`, the app's Send files button and the Nautilus script)
- [ ] Receive an SMS and check the desktop notification (MAP event or the 120 s poll)
- [ ] Pair adb over wireless debugging from the app; check exact battery, app notifications, adb push
- [ ] Install scrcpy (`sudo apt install scrcpy`, 1.25 in the Zorin repo) and test Mirror screen
      (2026-10-24: apt `scrcpy` 1.25 and `adb` 34 now installed in /usr/bin, so install.sh writes no drop-in
      and the service uses those; still to test Mirror screen, which needs adb connected)
- [ ] After logout/login: Galaxy phone tile
- [x] Commit phase 3 (2026-10-24)

Debugging: `systemctl --user stop galaxyphoned; ADB=$(command -v adb) /usr/libexec/galaxybook-settings/galaxyphoned --debug`

## Useful references

- Service log: `journalctl -u galaxybookd`
- D-Bus API: `busctl --system introspect io.github.jcarmog.GalaxyBook.Settings1 /io/github/jcarmog/GalaxyBook/Settings1`
- Remove everything: `./uninstall.sh` (add `--purge` to delete `/var/lib/galaxybook-settings`)
- Development setup: see README.md
