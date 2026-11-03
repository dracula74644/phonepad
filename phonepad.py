#!/usr/bin/env python3
"""
phonepad - lend this laptop's keyboard to a Bluetooth host.

Emulates a Bluetooth HID keyboard (Classic BT, HID profile UUID 0x1124) so
that a phone -- e.g. a Samsung running DeX on an external monitor -- sees an
ordinary wireless keyboard. Pointing is out of scope: use a mouse on the
phone. (A mouse collection was added on 2026-10-29 and removed again the
same day; the cursor did not behave properly on the phone.)

Input is captured straight off evdev, so this works on Wayland.

Toggle capture by double-tapping either Ctrl (configurable), or by poking
the control socket (see phonepad-toggle).
"""

import base64
import collections
import errno
import os
import queue
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

from evdev import InputDevice, ecodes, list_devices

import keymap

# ---------------------------------------------------------------- config

PROFILE_PATH = "/org/bluez/phonepad_profile"
AGENT_PATH = "/org/bluez/phonepad_agent"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
P_CTRL = 0x0011
P_INTR = 0x0013

CONTROL_SOCKET = "/run/phonepad.sock"
SOCKET_GROUP = "wheel"

STATE_DIR = "/var/lib/phonepad"
HOST_FILE = os.path.join(STATE_DIR, "host")

# How often to re-dial a remembered host, and how long to wait on each try.
DIAL_INTERVAL = 8.0
DIAL_TIMEOUT = 6.0

# How long to wait at startup for the bluetooth controller to register.
ADAPTER_WAIT = 90.0

# Outgoing HID reports held while the link applies back-pressure.
OUTQ_LIMIT = 256

ADAPTER_ALIAS = "Phonepad"

# Toggle: tap this key twice, on its own, inside TOGGLE_WINDOW seconds.
#
# It used to be a Ctrl+Alt+Shift+B chord, which had an unfixable flaw: while
# capture is off the keyboard is not grabbed, so the chord reached whichever
# laptop app had focus on its way to us -- and nothing can retract an event
# the compositor has already been handed. A bare modifier tap leaks just as
# much, but no application acts on one, so it stops mattering.
# Either Ctrl counts. Binding only the right one was a mistake: it is the
# less commonly used key, and on a laptop it may not physically exist even
# though the AT keyboard driver advertises it.
TOGGLE_KEYS = frozenset((ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL))
TOGGLE_WINDOW = 0.4

# A tap only counts if the key went down and up on its own. That is what
# keeps Right Ctrl usable as a real modifier: Ctrl+C twice in a row is two
# combinations, not two taps.
TOGGLE_MAX_HOLD = 0.5

# Panic release: hold Escape this long while captured to force an ungrab.
PANIC_KEY = ecodes.KEY_ESC
PANIC_HOLD = 2.0

# Longest clipboard we will type out, and how many report steps per loop
# iteration while doing it.
PASTE_LIMIT = 4096

# ASCII -> (evdev key name, needs shift), for typing the clipboard. This
# assumes the host is set to a US layout; a different layout on the phone
# will land different symbols, the same as any real USB keyboard.
_UNSHIFTED = {
    " ": "KEY_SPACE", "\n": "KEY_ENTER", "\t": "KEY_TAB",
    "-": "KEY_MINUS", "=": "KEY_EQUAL", "[": "KEY_LEFTBRACE",
    "]": "KEY_RIGHTBRACE", "\\": "KEY_BACKSLASH", ";": "KEY_SEMICOLON",
    "'": "KEY_APOSTROPHE", "`": "KEY_GRAVE", ",": "KEY_COMMA",
    ".": "KEY_DOT", "/": "KEY_SLASH",
}
_SHIFTED = {
    "!": "KEY_1", "@": "KEY_2", "#": "KEY_3", "$": "KEY_4", "%": "KEY_5",
    "^": "KEY_6", "&": "KEY_7", "*": "KEY_8", "(": "KEY_9", ")": "KEY_0",
    "_": "KEY_MINUS", "+": "KEY_EQUAL", "{": "KEY_LEFTBRACE",
    "}": "KEY_RIGHTBRACE", "|": "KEY_BACKSLASH", ":": "KEY_SEMICOLON",
    '"': "KEY_APOSTROPHE", "~": "KEY_GRAVE", "<": "KEY_COMMA",
    ">": "KEY_DOT", "?": "KEY_SLASH",
}


