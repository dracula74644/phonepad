# phonepad

**Lend your Linux laptop's keyboard to your phone, over Bluetooth, with nothing installed on the phone.**

Your phone is on an external monitor — Samsung DeX, a desktop-mode Android, a tablet, a games console — and you want to type on it with the good keyboard that is already in front of you. phonepad makes the laptop *become* a Bluetooth keyboard. The phone pairs with it exactly as it would with any wireless keyboard, because from the phone's point of view that is all it is.

Double-tap either Ctrl key and your keystrokes go to the phone. Double-tap again and they come back to the laptop.

## Why this instead of the alternatives

| Approach | What it needs |
|---|---|
| Deskflow / Barrier / Synergy | Both machines on one network, plus a client app on the phone |
| `adb shell input` | USB cable or TCP debugging, and the port tied up |
| Scrcpy | USB or network, and it mirrors the phone rather than feeding it |
| **phonepad** | **A Bluetooth pairing. Nothing else.** |

Because it emulates HID at the Bluetooth level, there is no app to install on the phone, no network in the path, no latency from a relay server, and nothing to break when you change Wi-Fi. It works when the two devices are on different networks or no network at all.

The trade-off is that it is a *keyboard* and only a keyboard. See [Limitations](#limitations).

## How it works

1. Registers a HID service record (Bluetooth profile UUID `0x1124`) with BlueZ, advertising itself as a peripheral keyboard.
2. Owns the two privileged L2CAP ports a HID device must listen on, PSM 17 (control) and PSM 19 (interrupt). BlueZ's bundled `input` plugin normally occupies these, so the installer disables it — see the warning below.
3. Reads your keyboard straight off `evdev`, which is why this works on Wayland as well as X11.
4. While capture is on, takes the keyboard exclusively with `EVIOCGRAB` so the laptop's own desktop sees nothing from it, and translates each event into a standard 8-byte HID keyboard report.

## Requirements

- Linux with BlueZ (`bluetoothd`) and a Bluetooth **Classic** adapter. BLE-only adapters will not work.
- Python 3 with `python-evdev`, `python-dbus`, `python-gobject`.
- `btmgmt`, from `bluez-utils`.
- Root. The L2CAP ports are privileged and grabbing input needs write access to `/dev/input/event*`.
- Optional: `wl-clipboard` for the clipboard feature, and GNOME Shell 45–51 for the top-bar switch.

Developed and used on Arch Linux with GNOME 51 on Wayland, against a Galaxy S25 in DeX. Nothing in it is Arch-specific or GNOME-specific except the optional shell extension, but other combinations are untested.

## Install

```sh
git clone https://github.com/dracula74644/phonepad
cd phonepad
sudo ./install.sh
```

Then pair, from the phone:

1. Settings → Connections → Bluetooth, and scan.
2. Pick **Phonepad**. Accept the prompt on the phone; the laptop accepts automatically.
3. Confirm the link came up with `journalctl -u phonepad -f`. You want `interrupt channel up ... - ready`.

The daemon remembers the host and re-dials it after a reboot, so you pair once.

## Use

| | |
|---|---|
| **Ctrl, Ctrl** | Move the keyboard between laptop and phone. Either Ctrl key, two solo taps within 0.4s. |
| **Phonepad switch** | Same toggle, in the GNOME top bar. |
| **Hold Escape 2s** | Panic release, if the toggle ever fails. |
| `phonepad-toggle` | The same toggle from a shell; also `on`, `off`, `status`. |
| `phonepad-toggle test` | Prove the Bluetooth path works without grabbing your only keyboard. |
| `phonepad-toggle paste` | Type the laptop's clipboard into the phone, character by character. |

Two taps only count when the Ctrl key goes down and up on its own, within 0.5s, with nothing pressed in between — so `Ctrl+C` twice in a row is two ordinary copy commands, not a toggle. This is what keeps Ctrl usable as a real modifier.

Capture releases itself if the Bluetooth link drops, so a phone that wanders out of range cannot leave you without a keyboard.

For a clipboard shortcut, bind `phonepad-toggle paste` to a key yourself; the installer does not touch your keybindings. On GNOME:

```sh
SCHEMA=org.gnome.settings-daemon.plugins.media-keys.custom-keybinding
KB=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/phonepad-paste/

# Note: this replaces the list of custom keybindings. If you already have
# some, add "$KB" to the existing list instead of overwriting it.
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$KB']"

gsettings set "$SCHEMA:$KB" name 'Phonepad: type clipboard into phone'
gsettings set "$SCHEMA:$KB" command '/usr/local/bin/phonepad-toggle paste'
gsettings set "$SCHEMA:$KB" binding '<Super><Shift>v'
```

## Limitations

**It disables BlueZ's `input` plugin.** This is the significant one. That plugin implements the HID *host* role and sits on the exact L2CAP ports a HID *device* must listen on, so the two cannot coexist. While phonepad is installed, **this machine can no longer use Bluetooth keyboards or mice of its own.** The installer does this through a systemd drop-in at `/etc/systemd/system/bluetooth.service.d/phonepad.conf`; delete that file and restart `bluetooth` to undo it, or run the uninstaller. Bluetooth audio and everything else are unaffected.

**It runs as root**, for the reasons above. The control socket is group-`wheel` and mode 0660, so unprivileged members of that group can toggle capture but not much else.

**Keyboard only — no mouse.** A keyboard+mouse composite HID descriptor was built and reverted: the pointer did not behave acceptably on the phone, and the laptop's own touchpad is an absolute multitouch device that would need libinput's finger tracking reimplemented before it could be shared at all. Use a mouse on the phone, or its touchscreen.

**Clipboard typing assumes a US layout** on the host. A different layout on the phone lands different symbols, exactly as it would with any real USB keyboard.

**It edits `/etc/bluetooth/main.conf`** to set `Class = 0x002540`, so the phone offers to pair with it as a keyboard rather than as a laptop. The original is kept at `main.conf.phonepad-backup` and restored on uninstall.

**Changing the HID report descriptor means re-pairing.** A host caches the descriptor when it bonds, so if you modify `sdp_record.xml` you must forget the device on the phone and pair again.

## Uninstall

```sh
sudo ./install.sh --uninstall
```

Removes the service, the binaries, the remembered host, the shell extension and the Bluetooth drop-in, and restores `main.conf`.

## Tests

```sh
python3 test_phonepad.py
```

No Bluetooth, no root and no real devices needed — it drives the report building, device selection, toggle detection and outgoing queue with synthetic data.

## Layout

| | |
|---|---|
| `phonepad.py` | The daemon: BlueZ/D-Bus plumbing, HID reports, evdev capture |
| `keymap.py` | evdev key name → HID usage code |
| `gen_sdp.py` | Generates the HID report descriptor and `sdp_record.xml` |
| `sdp_record.xml` | The service record BlueZ registers |
| `phonepad-toggle` | Control client over the unix socket |
| `gnome-extension/` | Optional GNOME Shell top-bar switch |
| `install.sh` | Installer and uninstaller |
