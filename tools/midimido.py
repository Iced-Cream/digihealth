# SPDX-License-Identifier: GPL-2.0-or-later
"""Mido transport shared by diagnostics and keys; adapted from the measurement runner."""
import time


class Port:
    def __init__(self, name=None):
        import mido
        self.mido = mido
        wanted = (name.lower(),) if name else ('digitakt', 'digitone')
        if name and ',' in name:                # IN,OUT: ports named apart (a MIDI interface)
            want_in, want_out = ((x.strip().lower(),) for x in name.split(',', 1))
        else:
            want_in = want_out = wanted

        def choose(names, wanted):
            matches = [n for n in names if any(product in n.lower() for product in wanted)]
            if len(matches) != 1:
                raise OSError('Expected one %s port; found %r. Available: %r'
                              % (name or 'Digitakt/Digitone', matches, names))
            return matches[0]

        self.incoming = mido.open_input(choose(mido.get_input_names(), want_in))
        try:
            self.outgoing = mido.open_output(choose(mido.get_output_names(), want_out))
        except Exception:
            self.incoming.close()
            raise

    def send(self, data):
        self.outgoing.send(self.mido.Message.from_bytes(data))

    def send_short(self, *data):
        """A channel message (note, CC): up to three bytes."""
        self.outgoing.send(self.mido.Message.from_bytes(list(data)))

    def receive(self, seconds=1.0, until=None):
        messages = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            messages.extend(bytes(message.bytes()) for message in self.incoming.iter_pending()
                            if message.type == 'sysex')
            if until and until(messages):
                break
            time.sleep(0.002)
        return messages

    def close(self):
        self.incoming.close()
        self.outgoing.close()
