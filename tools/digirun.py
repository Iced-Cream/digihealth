# SPDX-License-Identifier: GPL-2.0-or-later
"""One program on a Digitakt's or Digitone's USB MIDI port: keys, screenshots,
notes and CCs and the data store, in one session, so a step can look at the
screen while a key is held. --port NAME (before the command) picks the unit
by part of its port name when both are connected; --port tcp:HOST:PORT talks
to an emulator's USB MIDI link instead (digiemu's tools/usbrun.py).

    python digirun.py shot OUT.png [--scale N]       the screen now (stock OS too)
    python digirun.py run SCRIPT [--out DIR]         steps from a file ('-' = stdin)
    python digirun.py do 'hold FUNC; shot f.png; release FUNC'
    python digirun.py info                           product, OS version
    python digirun.py data ls PATH                   the data store: '/' lists the roots
    python digirun.py data get PATH OUT              one object, as Transfer stores it

Steps (one per line or separated by ';'; '#' starts a comment):

    tap KEY [MS]          press and release (a digihealth build with REMOTE KEYS)
    hold KEY [MS]         press; the firmware lets go after MS (default its maximum, 2 s)
    release KEY
    keys SEQ              as digiusb.py keys: 'SETTINGS,DOWN,FUNC(YES)'
    turn ENC DELTA [STEP] encoder turn, where the digihealth build has them
    shot FILE             screenshot: .png (4x) or .bin (raw framebuffer)
    expect-screen NAME [S] [rows=A-B] [settle]
                          stop unless the screen equals NAME.bin, retrying for S
                          seconds; a mismatch reports the differing rows and columns
                          and saves NAME-got.png and NAME-diff.png. rows=A-B compares
                          rows A..B only; settle: a match must still hold 0.4 s later
    listen TEXT           the person listens for TEXT: nothing is sent until they have
                          finished (Enter; or, with DIGIRUN_LISTEN_FILE or
                          ANALOGRUN_LISTEN_FILE set, until a host removes that file)
    cc CH NUM VAL         MIDI channel 1-16
    nrpn CH MSB LSB VAL   VAL 0-16383 (CC 99, 98, 6, 38)
    note CH NOTE VEL [MS] note on, off after MS (default 100)
    wait S
    stats                 one digihealth CPU/DSP/heap line
    dump ADDR LEN FILE    DDR to a file (digihealth)
    say TEXT              print TEXT

Screenshots and the data store use Elektron's own RPC (elkrpc.py), which the
stock OS answers. Key steps open a digihealth key session on first use and
close it (releasing any synthetic key) at the end. Each command here is the
only user of the port while it runs: close Transfer, Overbridge's MIDI and
digiusb.py first.
"""
import argparse
import json
import os
import shlex
import sys
import time

import elkrpc
import midisock
from digiusb import Diag, RemoteKeys, decode_stats, key_sequence, peek, req_stats


def say(*args, **kw):
    kw.setdefault('flush', True)
    print(*args, **kw)


def screen_diff(got, want, w=128, h=64, rows=None):
    """Two framebuffers -> (differing pixels as (y, x), a short text report); rows=(A, B)
    compares only rows A..B."""
    g, r = elkrpc.Screen(w, h, got).rows(), elkrpc.Screen(w, h, want).rows()
    y_lo, y_hi = rows or (0, h - 1)
    diff = [(y, x) for y in range(y_lo, y_hi + 1) for x in range(w) if g[y][x] != r[y][x]]
    if not diff:
        return diff, 'identical'
    ys, xs = [d[0] for d in diff], [d[1] for d in diff]
    y0, y1, x0, x1 = min(ys), max(ys), min(xs), max(xs)
    text = '%d pixels differ in rows %d-%d, columns %d-%d' % (len(diff), y0, y1, x0, x1)
    if y1 - y0 < 12 and x1 - x0 < 64:          # small enough to show: + lit only now, - only before
        art = []
        for y in range(y0, y1 + 1):
            art.append(''.join('+' if g[y][x] and not r[y][x] else '-' if r[y][x] and not g[y][x]
                               else '#' if g[y][x] else '.' for x in range(x0, x1 + 1)))
        text += '\n    ' + '\n    '.join(art)
    return diff, text


def diff_png(w, h, diff):
    """A PNG with only the differing pixels lit."""
    stride = (h + 31) // 32
    cols = [0] * w
    for y, x in diff:
        cols[x] |= 1 << (stride * 32 - h + y)
    pixels = b''.join(c.to_bytes(stride * 4, 'big') for c in cols)
    return elkrpc.Screen(w, h, pixels).png(4)