def _build_charmap():
    table = {}
    for ch, name in _UNSHIFTED.items():
        table[ch] = (name, False)
    for ch, name in _SHIFTED.items():
        table[ch] = (name, True)
    for c in "abcdefghijklmnopqrstuvwxyz":
        table[c] = ("KEY_" + c.upper(), False)
        table[c.upper()] = ("KEY_" + c.upper(), True)
    for d in "1234567890":
        table[d] = ("KEY_" + d, False)
    return table


CHARMAP = _build_charmap()

HID_MODMAP = {
    ecodes.KEY_LEFTCTRL: 0x01,
    ecodes.KEY_LEFTSHIFT: 0x02,
    ecodes.KEY_LEFTALT: 0x04,
    ecodes.KEY_LEFTMETA: 0x08,
    ecodes.KEY_RIGHTCTRL: 0x10,
    ecodes.KEY_RIGHTSHIFT: 0x20,
    ecodes.KEY_RIGHTALT: 0x40,
    ecodes.KEY_RIGHTMETA: 0x80,
}

def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ------------------------------------------------------------ bluetooth

class Profile(dbus.service.Object):
    """Stub org.bluez.Profile1. We own the L2CAP sockets ourselves; this
    object exists so BlueZ will publish our SDP record."""

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        log("profile released by bluez")

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Cancel(self):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, path, fd, properties):
        # Not expected: we listen on the PSMs directly.
        os.close(fd.take())

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, path):
        pass


