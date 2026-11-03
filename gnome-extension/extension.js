/* Phonepad top bar toggle.
 *
 * A switch that sits directly in the panel -- one click flips the keyboard
 * between this laptop and the paired phone, with no menu to open.
 *
 * Talks to the phonepad daemon over its unix control socket: one line in
 * ("status" / "toggle"), one line back ("<capture> <host>"). Polling keeps
 * the switch honest when the Ctrl double-tap is used instead of the button.
 */

import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const SOCKET_PATH = '/run/phonepad.sock';
const POLL_SECONDS = 2;

const PhonepadButton = GObject.registerClass(
class PhonepadButton extends PanelMenu.Button {
    _init() {
        // Third argument suppresses the popup menu: a click should toggle,
        // not open something.
        super._init(0.0, 'Phonepad', true);

        this._icon = new St.Icon({
            icon_name: 'input-keyboard-symbolic',
            style_class: 'system-status-icon',
            y_align: Clutter.ActorAlign.CENTER,
        });

        // The knob is the track's only child; aligning the track's content
        // left or right is what slides it. Drawn from St primitives rather
        // than a shell-internal widget, so it cannot break on an API change.
        this._knob = new St.Bin({style_class: 'phonepad-knob'});
        this._track = new St.Bin({
            style_class: 'phonepad-track',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._track.set_child(this._knob);

        const box = new St.BoxLayout({
            style_class: 'phonepad-box',
            y_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(this._icon);
        box.add_child(this._track);
        this.add_child(box);

        this._capture = 'off';
        this._host = 'down';
        this._busy = false;
        this._cancellable = new Gio.Cancellable();
        this._render();
    }

    vfunc_event(event) {
        const type = event.type();
        if (type === Clutter.EventType.BUTTON_PRESS ||
            type === Clutter.EventType.TOUCH_BEGIN) {
            this._send('toggle');
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    poll() {
        this._send('status');
    }

    _send(command) {
        // Drop the request rather than queue: the next poll is 2s away.
        if (this._busy)
            return;
        this._busy = true;

        const client = new Gio.SocketClient();
        const address = new Gio.UnixSocketAddress({path: SOCKET_PATH});

        client.connect_async(address, this._cancellable, (_src, res) => {
            let connection;
            try {
                connection = client.connect_finish(res);
            } catch (_e) {
                this._finish(null);
                return;
            }
            this._write(connection, command);
        });
    }

    _write(connection, command) {
        const stream = connection.get_output_stream();
        const bytes = new TextEncoder().encode(command + '\n');

        stream.write_all_async(bytes, GLib.PRIORITY_DEFAULT, this._cancellable,
            (_src, res) => {
                try {
                    stream.write_all_finish(res);
                } catch (_e) {
                    this._finish(null, connection);
                    return;
                }
                this._read(connection);
            });
    }

    _read(connection) {
        const reader = new Gio.DataInputStream({
            base_stream: connection.get_input_stream(),
        });

        reader.read_line_async(GLib.PRIORITY_DEFAULT, this._cancellable,
            (_src, res) => {
                let line = null;
                try {
                    const [raw] = reader.read_line_finish_utf8(res);
                    line = raw;
                } catch (_e) {
                    // fall through with line === null
                }
                this._finish(line, connection);
            });
    }

    _finish(line, connection) {
        if (connection) {
            try {
                connection.close(null);
            } catch (_e) {
                // nothing useful to do
            }
        }

        if (line) {
            const parts = line.trim().split(/\s+/);
            this._capture = parts[0] || 'off';
            this._host = parts[1] || 'down';
        } else {
            // Daemon unreachable: stopped, or we are not in its socket group.
            this._capture = 'off';
            this._host = 'down';
        }

        this._busy = false;
        this._render();
    }

    _render() {
        const connected = this._host === 'connected';
        const on = connected && this._capture === 'on';

        this._track.remove_style_class_name('phonepad-on');
        this._track.remove_style_class_name('phonepad-unavailable');

        if (!connected) {
            this._track.add_style_class_name('phonepad-unavailable');
            this._icon.icon_name = 'bluetooth-disabled-symbolic';
            this._icon.opacity = 110;
            this.accessible_name = this._host === 'down'
                ? 'Phonepad off: daemon not running'
                : 'Phonepad off: no phone connected';
        } else if (on) {
            this._track.add_style_class_name('phonepad-on');
            this._icon.icon_name = 'phone-symbolic';
            this._icon.opacity = 255;
            this.accessible_name = 'Phonepad on: keyboard going to the phone';
        } else {
            this._icon.icon_name = 'input-keyboard-symbolic';
            this._icon.opacity = 255;
            this.accessible_name = 'Phonepad off: keyboard staying on the laptop';
        }

        // Slide the knob by setting its left margin outright. Alignment
        // inside an St.Bin depends on expand flags that are easy to get
        // subtly wrong; an explicit offset is unambiguous.
        // Track is 32px wide, knob 12px, so 2px sits left and 18px sits right.
        this._knob.set_style(`margin-left: ${on ? 18 : 2}px;`);
    }

    destroy() {
        this._cancellable.cancel();
        super.destroy();
    }
});

export default class PhonepadExtension extends Extension {
    enable() {
        this._button = new PhonepadButton();
        Main.panel.addToStatusArea('phonepad', this._button, 0, 'right');

        this._timer = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, POLL_SECONDS, () => {
                this._button?.poll();
                return GLib.SOURCE_CONTINUE;
            });

        this._button.poll();
    }

    disable() {
        if (this._timer) {
            GLib.Source.remove(this._timer);
            this._timer = null;
        }
        this._button?.destroy();
        this._button = null;
    }
}
