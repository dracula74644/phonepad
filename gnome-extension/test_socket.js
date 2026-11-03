// Exercises exactly the Gio async socket code the extension uses, outside
// GNOME Shell, against the live phonepad daemon.
//   gjs -m test_socket.js

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

const SOCKET_PATH = '/run/phonepad.sock';
const loop = new GLib.MainLoop(null, false);
let failures = 0;

function check(label, cond, detail) {
    print((cond ? '  ok   ' : '  FAIL ') + label + (cond ? '' : ' :: ' + detail));
    if (!cond)
        failures++;
}

function converse(command, done) {
    const client = new Gio.SocketClient();
    const address = new Gio.UnixSocketAddress({path: SOCKET_PATH});

    client.connect_async(address, null, (_s, res) => {
        let connection;
        try {
            connection = client.connect_finish(res);
        } catch (e) {
            done(null, 'connect: ' + e.message);
            return;
        }

        const out = connection.get_output_stream();
        const bytes = new TextEncoder().encode(command);

        out.write_all_async(bytes, GLib.PRIORITY_DEFAULT, null, (_o, wres) => {
            try {
                out.write_all_finish(wres);
            } catch (e) {
                done(null, 'write: ' + e.message);
                return;
            }

            const reader = new Gio.DataInputStream({
                base_stream: connection.get_input_stream(),
            });
            reader.read_line_async(GLib.PRIORITY_DEFAULT, null, (_r, rres) => {
                let line = null;
                let err = null;
                try {
                    const [raw] = reader.read_line_finish_utf8(rres);
                    line = raw;
                } catch (e) {
                    err = 'read: ' + e.message;
                }
                try {
                    connection.close(null);
                } catch (_e) {}
                done(line, err);
            });
        });
    });
}

print('extension socket path');
converse('status', (line, err) => {
    check('status round-trip succeeds', line !== null, err || 'no line');
    if (line) {
        const parts = line.trim().split(/\s+/);
        check('reply has two fields', parts.length === 2, JSON.stringify(line));
        check('capture field is on/off', ['on', 'off'].includes(parts[0]), parts[0]);
        check('host field is known', ['connected', 'waiting'].includes(parts[1]), parts[1]);
        print('  -> daemon says: capture=' + parts[0] + ' host=' + parts[1]);
    }

    // Second call proves the daemon closes cleanly and accepts a new client.
    converse('status', (line2, err2) => {
        check('second round-trip succeeds', line2 !== null, err2 || 'no line');
        check('replies are consistent', line2 === line,
              JSON.stringify(line) + ' vs ' + JSON.stringify(line2));

        // A bad command must not wedge anything.
        converse('bogus', (line3, err3) => {
            check('unknown command still answers', line3 !== null, err3 || 'no line');
            print('');
            print(failures ? failures + ' FAILED' : 'all checks passed');
            loop.quit();
        });
    });
});

GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 10, () => {
    print('TIMED OUT');
    failures++;
    loop.quit();
    return GLib.SOURCE_REMOVE;
});

loop.run();
if (failures)
    imports.system.exit(1);