class Agent(dbus.service.Object):
    """Auto-accepting pairing agent, so pairing needs no bluetoothctl prompt."""

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log("authorized service", uuid, "for", device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log("passkey for %s: %06u" % (device, passkey))

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log("pin for %s: %s" % (device, pincode))

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log("auto-confirming pairing with", device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log("auto-authorizing", device)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        pass


class BluetoothHID:
    def __init__(self, service_record):
        self.bus = dbus.SystemBus()
        self.adapter_path = "/org/bluez/hci0"
        self.ctrl = None
        self.intr = None
        self.selector = None
        self.outq = collections.deque()
        self._writing = False
        self.host = self._load_host()
        self.dialled = queue.Queue()
        self._dialing = False
        self._last_dial = 0.0
        self._dial_complained = False

        adapter = self._wait_for_adapter()
        self.mac = str(adapter.Get("org.bluez.Adapter1", "Address"))
        for prop, val in (("Powered", True), ("Alias", ADAPTER_ALIAS),
                          ("Pairable", True), ("PairableTimeout", dbus.UInt32(0)),
                          ("DiscoverableTimeout", dbus.UInt32(0)),
                          ("Discoverable", True)):
            try:
                adapter.Set("org.bluez.Adapter1", prop, val)
            except dbus.DBusException as e:
                log("warn: could not set %s: %s" % (prop, e.get_dbus_name()))
        log("adapter", self.mac, "powered, discoverable, pairable")
        self._set_device_class()

        # Sockets must be bound before BlueZ publishes the record, otherwise a
        # host can race in and find nothing listening on the PSMs.
        self.s_ctrl = self._listener(P_CTRL)
        self.s_intr = self._listener(P_INTR)

        Profile(self.bus, PROFILE_PATH)
        manager = dbus.Interface(
            self.bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
        manager.RegisterProfile(PROFILE_PATH, HID_UUID, {
            "ServiceRecord": service_record,
            "Name": "Phonepad HID",
            "Role": "server",
            "Service": HID_UUID,
            "RequireAuthentication": dbus.Boolean(False),
            "RequireAuthorization": dbus.Boolean(False),
            "AutoConnect": dbus.Boolean(True),
        })
        log("SDP record registered")

        Agent(self.bus, AGENT_PATH)
        am = dbus.Interface(self.bus.get_object("org.bluez", "/org/bluez"),
                            "org.bluez.AgentManager1")
        am.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
        am.RequestDefaultAgent(AGENT_PATH)
        log("pairing agent registered")

    def _wait_for_adapter(self):
        """At boot, bluetoothd is up well before the controller is registered.
        systemd ordering cannot express that, so wait for the object here
        instead of dying and leaving systemd to restart us in a loop."""
        deadline = time.monotonic() + ADAPTER_WAIT
        complained = False
        while True:
            try:
                iface = dbus.Interface(
                    self.bus.get_object("org.bluez", self.adapter_path),
                    "org.freedesktop.DBus.Properties")
                iface.Get("org.bluez.Adapter1", "Address")
                return iface
            except dbus.DBusException:
                if time.monotonic() >= deadline:
                    raise
                if not complained:
                    log("waiting for the bluetooth controller to appear...")
                    complained = True
                time.sleep(1.0)

    def _set_device_class(self):
        """BlueZ 5.87 ignores main.conf's Class and computes the class of
        device from its registered profiles, which leaves us looking like a
        laptop. Force peripheral/keyboard. Done here rather than in an
        ExecStartPre so it cannot run before the controller exists."""
        try:
            r = subprocess.run(["btmgmt", "--index", "0", "class", "5", "64"],
                               capture_output=True, timeout=10, check=False)
            if r.returncode != 0:
                log("warn: setting device class failed:",
                    r.stderr.decode(errors="replace").strip() or r.returncode)
        except (OSError, subprocess.SubprocessError) as e:
            log("warn: could not run btmgmt:", e)

    def _listener(self, psm):
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.mac, psm))
        s.listen(1)
        s.setblocking(False)
        return s

    @property
    def connected(self):
        return self.ctrl is not None and self.intr is not None

    def drop(self):
        self.outq.clear()
        self._writing = False
        for s in (self.ctrl, self.intr):
            if s is None:
                continue
            if self.selector is not None:
                try:
                    self.selector.unregister(s)
                except (KeyError, ValueError):
                    pass
            try:
                s.close()
            except OSError:
                pass
        self.ctrl = self.intr = None

    # -- remembering, and re-dialling, the host --

    def _load_host(self):
        try:
            with open(HOST_FILE) as fh:
                mac = fh.read().strip()
            return mac if len(mac) == 17 else None
        except OSError:
            return None

    def remember(self, mac):
        if mac == self.host:
            return
        self.host = mac
        try:
            os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
            fd = os.open(HOST_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(mac + "\n")
            log("remembered host", mac)
        except OSError as e:
            log("warn: could not remember host:", e)

    def dial(self):
        """Reconnect to the remembered host the way a real Bluetooth keyboard
        does. Runs off-thread, because connecting to a phone that is asleep or
        out of range blocks for seconds and the input loop must keep running."""
        if self._dialing or self.connected or not self.host:
            return
        now = time.monotonic()
        if now - self._last_dial < DIAL_INTERVAL:
            return
        self._last_dial = now
        self._dialing = True
        threading.Thread(target=self._dial_worker, daemon=True).start()

    def _dial_worker(self):
        socks = []
        try:
            for psm in (P_CTRL, P_INTR):
                s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET,
                                  socket.BTPROTO_L2CAP)
                s.settimeout(DIAL_TIMEOUT)
                s.connect((self.host, psm))
                s.settimeout(None)
                socks.append(s)
            self.dialled.put(tuple(socks))
            self._dial_complained = False
        except OSError as e:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass
            # Say so once, not every eight seconds while the phone is away.
            if not self._dial_complained:
                log("could not re-dial %s: %s" % (self.host, e))
                self._dial_complained = True
        finally:
            self._dialing = False

    def adopt(self, ctrl, intr):
        """Install an outgoing connection pair as the live link."""
        if self.connected:
            for s in (ctrl, intr):
                try:
                    s.close()
                except OSError:
                    pass
            return False
        self.ctrl, self.intr = ctrl, intr
        ctrl.setblocking(False)
        intr.setblocking(False)
        if self.selector is not None:
            self.selector.register(ctrl, selectors.EVENT_READ, ("hid-ctrl", None))
        return True

    def send(self, payload):
        """payload: bytes starting with the report id."""
        if self.intr is None:
            return
        if len(self.outq) >= OUTQ_LIMIT:
            # The link is wedged. Shed the oldest rather than grow without
            # bound; a stale pointer delta matters less than a fresh one.
            self.outq.popleft()
        self.outq.append(bytes([0xA1]) + payload)
        self.flush()

    def flush(self):
        """Drain the outgoing queue. A full socket buffer is back-pressure,
        not an error: keep the report and finish when the link drains, or
        keystrokes go missing under load."""
        while self.outq:
            try:
                self.intr.send(self.outq[0])
            except (BlockingIOError, InterruptedError):
                self._watch_write(True)
                return
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS):
                    self._watch_write(True)
                    return
                log("interrupt channel send failed:", e)
                self.drop()
                return
            self.outq.popleft()
        self._watch_write(False)

    def _watch_write(self, on):
        if self.selector is None or self.intr is None or on == self._writing:
            return
        try:
            if on:
                self.selector.register(self.intr, selectors.EVENT_WRITE,
                                       ("hid-intr-w", None))
            else:
                self.selector.unregister(self.intr)
            self._writing = on
        except (KeyError, ValueError):
            pass

    def handle_control(self):
        """Minimal HIDP control-channel responder."""
        try:
            data = self.ctrl.recv(64)
        except OSError:
            data = b""
        if not data:
            log("control channel closed by host")
            self.drop()
            return
        msg = data[0]
        ttype = msg >> 4
        param = msg & 0x0F
        if ttype == 0x1 and param == 0x05:          # HIDP_CTRL virtual cable unplug
            log("host unplugged virtual cable")
            self.drop()
            return
        if ttype == 0x7:                             # SET_PROTOCOL
            # Bit 0 clear means boot protocol, where the host ignores our
            # report descriptor and assumes the fixed boot layout. Our
            # keyboard report happens to match it, but log which one the
            # host picked: it decides whether the descriptor is read at all.
            log("host set %s protocol"
                % ("report" if param & 0x01 else "BOOT"))
            reply = 0x00                             # handshake: successful
        elif ttype == 0x8:                           # SET_IDLE
            reply = 0x00
        else:
            reply = 0x03                             # ERR_UNSUPPORTED_REQUEST
        try:
            self.ctrl.send(bytes([reply]))
        except OSError:
            self.drop()


