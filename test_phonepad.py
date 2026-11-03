#!/usr/bin/env python3
"""Exercise phonepad's keyboard report building, device selection and
outgoing queue with synthetic data. No Bluetooth, no root, no real devices."""

import collections
import errno
import os
import re
import sys
import time

import phonepad
from evdev import ecodes

failures = []


def check(label, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + label +
          (" :: " + detail if detail and not cond else ""))
    if not cond:
        failures.append(label)


class FakeLink:
    def __init__(self):
        self.sent = []
        self.connected = True

    def send(self, payload):
        self.sent.append(payload)


def hid(name):
    return phonepad.keymap.keytable[name]


print("keyboard report format")
link = FakeLink()
rs = phonepad.ReportState(link)

rs.key(hid("KEY_A"), 0, True)
check("report is 9 bytes", len(link.sent[-1]) == 9, "got %d" % len(link.sent[-1]))
check("report id is 1", link.sent[-1][0] == 0x01, repr(link.sent[-1]))
check("usage 0x04 for A lands in the first key slot", link.sent[-1][3] == 4,
      repr(link.sent[-1]))
check("reserved byte is zero", link.sent[-1][2] == 0)

rs.key(0, phonepad.HID_MODMAP[ecodes.KEY_LEFTSHIFT], True)
check("left shift sets modifier bit 0x02", link.sent[-1][1] == 0x02,
      repr(link.sent[-1]))
check("shift does not disturb the held key", link.sent[-1][3] == 4,
      repr(link.sent[-1]))

rs.key(0, phonepad.HID_MODMAP[ecodes.KEY_RIGHTALT], True)
check("a second modifier ors in", link.sent[-1][1] == 0x42, hex(link.sent[-1][1]))
rs.key(0, phonepad.HID_MODMAP[ecodes.KEY_RIGHTALT], False)
check("releasing one modifier keeps the other", link.sent[-1][1] == 0x02,
      hex(link.sent[-1][1]))

n = len(link.sent)
rs.key(hid("KEY_A"), 0, True)
check("re-pressing a held key sends nothing new", len(link.sent) == n)

rs.release_all()
check("release_all clears everything",
      link.sent[-1] == bytes([1, 0, 0, 0, 0, 0, 0, 0, 0]), repr(link.sent[-1]))
n = len(link.sent)
rs.release_all()
check("an unchanged report is not resent", len(link.sent) == n)

print()
print("rollover")
link = FakeLink()
rs = phonepad.ReportState(link)
for name in ("KEY_A", "KEY_B", "KEY_C", "KEY_D", "KEY_E", "KEY_F"):
    rs.key(hid(name), 0, True)
check("six keys all fit in one report",
      list(link.sent[-1][3:]) == [hid(k) for k in
                                  ("KEY_A", "KEY_B", "KEY_C", "KEY_D", "KEY_E", "KEY_F")],
      repr(link.sent[-1]))
rs.key(hid("KEY_G"), 0, True)
check("a seventh key evicts the oldest", hid("KEY_A") not in link.sent[-1][3:]
      and hid("KEY_G") in link.sent[-1][3:], repr(link.sent[-1]))
rs.key(hid("KEY_C"), 0, False)
check("releasing a key removes just that one",
      hid("KEY_C") not in link.sent[-1][3:] and hid("KEY_D") in link.sent[-1][3:],
      repr(link.sent[-1]))

print()
print("typing a word produces the right sequence")
link = FakeLink()
rs = phonepad.ReportState(link)
for name in ("KEY_H", "KEY_I"):
    rs.key(hid(name), 0, True)
    rs.key(hid(name), 0, False)
pressed = [r[3] for r in link.sent if r[3] != 0]
check("each letter appears once, in order",
      pressed == [hid("KEY_H"), hid("KEY_I")], str(pressed))
check("every press is followed by a release",
      len(link.sent) == 4, "%d reports" % len(link.sent))

print()
print("device selection")


class StubDev:
    def __init__(self, keys):
        self._keys = keys

    def capabilities(self):
        return {ecodes.EV_KEY: self._keys}


full = [ecodes.KEY_A, ecodes.KEY_Z, ecodes.KEY_SPACE, ecodes.KEY_ENTER]
check("a real keyboard is accepted", phonepad.is_keyboard(StubDev(full)))
check("a power button is rejected",
      not phonepad.is_keyboard(StubDev([ecodes.KEY_POWER])))