class Session:
    def __init__(self, port, out='.'):
        self.port = port
        self.rpc = elkrpc.Rpc(port)
        self.diag = Diag(port)
        self.out = out
        self.refs = None                 # folder of reference screens (.bin), for test plans
        self.record_refs = False         # expect-screen saves the screen as the reference
        self._keys = None

    @property
    def keys(self):
        if self._keys is None:
            self._keys = RemoteKeys(self.diag)
        return self._keys

    def close(self):
        if self._keys is not None:
            self._keys.cancel()
            self._keys = None

    def path(self, name):
        return name if os.path.isabs(name) else os.path.join(self.out, name)

    def shot(self, name, scale=4):
        screen = self.rpc.screenshot()
        with open(self.path(name), 'wb') as fh:
            fh.write(screen.pixels if name.endswith('.bin') else screen.png(scale))
        return screen

    def cc(self, ch, num, val):
        self.port.send_short(0xB0 | (ch - 1), num, val)

    def nrpn(self, ch, msb, lsb, val):
        for num, v in ((99, msb), (98, lsb), (6, val >> 7), (38, val & 0x7F)):
            self.cc(ch, num, v)

    def note(self, ch, note, vel, ms=100):
        self.port.send_short(0x90 | (ch - 1), note, vel)
        time.sleep(ms / 1000)
        self.port.send_short(0x80 | (ch - 1), note, 0)

    def key_steps(self, text):
        for hold, tap in key_sequence(text):
            if hold:
                self.keys.press(hold)
            self.keys.tap(tap)
            if hold:
                self.keys.release(hold)

    def expect_screen(self, ref, wait=0.0, rows=None, settle=False):
        """Stop unless the screen equals the framebuffer in `ref` (in the refs folder if set),
        retrying for `wait` s; rows=(A, B) compares only those rows; settle: a match counts
        only when it still holds 0.4 s later. With record_refs, save the screen as `ref`."""
        ref = ref if ref.endswith('.bin') else ref + '.bin'
        refpath = os.path.join(self.refs, ref) if self.refs and not os.path.isabs(ref) else self.path(ref)
        got = self.rpc.screenshot()
        if self.record_refs:
            os.makedirs(os.path.dirname(refpath) or '.', exist_ok=True)
            with open(refpath, 'wb') as fh:
                fh.write(got.pixels)
            with open(os.path.splitext(refpath)[0] + '.png', 'wb') as fh:
                fh.write(got.png(4))
            say('  recorded %s' % refpath)
            return
        with open(refpath, 'rb') as fh:
            want = fh.read()
        end = time.monotonic() + wait

        def same(g):
            return g.pixels == want if rows is None else not screen_diff(g.pixels, want, g.w, g.h, rows)[0]
        while True:
            while not same(got) and time.monotonic() < end:
                time.sleep(0.2)
                got = self.rpc.screenshot()
            if not settle or not same(got):
                break
            time.sleep(0.4)
            got = self.rpc.screenshot()
            if same(got) or time.monotonic() >= end:
                break
        if not same(got):
            diff, text = screen_diff(got.pixels, want, got.w, got.h, rows)
            stem = self.path(os.path.splitext(os.path.basename(ref))[0])
            with open(stem + '-got.png', 'wb') as fh:
                fh.write(got.png(4))
            with open(stem + '-diff.png', 'wb') as fh:
                fh.write(diff_png(got.w, got.h, diff))
            raise AssertionError('screen differs from %s: %s (saved %s-got.png, -diff.png)'
                                 % (ref, text, os.path.basename(stem)))

    def step(self, line):
        words = shlex.split(line, comments=True)
        if not words:
            return
        op, a = words[0].lower(), words[1:]
        num = lambda s: int(s, 0)
        if op == 'tap':
            self.keys.tap(a[0], num(a[1]) if len(a) > 1 else None)
        elif op == 'hold':
            self.keys.press(a[0], num(a[1]) if len(a) > 1 else None)
        elif op == 'release':
            self.keys.release(a[0])
        elif op == 'keys':
            self.key_steps(' '.join(a))
        elif op == 'turn':
            if not hasattr(self.keys, 'turn'):
                raise ValueError('this digiusb.py has no encoder turns')
            self.keys.turn(a[0].upper(), num(a[1]), num(a[2]) if len(a) > 2 else 1)
        elif op == 'shot':
            s = self.shot(a[0])
            say('  %s: %dx%d, %d lit' % (a[0], s.w, s.h, s.lit()))
        elif op == 'expect-screen':
            settle = 'settle' in a[1:]
            opts = [w for w in a[1:] if w.startswith('rows=')]
            a = a[:1] + [w for w in a[1:] if w != 'settle' and not w.startswith('rows=')]
            rows = None
            if opts:
                try:
                    rows = tuple(int(v) for v in opts[0][5:].split('-'))
                    assert len(rows) == 2 and 0 <= rows[0] <= rows[1] <= 63
                except (ValueError, AssertionError):
                    raise ValueError('rows=A-B with 0 <= A <= B <= 63, not %r' % opts[0])
            self.expect_screen(a[0], float(a[1]) if len(a) > 1 else 0.0, rows, settle)
        elif op == 'cc':
            self.cc(*map(num, a))
        elif op == 'nrpn':
            self.nrpn(*map(num, a))
        elif op == 'note':
            self.note(*map(num, a))
        elif op == 'wait':
            time.sleep(float(a[0]))
        elif op == 'stats':
            s = decode_stats(self.diag.call(req_stats))
            say('  second %d: CPU %.1f %%, DSP %.1f %% (peak %.1f %%), heap free %d' % (
                s['seconds'], 100 * s.get('cpu', 0), 100 * s.get('dsp', 0),
                100 * s.get('dsp_peak', 0), s['heap_free']))
        elif op == 'dump':
            data = peek(self.diag, num(a[0]), num(a[1]))
            with open(self.path(a[2]), 'wb') as fh:
                fh.write(data)
        elif op == 'say':
            say(' '.join(a))
        elif op == 'listen':
            # The person listens to the unit: nothing more is sent until they say they have
            # finished, however long that takes.
            self.close()                     # no synthetic key stays held meanwhile
            what = ' '.join(a)
            say('  listening: %s' % what)
            flag = os.environ.get('DIGIRUN_LISTEN_FILE') or os.environ.get('ANALOGRUN_LISTEN_FILE')
            if flag:
                # A host that asks the person itself: it removes the file when they have finished.
                with open(flag, 'w', encoding='utf-8') as fh:
                    json.dump({'what': what, 'since': time.time()}, fh)
                while os.path.exists(flag):
                    time.sleep(0.5)
            else:
                input('  press Enter when you have finished listening ')
            say('  finished listening')
        else:
            raise ValueError('unknown step %r' % op)

    def run(self, text):
        lines = [s for line in text.splitlines() if not line.strip().startswith('#')
                 for s in line.split(';')]
        for n, line in enumerate(lines, 1):
            if line.strip() and not line.strip().startswith('#'):
                say('%3d  %s' % (n, line.strip()))
                self.step(line)