# ----------------------------------------------------------- hid reports

class ReportState:
    """Builds the HID keyboard input report and only sends it on change."""

    def __init__(self, link):
        self.link = link
        self.mods = 0
        self.keys = []          # up to 6 HID usage codes
        self._last_kbd = None

    def key(self, hid_code, modbit, pressed):
        if modbit:
            self.mods = (self.mods | modbit) if pressed else (self.mods & ~modbit)
        elif hid_code:
            if pressed:
                if hid_code not in self.keys:
                    if len(self.keys) >= 6:
                        self.keys.pop(0)
                    self.keys.append(hid_code)
            elif hid_code in self.keys:
                self.keys.remove(hid_code)
        self.flush_keyboard()

    def flush_keyboard(self):
        padded = (self.keys + [0] * 6)[:6]
        report = bytes([0x01, self.mods, 0x00] + padded)
        if report != self._last_kbd:
            self._last_kbd = report
            self.link.send(report)

    def release_all_keys(self):
        self.mods = 0
        self.keys = []
        self.flush_keyboard()

    def release_all(self):
        self.release_all_keys()


# ------------------------------------------------------------ input side

def is_keyboard(dev):
    """A real typing keyboard, not a power button or a lid switch that also
    happens to report a key or two."""
    keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
    return (ecodes.KEY_A in keys and ecodes.KEY_Z in keys
            and ecodes.KEY_SPACE in keys)