check("a mouse is rejected",
      not phonepad.is_keyboard(StubDev([ecodes.BTN_LEFT, ecodes.BTN_RIGHT])))
check("a partial key set is rejected",
      not phonepad.is_keyboard(StubDev([ecodes.KEY_A, ecodes.KEY_Z])))

print()
print("outgoing queue survives back-pressure")


class StubSocket:
    """Refuses the first `block` sends with EAGAIN, like a full L2CAP buffer."""

    def __init__(self, block):
        self.block = block
        self.sent = []

    def send(self, data):
        if self.block > 0:
            self.block -= 1
            raise BlockingIOError(errno.EAGAIN, "would block")
        self.sent.append(data)
        return len(data)

    def close(self):
        pass


def bare_link(sock):
    link = object.__new__(phonepad.BluetoothHID)
    link.ctrl = None
    link.intr = sock
    link.selector = None
    link.outq = collections.deque()
    link._writing = False
    return link


# Two refusals: one for each send() below, leaving the explicit flush() to
# find the socket writable again.
link = bare_link(StubSocket(block=2))
link.send(bytes([0x01, 0, 0, 4, 0, 0, 0, 0, 0]))
check("report is retained when the socket blocks", len(link.outq) == 1,
      "queue=%d" % len(link.outq))
check("link is not torn down by EAGAIN", link.intr is not None)

link.send(bytes([0x01, 0, 0, 5, 0, 0, 0, 0, 0]))
check("a second report queues behind it", len(link.outq) == 2, str(len(link.outq)))

link.flush()
check("both reports go out once the socket drains", len(link.intr.sent) == 2,
      "sent=%d queue=%d" % (len(link.intr.sent), len(link.outq)))
check("queue is empty afterwards", not link.outq)
# 0xa1 HIDP header, 0x01 report id, modifiers, reserved, then the keycodes.
check("reports kept their order and HIDP header",
      link.intr.sent[0][0] == 0xA1 and link.intr.sent[0][1] == 0x01 and
      link.intr.sent[0][4] == 4 and link.intr.sent[1][4] == 5,
      repr(link.intr.sent))

# A link that never drains must not grow the queue without bound.
link = bare_link(StubSocket(block=10 ** 9))
for i in range(phonepad.OUTQ_LIMIT + 50):
    link.send(bytes([0x01, 0, 0, i % 100, 0, 0, 0, 0, 0]))
check("queue stays bounded on a wedged link",
      len(link.outq) <= phonepad.OUTQ_LIMIT, "queue=%d" % len(link.outq))
check("the newest report is the one kept",
      link.outq[-1][4] == (phonepad.OUTQ_LIMIT + 49) % 100, repr(link.outq[-1]))


class DeadSocket:
    def send(self, data):
        raise OSError(errno.ECONNRESET, "connection reset")

    def close(self):
        pass


link = bare_link(DeadSocket())
link.send(bytes([0x01, 0, 0, 4, 0, 0, 0, 0, 0]))
check("a genuinely dead socket does drop the link", link.intr is None)

print()
print("typing out the clipboard")

link = FakeLink()
pad = phonepad.Phonepad(link)

missing = [c for c in
           "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
           " !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~\n\t"
           if c not in phonepad.CHARMAP]
check("every printable ASCII character is mappable", not missing, repr(missing))

n = pad.queue_text("Hi!")
check("three characters queue three press/release pairs",
      n == 3 and len(pad.paste_steps) == 6,
      "n=%d steps=%d" % (n, len(pad.paste_steps)))

while pad.paste_steps:
    pad.pump_paste()
check("one report per step", len(link.sent) == 6, "%d reports" % len(link.sent))

shift = phonepad.HID_MODMAP[ecodes.KEY_LEFTSHIFT]
check("capital H is shifted", link.sent[0][1] == shift and
      link.sent[0][3] == hid("KEY_H"), repr(link.sent[0]))
check("lowercase i is not shifted", link.sent[2][1] == 0 and
      link.sent[2][3] == hid("KEY_I"), repr(link.sent[2]))
check("exclamation mark is shift+1", link.sent[4][1] == shift and
      link.sent[4][3] == hid("KEY_1"), repr(link.sent[4]))
