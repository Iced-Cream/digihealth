# SPDX-License-Identifier: GPL-2.0-or-later
"""Small helpers for the Digitone's data store and SysEx dumps, shared by digirun.py and by a
local write policy (writepolicy.py). The names and calls match analoghealth's unitdata.py, so
one policy file can serve both runners.

Digitone (mk1) dumps, as OS 1.44 answers them (device byte 0x0D): a request is dump id + 0x10,
`F0 00 20 3C 0D 00 <id+0x10> 01 01 <n> 00 00 00 05 F7`.

    0x50  stored pattern n with its kit (20992 B unpacked)   0x58  the working one
    0x51  stored pattern n alone (18432 B)                   0x59  the working one
    0x52  stored kit n (2560 B)                              0x5A  the working one
    0x53  sound n (302 B)                                    0x5B  the working one

A pattern's name is 16 bytes at 18212 of the unpacked body, a kit's at 4. A dump is the 10-byte
header, the 7-bit packed body, a 14-bit checksum (the sum of the packed bytes, & 0x3FFF), two
length bytes and F7.
"""
import re

DATA_RENAME = 0x5E
DEVICE = 0x0D
DUMP_HEADER = bytes([0xF0, 0x00, 0x20, 0x3C, DEVICE, 0x00])
DUMP_HEADERS = (DUMP_HEADER,)
PATTERN, PATTERN_REQUEST = 0x50, 0x60
PATTERN_NAME_AT, KIT_NAME_AT, NAME_LEN = 18212, 4, 16


class DataError(Exception):
    """A data-store path, name or dump the runner cannot use."""


def split_path(path):
    """'/projects/5' -> ('/projects', 5); any '/DIR/N' path to a numbered slot."""
    m = re.fullmatch(r'(/.+)/(\d+)', path)
    if not m:
        raise DataError('expected a path to a numbered slot, /DIR/N, not %r' % path)
    return m.group(1), int(m.group(2))


def target(rpc, path):
    """-> (current name or None if the slot is empty, entry)."""
    parent, ident = split_path(path)
    for e in rpc.data_list(parent):
        if e.get('id') == ident and not e.get('dir'):
            return e['name'], e
    return None, None


def rename(rpc, path, name, timeout=5.0):
    """DataRename (0x5E): path, new name; reply u8 status (1 ok) and text."""
    args = path.encode('cp1252') + b'\0' + name.encode('cp1252') + b'\0'
    a = rpc.call(DATA_RENAME, args, timeout)
    if not a or not a[0]:
        text = a[1:].split(b'\0')[0].decode('cp1252', 'replace') if len(a) > 1 else ''
        raise DataError('rename of %s to %r refused: %s' % (path, name, text))


def dump_request(kind, number=0):
    if not 0 <= number < 128:
        raise ValueError('number 0..127')
    return DUMP_HEADER + bytes([kind, 0x01, 0x01, number, 0x00, 0x00, 0x00, 0x05, 0xF7])


def _body(dump, ids, what):
    from digiusb import unpack7
    if len(dump) < 30 or dump[:6] not in DUMP_HEADERS or dump[6] not in ids:
        raise DataError('not a %s dump' % what)
    return bytearray(unpack7(dump[10:-5]))


def _renamed(dump, body, at, name):
    from digiusb import pack7
    body[at:at + NAME_LEN] = name.encode('ascii').ljust(NAME_LEN, b'\0')[:NAME_LEN]
    data = pack7(bytes(body))
    cs = sum(data) & 0x3FFF
    return dump[:10] + data + bytes([cs >> 7, cs & 0x7F]) + dump[-3:]


def _name(body, at):
    return bytes(body[at:at + NAME_LEN]).split(b'\0')[0].decode('ascii', 'replace').rstrip()


def pattern_name(dump):
    """The name in a pattern dump (stored 0x50/0x51 or working 0x58/0x59)."""
    return _name(_body(dump, (0x50, 0x51, 0x58, 0x59), 'pattern'), PATTERN_NAME_AT)


def renamed_pattern_dump(dump, name):
    """A pattern dump with a new name and its checksum redone."""
    return _renamed(dump, _body(dump, (0x50, 0x51, 0x58, 0x59), 'pattern'), PATTERN_NAME_AT, name)


def kit_name(dump):
    """The name in a kit dump (stored 0x52 or working 0x5A)."""
    return _name(_body(dump, (0x52, 0x5A), 'kit'), KIT_NAME_AT)


def renamed_kit_dump(dump, name):
    return _renamed(dump, _body(dump, (0x52, 0x5A), 'kit'), KIT_NAME_AT, name)


def is_kit_dump(msg):
    """A pattern, kit or sound dump the unit stores in the active project when it receives it."""
    return len(msg) > 7 and msg[:6] in DUMP_HEADERS and 0x50 <= msg[6] <= 0x5F