class Phonepad:
    def __init__(self, link):
        self.link = link
        self.report = ReportState(link)
        self.devices = {}         # fd -> InputDevice
        self.grabbed = False
        self.pending_grab = False
        self.held = set()
        self.esc_since = None
        # Double-tap tracking: when the toggle key went down, whether it has
        # stayed alone since, and when the last clean tap completed.
        self.tap_since = None
        self.tap_clean = False
        self.last_tap = None
        self.paste_steps = collections.deque()
        self.sel = selectors.DefaultSelector()

    # -- typing out the clipboard --
    def queue_text(self, text):
        """Turn text into a run of keyboard report states. Emitted a step at
        a time from the main loop, so a long clipboard cannot stall input."""
        shift = HID_MODMAP[ecodes.KEY_LEFTSHIFT]
        queued = 0
        for ch in text:
            entry = CHARMAP.get(ch)
            if entry is None:
                continue
            name, needs_shift = entry
            code = keymap.keytable.get(name, 0)
            if not code:
                continue
            self.paste_steps.append((shift if needs_shift else 0, code))
            self.paste_steps.append((0, 0))     # release between characters
            queued += 1
        return queued

    def pump_paste(self):
        if not self.paste_steps:
            return
        if not self.link.connected:
            self.paste_steps.clear()
            log("clipboard typing abandoned: host went away")
            return
        mods, code = self.paste_steps.popleft()
        self.link.send(bytes([0x01, mods, 0x00, code, 0, 0, 0, 0, 0]))
        if not self.paste_steps:
            # These reports bypassed ReportState, so make sure its idea of
            # what the host last saw cannot suppress the next real keystroke.
            self.report._last_kbd = None
            log("clipboard typed")

    # -- device management --
    def scan(self):
        seen = {d.path for d in self.devices.values()}
        for path in sorted(list_devices()):
            if path in seen:
                continue
            try:
                dev = InputDevice(path)
            except OSError:
                continue
            if not is_keyboard(dev):
                dev.close()
                continue
            os.set_blocking(dev.fd, False)
            self.devices[dev.fd] = dev
            self.sel.register(dev.fd, selectors.EVENT_READ, ("input", dev.fd))
            log("watching keyboard %s" % dev.name)
            if self.grabbed:
                try:
                    dev.grab()
                except OSError:
                    pass

    def forget(self, fd):
        dev = self.devices.pop(fd, None)
        if dev is None:
            return
        try:
            self.sel.unregister(fd)
        except (KeyError, ValueError):
            pass
        log("lost device", dev.name)
        try:
            dev.close()
        except OSError:
            pass

    # -- capture --
    def set_grab(self, on):
        if on == self.grabbed:
            return
        if on and not self.link.connected:
            log("refusing to capture: no host connected")
            return
        if on and self.held:
            # Wait for a clean keyboard before stealing it, otherwise the
            # desktop is left believing those keys are still held down.
            self.pending_grab = True
            return
        for dev in self.devices.values():
            try:
                dev.grab() if on else dev.ungrab()
            except OSError as e:
                log("grab/ungrab failed on %s: %s" % (dev.name, e))
        self.grabbed = on
        self.pending_grab = False
        self.report.release_all()
        log("capture", "ON  -> phone" if on else "OFF -> laptop")

    def toggle(self):
        self.set_grab(not self.grabbed)

    def double_tapped(self, code, value):
        """True when the toggle key has just completed a second solo tap.

        Anything else going down while it is held disqualifies the tap, so
        the key keeps working as an ordinary modifier: Ctrl+C twice over is
        two combinations, not two taps.
        """
        now = time.monotonic()

        if code not in TOGGLE_KEYS:
            if value == 1:
                # Anything else going down ends a combination's claim to be
                # a tap, and also breaks a half-finished pair: two taps only
                # count when nothing was typed between them.
                if self.tap_since is not None:
                    self.tap_clean = False
                self.last_tap = None
            return False

        if value == 1:
            self.tap_since = now
            # Solo means *this* key alone: subtracting the whole TOGGLE_KEYS
            # set would let one Ctrl be held down while the other was
            # tapped, which is a modifier being used, not a tap. on_key has
            # already added this key, hence the subtraction.
            self.tap_clean = not (self.held - {code})
            return False

        if value != 0 or self.tap_since is None:
            return False

        held_for = now - self.tap_since
        clean = self.tap_clean
        self.tap_since = None
        self.tap_clean = False

        # A held key or a combination is not a tap, and it also breaks any
        # half-finished double-tap, so a stray Ctrl+C cannot complete one.
        if not clean or held_for > TOGGLE_MAX_HOLD:
            self.last_tap = None
            return False

        if self.last_tap is not None and now - self.last_tap <= TOGGLE_WINDOW:
            self.last_tap = None
            return True

        self.last_tap = now
        return False

    # -- event handling --
    def on_input(self, fd):
        dev = self.devices.get(fd)
        if dev is None:
            return
        try:
            events = list(dev.read())
        except BlockingIOError:
            return
        except OSError as e:
            if e.errno in (errno.ENODEV, errno.EBADF):
                self.forget(fd)
            return

        for e in events:
            self.on_key(e)

    def on_key(self, e):
        if e.type != ecodes.EV_KEY:
            return
        code, value = e.code, e.value
        if value == 1:
            self.held.add(code)
        elif value == 0:
            self.held.discard(code)

        # Panic release: Escape held down while captured.
        if self.grabbed and code == PANIC_KEY:
            if value == 1:
                self.esc_since = time.monotonic()
            elif value == 0:
                self.esc_since = None

        if value != 2 and self.double_tapped(code, value):
            self.toggle()
            return
        if self.pending_grab and not self.held:
            self.set_grab(True)
            return
        if not self.grabbed or value == 2:
            return
        # The toggle key itself still reaches the phone as a plain
        # modifier; only the tap that completes a toggle is swallowed,
        # above, and set_grab's release_all clears it either way.
        name = ecodes.KEY.get(code)
        if isinstance(name, list):
            name = name[0]
        modbit = HID_MODMAP.get(code, 0)
        hid = 0 if modbit else keymap.keytable.get(name, 0)
        if modbit or hid:
            self.report.key(hid, modbit, value == 1)

    def tick(self):
        if self.grabbed and self.esc_since and \
                time.monotonic() - self.esc_since > PANIC_HOLD:
            log("panic release")
            self.esc_since = None
            self.set_grab(False)
        if self.grabbed and not self.link.connected:
            log("host went away; releasing capture")
            self.set_grab(False)


