# digiremote: remote keys on an otherwise stock Digitone

`standalone/patch.py` patches digihealth's remote keys and encoder turns,
and nothing else, into a stock Digitone mk1 OS 1.44 file. It is for tests
that need keys and turns from a computer, on firmware that is as close to
stock as remote input allows.

It does not use an elekloader core or any other mod. elekloader is used only
to read the stock file and to pack and verify the output.

```bash
python standalone/patch.py --stock Digitone_and_Digitone_Keys_OS1.44.syx --out RK44.syx
```

The unit shows version RK44. Give `--version` for another 4-character name
and `--elekloader DIR` if elekloader is not importable. The build needs the
m68k cross toolchain, as for digihealth: native, inside WSL on Windows, or a
prefix set in `CROSS`. A manifest goes next to the output (`.syx.json`) with
each site, the layout and the hashes. The same stock file always gives the
same output.

## What it is

- **The same USB channel and client.** `tools/digiusb.py` works unchanged:
  HELLO answers `digiremote 1.0` and the uptime, and `keys`, `turn` and `peek`
  work as usual. `stats` answers "unknown command".
- **Remote keys are always on.** There is no SETTINGS row.
- **The desktop Digitone mk1 only.** On a Digitone Keys, the button helper
  refuses keys, as digihealth does.
- **The key and encoder code is digihealth's own** (`remote_keys.c`,
  `core_candidate.c/.s`), unchanged.

## How it differs from digihealth

| | digihealth (on core-dn1) | digiremote |
|---|---|---|
| SETTINGS | SYSTEM INFO row (and the core's own menu) | stock |
| top-bar readout, STATS | yes | no |
| timing in the audio render and the task switch | yes | no |
| once-a-second heap walk | yes | no |
| the core's hooks (UI, MIDI, pages, voices, project data) | yes | no |
| sites | 3 + the core's | 4 |

The four sites are an early start-up call (to copy the code to the mods' RAM
area), the UI's periodic tick (to pace taps, holds and turns), the SysEx
input (for the channel) and the panel's button driver (to merge remote keys
with the panel's). Only the main OS changes. The output is refused unless
every other section, the bootstrap included, is the stock file's byte for
byte.

## What still differs from stock

These come from remote input itself:

- The button driver always runs with interrupts masked.
- Remote encoder counts arrive once per UI tick from the UI task, not from
  the panel, so a remote turn is not the same as a hand turn of the same
  counts.
- SysEx messages to device 0x7D on USB are answered. A stock OS ignores them.
- A few instructions run per UI tick and per incoming SysEx message.
- The main OS is packed by elekloader's packer: the same unpacked image, but
  a different layout in flash.
- Taps and holds are timed by the UI tick (30 a second), not by DTIM0. A
  stock OS need not have DTIM0 running, and this patch leaves it alone.

So results that do not go through the button driver or the encoder input
can be compared with stock directly. For results that do, check them with
the same input on the stock image in the emulator.