check("every character is released after it",
      all(link.sent[i][1] == 0 and link.sent[i][3] == 0 for i in (1, 3, 5)),
      repr(link.sent[1::2]))
check("finishing a paste resets the dedup cache", pad.report._last_kbd is None)

link.sent.clear()
pad.paste_steps.clear()
n = pad.queue_text("aéb中c")
check("unmappable characters are skipped, not mistyped", n == 3,
      "queued %d" % n)

link.sent.clear()
pad.paste_steps.clear()
pad.queue_text("xy")
link.connected = False
pad.pump_paste()
check("a paste in flight is abandoned when the host goes away",
      not pad.paste_steps)

print()
print("toggle by double-tapping the toggle key")


class StubEvent:
    type = ecodes.EV_KEY

    def __init__(self, code, value):
        self.code, self.value = code, value


RC = ecodes.KEY_RIGHTCTRL
LC = ecodes.KEY_LEFTCTRL
OTHER = ecodes.KEY_C
toggles = []


def fresh():
    """A pad with the tap state clean, so one case cannot seed the next."""
    pad = phonepad.Phonepad(FakeLink())
    pad.toggle = lambda: toggles.append(1)
    toggles.clear()
    return pad


def tap(pad, code=RC, gap=0.0, hold=0.0):
    """One solo press/release, with optional waits, through on_key."""
    if gap:
        time.sleep(gap)
    pad.on_key(StubEvent(code, 1))
    if hold:
        time.sleep(hold)
    pad.on_key(StubEvent(code, 0))


def combo(pad):
    """The toggle key used as a real modifier: RightCtrl+C."""
    pad.on_key(StubEvent(RC, 1))
    pad.on_key(StubEvent(OTHER, 1))
    pad.on_key(StubEvent(OTHER, 0))
    pad.on_key(StubEvent(RC, 0))


pad = fresh()
tap(pad)
check("a single tap does not toggle", not toggles, "%d toggles" % len(toggles))
tap(pad)
check("a second tap inside the window toggles", len(toggles) == 1,
      "%d toggles" % len(toggles))

pad = fresh()
tap(pad)
tap(pad)
tap(pad)
tap(pad)
check("four taps toggle twice, not three times", len(toggles) == 2,
      "%d toggles" % len(toggles))

pad = fresh()
tap(pad)
tap(pad, gap=phonepad.TOGGLE_WINDOW + 0.15)
check("a second tap after the window does not toggle", not toggles,
      "%d toggles" % len(toggles))
tap(pad)
check("that late tap still starts a fresh pair", len(toggles) == 1,
      "%d toggles" % len(toggles))

pad = fresh()
combo(pad)
combo(pad)
check("RightCtrl+C twice is not a double-tap", not toggles,
      "%d toggles" % len(toggles))

pad = fresh()
tap(pad)
combo(pad)
tap(pad)
check("a combination breaks a half-finished pair", not toggles,
      "%d toggles" % len(toggles))

pad = fresh()
tap(pad)
tap(pad, hold=phonepad.TOGGLE_MAX_HOLD + 0.15)
check("holding the key is not a tap", not toggles, "%d toggles" % len(toggles))

pad = fresh()
tap(pad)
pad.on_key(StubEvent(RC, 2))
pad.on_key(StubEvent(RC, 2))
check("autorepeat is not a second tap", not toggles,
      "%d toggles" % len(toggles))

pad = fresh()
tap(pad)
tap(pad, code=ecodes.KEY_X)
tap(pad)
check("a keystroke between taps breaks the pair", not toggles,
      "%d toggles" % len(toggles))

pad = fresh()
tap(pad, code=LC)
tap(pad, code=LC)
check("double-tapping LEFT ctrl also toggles", len(toggles) == 1,
      "%d toggles" % len(toggles))

pad = fresh()
tap(pad, code=LC)
tap(pad, code=RC)
check("a left tap then a right tap counts as a pair", len(toggles) == 1,
      "%d toggles" % len(toggles))

# Left Ctrl is in constant use, so the solo rule has to hold for it too.
pad = fresh()
for _ in range(2):
    pad.on_key(StubEvent(LC, 1))
    pad.on_key(StubEvent(OTHER, 1))
    pad.on_key(StubEvent(OTHER, 0))
    pad.on_key(StubEvent(LC, 0))