# ------------------------------------------------------------------ main

def control_listener():
    if os.path.exists(CONTROL_SOCKET):
        os.unlink(CONTROL_SOCKET)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(CONTROL_SOCKET)
    s.listen(4)
    s.setblocking(False)
    try:
        import grp
        os.chown(CONTROL_SOCKET, 0, grp.getgrnam(SOCKET_GROUP).gr_gid)
        os.chmod(CONTROL_SOCKET, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
    except (KeyError, OSError) as e:
        log("warn: could not set socket ownership:", e)
    return s


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "sdp_record.xml")) as fh:
        record = fh.read()

    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    loop = GLib.MainLoop()
    threading.Thread(target=loop.run, daemon=True).start()

    link = BluetoothHID(record)
    pad = Phonepad(link)
    link.selector = pad.sel
    pad.scan()

    ctl = control_listener()
    pad.sel.register(link.s_ctrl, selectors.EVENT_READ, ("accept-ctrl", None))
    pad.sel.register(link.s_intr, selectors.EVENT_READ, ("accept-intr", None))
    pad.sel.register(ctl, selectors.EVENT_READ, ("control", None))

    def shutdown(*_):
        log("shutting down")
        pad.set_grab(False)
        try:
            os.unlink(CONTROL_SOCKET)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log("waiting for a host to connect on %s (pair from the phone)" % link.mac)
    last_scan = time.monotonic()

    while True:
        # Spin faster while typing out a clipboard: one report step per
        # iteration works out to roughly 125 characters a second.
        for key, _ in pad.sel.select(timeout=0.004 if pad.paste_steps else 0.4):
            tag, fd = key.data
            if tag == "input":
                pad.on_input(fd)
            elif tag == "accept-ctrl":
                sock, info = link.s_ctrl.accept()
                if link.ctrl is not None:
                    link.drop()
                link.ctrl = sock
                sock.setblocking(False)
                pad.sel.register(sock, selectors.EVENT_READ, ("hid-ctrl", None))
                log("control channel up from", info[0])
            elif tag == "accept-intr":
                sock, info = link.s_intr.accept()
                link.intr = sock
                sock.setblocking(False)
                link.remember(info[0])
                log("interrupt channel up from", info[0], "- ready")
            elif tag == "hid-ctrl":
                link.handle_control()
            elif tag == "hid-intr-w":
                link.flush()
            elif tag == "control":
                conn, _ = ctl.accept()
                # Never let a silent client stall the input loop.
                conn.settimeout(0.5)
                buf = b""
                try:
                    while b"\n" not in buf and len(buf) < 1 << 16:
                        chunk = conn.recv(8192)
                        if not chunk:
                            break
                        buf += chunk
                except (OSError, socket.timeout):
                    pass
                cmd = buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
                if cmd == "toggle":
                    pad.toggle()
                elif cmd == "on":
                    pad.set_grab(True)
                elif cmd == "off":
                    pad.set_grab(False)
                elif cmd == "test":
                    # Tap and release Shift: it reaches the host as a real
                    # keystroke but types nothing, so the HID path can be
                    # proven without grabbing the only keyboard we have.
                    if link.connected:
                        log("sending test keystroke (shift)")
                        pad.report.key(0, HID_MODMAP[ecodes.KEY_LEFTSHIFT], True)
                        time.sleep(0.05)
                        pad.report.key(0, HID_MODMAP[ecodes.KEY_LEFTSHIFT], False)
                elif cmd.startswith("paste "):
                    if link.connected:
                        try:
                            text = base64.b64decode(cmd[6:], validate=True)
                            text = text.decode("utf-8", "replace")
                        except (ValueError, UnicodeDecodeError):
                            text = ""
                        dropped = len(text) > PASTE_LIMIT
                        n = pad.queue_text(text[:PASTE_LIMIT])
                        log("typing %d characters from the clipboard%s"
                            % (n, " (truncated)" if dropped else ""))
                state = "on" if pad.grabbed else "off"
                link_state = "connected" if link.connected else "waiting"
                try:
                    conn.send(("%s %s\n" % (state, link_state)).encode())
                except OSError:
                    pass
                conn.close()

        # Pick up a connection the dialler managed to open, then keep trying.
        try:
            ctrl, intr = link.dialled.get_nowait()
        except queue.Empty:
            pass
        else:
            if link.adopt(ctrl, intr):
                log("re-dialled host", link.host, "- ready")
        link.dial()

        pad.pump_paste()
        pad.tick()
        if time.monotonic() - last_scan > 3.0:
            pad.scan()
            last_scan = time.monotonic()


if __name__ == "__main__":
    main()
