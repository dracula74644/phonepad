#!/usr/bin/env bash
# Install (or remove) phonepad. Run with sudo from this directory.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBDIR=/usr/local/lib/phonepad
BTDROPIN=/etc/systemd/system/bluetooth.service.d/phonepad.conf
MAINCONF=/etc/bluetooth/main.conf
EXT_UUID="phonepad@local"

# Run a command as the invoking user, with their session bus attached, so
# gsettings talks to their GNOME session rather than root's.
run_as_user() {
    local uid
    uid=$(id -u "$SUDO_USER")
    sudo -u "$SUDO_USER" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        XDG_RUNTIME_DIR="/run/user/$uid" \
        "$@"
}

if [[ $EUID -ne 0 ]]; then
    echo "run this with sudo" >&2
    exit 1
fi

# ------------------------------------------------------------- uninstall
if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now phonepad.service 2>/dev/null || true
    rm -f /etc/systemd/system/phonepad.service
    rm -f /usr/local/bin/phonepad-toggle
    rm -f /usr/share/applications/phonepad.desktop
    rm -rf "$LIBDIR" /var/lib/phonepad
    rm -f "$BTDROPIN"
    if [[ -n "${SUDO_USER:-}" ]]; then
        USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
        rm -rf "$USER_HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
        run_as_user python3 - "$EXT_UUID" <<'PYEOF' 2>/dev/null || true
import ast, subprocess, sys
uuid = sys.argv[1]
cur = subprocess.run(['gsettings', 'get', 'org.gnome.shell', 'enabled-extensions'],
                     capture_output=True, text=True).stdout.strip()
try:
    items = ast.literal_eval(cur) if cur and cur != '@as []' else []
except (ValueError, SyntaxError):
    items = []
if uuid in items:
    items.remove(uuid)
    subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'enabled-extensions',
                    '[' + ', '.join("'%s'" % i for i in items) + ']'], check=True)
PYEOF
    fi
    rmdir /etc/systemd/system/bluetooth.service.d 2>/dev/null || true
    if [[ -f "$MAINCONF.phonepad-backup" ]]; then
        mv "$MAINCONF.phonepad-backup" "$MAINCONF"
        echo "restored $MAINCONF"
    fi
    systemctl daemon-reload
    systemctl restart bluetooth
    echo "phonepad removed; bluetooth restored to stock configuration"
    exit 0
fi

# --------------------------------------------------------------- install
echo "==> installing files"
install -d "$LIBDIR"
install -m 0644 "$SRC/phonepad.py" "$SRC/keymap.py" "$SRC/sdp_record.xml" "$LIBDIR/"
install -m 0755 "$SRC/phonepad-toggle" /usr/local/bin/phonepad-toggle
# Remembers which host to re-dial after a restart.
install -d -m 0755 /var/lib/phonepad

cat > /etc/systemd/system/phonepad.service <<'EOF'
[Unit]
Description=Phonepad - lend this laptop's keyboard over Bluetooth HID
After=bluetooth.service
Requires=bluetooth.service

[Service]
Type=simple
# BlueZ 5.87 ignores main.conf's Class and derives the class of device from
# its registered profiles, which leaves us advertising as a laptop. Force
# major 0x05 (peripheral) / minor 0x40 (keyboard) so the
# phone offers to pair with us as a keyboard.
ExecStartPre=-/usr/bin/btmgmt --index 0 class 5 64
ExecStart=/usr/bin/python3 /usr/local/lib/phonepad/phonepad.py
Restart=on-failure
RestartSec=3
# Runs as root: L2CAP PSMs 17/19 are privileged, and grabbing input
# requires write access to /dev/input/event*.

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/share/applications/phonepad.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Phonepad
Comment=Switch this laptop's keyboard between the laptop and the paired phone
Exec=/usr/local/bin/phonepad-toggle
Icon=input-tablet
Terminal=false
Categories=Utility;
Keywords=bluetooth;keyboard;phone;dex;
EOF