def open_port(name=None):
    if midisock.is_address(name):
        return midisock.Port(name)
    if sys.platform == 'win32':
        from winmidi import Port
    else:
        from midimido import Port
    return Port(name)


def progress(frac):
    say('\r  %5.1f %%' % (100 * frac), end='')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='digirun.py', description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split('\n', 1)[1])
    ap.add_argument('--port', default=None,
                    help="part of the unit's MIDI port name (default: the Digitakt or Digitone), "
                         "or tcp:HOST:PORT for an emulator's USB MIDI link")
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('shot')
    p.add_argument('out')
    p.add_argument('--scale', type=int, default=4)
    p = sub.add_parser('run')
    p.add_argument('script')
    p.add_argument('--out', default='.')
    p = sub.add_parser('do')
    p.add_argument('steps')
    p.add_argument('--out', default='.')
    sub.add_parser('info')
    p = sub.add_parser('data')
    p.add_argument('op', choices=('ls', 'get'))
    p.add_argument('path')
    p.add_argument('file', nargs='?')
    args = ap.parse_args(argv)
    if args.cmd == 'data' and args.op == 'get' and not args.file:
        ap.error('data get needs an output file')

    port = open_port(args.port)
    s = Session(port, args.out if args.cmd in ('run', 'do') else '.')
    try:
        if args.cmd == 'shot':
            screen = s.shot(args.out, args.scale)
            say('%s: %dx%d, %d lit pixels' % (args.out, screen.w, screen.h, screen.lit()))
        elif args.cmd in ('run', 'do'):
            os.makedirs(args.out, exist_ok=True)
            if args.cmd == 'do':
                text = args.steps
            elif args.script == '-':
                text = sys.stdin.read()
            else:
                with open(args.script) as fh:
                    text = fh.read()
            s.run(text)
            say('done')
        elif args.cmd == 'info':
            pid, name = s.rpc.ping()
            say('%s (product id 0x%02X), OS %s' % (name, pid, s.rpc.version()))
        elif args.op == 'ls':
            for e in s.rpc.data_list(args.path):
                if 'id' in e:
                    say('%-24s id %4d  %8d B  ops 0x%04x%s%s%s' % (
                        e['name'] or '-', e['id'], e['size'], e['ops'], '  dir' if e['dir'] else '',
                        '' if e['valid'] else '  (no valid data)', '  +metadata' if e['metadata'] else ''))
                else:
                    say('%-24s dir, %d entries' % (e['name'], e['entries']))
        else:
            data = s.rpc.data_read(args.path, progress=progress)
            with open(args.file, 'wb') as fh:
                fh.write(data)
            say('\n%s: %d bytes' % (args.file, len(data)))
        return 0
    finally:
        try:
            s.close()
        finally:
            port.close()


if __name__ == '__main__':
    sys.exit(main())