check("LeftCtrl+C twice is not a double-tap", not toggles,
      "%d toggles" % len(toggles))

pad = fresh()
tap(pad, code=LC)
tap(pad, code=ecodes.KEY_X)
tap(pad, code=LC)
check("typing between left-ctrl taps breaks the pair", not toggles,
      "%d toggles" % len(toggles))

# Holding Left Ctrl while tapping the other one is a combination, not taps.
pad = fresh()
pad.on_key(StubEvent(LC, 1))
tap(pad, code=RC)
tap(pad, code=RC)
pad.on_key(StubEvent(LC, 0))
check("one ctrl held while tapping the other does not toggle", not toggles,
      "%d toggles" % len(toggles))

check("the old chord machinery is gone",
      not hasattr(phonepad, "TOGGLE_CHORD") and
      not hasattr(phonepad, "CHORD_ALIASES") and
      not hasattr(phonepad.Phonepad, "chord_held"))
check("both ctrl keys are registered as triggers",
      phonepad.TOGGLE_KEYS == frozenset((ecodes.KEY_LEFTCTRL,
                                         ecodes.KEY_RIGHTCTRL)))

print()
print("startup waits for the bluetooth controller")

# At boot, bluetoothd answers on D-Bus before the controller is registered.
# The daemon must retry rather than exit and be restarted in a loop.
try:
    import dbus
    import time as _time

    probe = object.__new__(phonepad.BluetoothHID)
    probe.bus = dbus.SystemBus()
    probe.adapter_path = "/org/bluez/hci_does_not_exist"
    original = phonepad.ADAPTER_WAIT
    phonepad.ADAPTER_WAIT = 2.5
    start = _time.monotonic()
    try:
        probe._wait_for_adapter()
        check("a missing controller is eventually reported", False,
              "returned an adapter that cannot exist")
    except dbus.DBusException:
        waited = _time.monotonic() - start
        check("it retries instead of dying immediately", waited >= 2.0,
              "gave up after %.2fs" % waited)
        check("it does give up rather than hang forever", waited < 10.0,
              "waited %.2fs" % waited)
    finally:
        phonepad.ADAPTER_WAIT = original
except ImportError:
    print("  skip   (no dbus module)")
except Exception as e:                                   # no system bus here
    print("  skip   (no system bus: %s)" % e)

print()
print("link state")

probe_link = object.__new__(phonepad.BluetoothHID)
probe_link.ctrl = probe_link.intr = None
probe_link.outq = collections.deque()
check("connected is a property, not a method that is always truthy",
      probe_link.connected is False, repr(probe_link.connected))
probe_link.ctrl = probe_link.intr = object()
check("and it goes true once both channels exist", probe_link.connected is True)

print()
print("keymap coverage")
missing = [n for n in ("KEY_A", "KEY_ENTER", "KEY_TAB", "KEY_BACKSPACE", "KEY_ESC",
                       "KEY_LEFT", "KEY_RIGHT", "KEY_UP", "KEY_DOWN", "KEY_F1",
                       "KEY_SPACE", "KEY_DOT", "KEY_COMMA", "KEY_APOSTROPHE",
                       "KEY_SLASH", "KEY_HOME", "KEY_END", "KEY_SEMICOLON")
           if n not in phonepad.keymap.keytable]
check("common keys all present in the keymap", not missing, str(missing))
check("every modifier maps to a distinct HID bit",
      len(set(phonepad.HID_MODMAP.values())) == 8)
check("no pointer machinery left behind",
      not hasattr(phonepad.ReportState, "motion") and
      not hasattr(phonepad, "is_pointer") and
      not hasattr(phonepad, "MOUSE_BUTTONS"))

_xml = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "sdp_record.xml")).read()
_hex = re.search(r'encoding="hex" value="([0-9a-f]+)"', _xml)
_desc = bytes.fromhex(_hex.group(1)) if _hex else b""
_ids = [_desc[i + 1] for i in range(len(_desc) - 1) if _desc[i] == 0x85]
check("the shipped descriptor declares the keyboard report and nothing else",
      _ids == [0x01], str(_ids))
check("and the SDP record calls itself a keyboard",
      '<attribute id="0x0202"><uint8 value="0x40" />' in _xml)

print()
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all checks passed")