echo "==> configuring bluetoothd"
# The input plugin implements the HID *host* role and sits on L2CAP PSMs
# 17 and 19, which we need in order to be a HID *device*.
install -d /etc/systemd/system/bluetooth.service.d
cat > "$BTDROPIN" <<'EOF'
# Added by phonepad. The bundled "input" plugin occupies the L2CAP ports a
# HID device must listen on, so it is disabled here.
# Side effect: this machine can no longer use Bluetooth mice or keyboards
# of its own. Remove this file and restart bluetooth to undo.
[Service]
ExecStart=
ExecStart=/usr/lib/bluetooth/bluetoothd -P input
EOF

# Advertise as a peripheral keyboard so the phone offers to pair with us as
# a keyboard rather than as a laptop: 0x002540 = peripheral / keyboard.
if [[ ! -f "$MAINCONF.phonepad-backup" ]]; then
    cp "$MAINCONF" "$MAINCONF.phonepad-backup"
fi
if grep -qE '^\s*Class\s*=' "$MAINCONF"; then
    sed -i -E 's|^\s*Class\s*=.*|Class = 0x002540|' "$MAINCONF"
else
    sed -i -E 's|^#\s*Class\s*=.*|Class = 0x002540|' "$MAINCONF"
fi
grep -qE '^\s*Class\s*=\s*0x002540' "$MAINCONF" \
    || echo "warning: could not set Class in $MAINCONF; set it by hand" >&2

echo "==> installing the top bar indicator"
# The shell extension is per-user, and gsettings needs that user's session bus.
if [[ -n "${SUDO_USER:-}" ]] && [[ -d "$SRC/gnome-extension" ]]; then
    USER_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    EXTDIR="$USER_HOME/.local/share/gnome-shell/extensions/$EXT_UUID"
    install -d -o "$SUDO_USER" -g "$SUDO_USER" "$EXTDIR"
    install -m 0644 -o "$SUDO_USER" -g "$SUDO_USER" \
        "$SRC/gnome-extension/metadata.json" "$SRC/gnome-extension/extension.js" \
        "$SRC/gnome-extension/stylesheet.css" "$EXTDIR/"
    run_as_user gsettings get org.gnome.shell enabled-extensions >/dev/null 2>&1 && \
    run_as_user python3 - "$EXT_UUID" <<'PYEOF' || echo "  (could not update gsettings; enable it by hand)"
import ast, subprocess, sys
uuid = sys.argv[1]
cur = subprocess.run(['gsettings', 'get', 'org.gnome.shell', 'enabled-extensions'],
                     capture_output=True, text=True).stdout.strip()
try:
    items = ast.literal_eval(cur) if cur and cur != '@as []' else []
except (ValueError, SyntaxError):
    items = []
if uuid not in items:
    items.append(uuid)
    subprocess.run(['gsettings', 'set', 'org.gnome.shell', 'enabled-extensions',
                    '[' + ', '.join("'%s'" % i for i in items) + ']'], check=True)
    print('  enabled %s (takes effect at next login)' % uuid)
else:
    print('  %s already enabled' % uuid)
PYEOF
fi

echo "==> starting services"
systemctl daemon-reload
systemctl restart bluetooth
sleep 2
systemctl enable --now phonepad.service

sleep 2
systemctl --no-pager --lines=15 status phonepad.service || true

cat <<'EOF'

----------------------------------------------------------------------
Installed. To pair:

  1. On the phone: Settings -> Connections -> Bluetooth, scan.
  2. Pick "Phonepad". Accept the pairing prompt on the phone; this side
     accepts automatically.
  3. The phone should connect to it as a keyboard. Check with:
       journalctl -u phonepad -f
     You want to see "interrupt channel up ... - ready".

To use:

  Ctrl x2            toggle the keyboard between laptop and phone (either Ctrl)
                     (two solo taps within 0.4s)
  Phonepad app icon  same toggle, pin it to the dash for a taskbar button
  Hold Escape 2s     panic release, if the toggle ever fails

  phonepad-toggle test        prove the keyboard path without grabbing

While capture is on, this laptop's keyboard is grabbed and the desktop sees
nothing from it. Pointing is out of scope: use a mouse on the phone.
Capture also releases itself if the Bluetooth link drops.

To undo everything:  sudo ./install.sh --uninstall
----------------------------------------------------------------------
EOF
