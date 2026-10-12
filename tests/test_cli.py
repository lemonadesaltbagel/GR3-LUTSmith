"""Checks that need no RICOH file (run in CI before every build):  uv run python tests/test_cli.py
With GR3_FIRMWARE=/path/to/the/official/file it also builds, reads the result back and runs the self-test.
Several official files can be given, separated by os.pathsep.
"""
import io
import os
import struct
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gr3_lutsmith as g      # noqa: E402

IDENTITY = [min(64 * i, 16383) for i in range(255)] + [16383]
NEUTRAL_MATRIX = [[8192, 0, 0], [0, 8192, 0], [0, 0, 8192]]
failures = []


def check(name, cond, detail=''):
    print(f'{"ok  " if cond else "FAIL"}  {name}' + (f'  -- {detail}' if detail and not cond else ''))
    if not cond:
        failures.append(name)


def refuses(name, look, kinds=(g.Refused, ValueError, TypeError, KeyError, struct.error)):
    try:
        g.check_look(look)
        g.all_changes([look])
    except kinds:
        check(name, True)
        return
    except Exception as exc:                                     # noqa: BLE001
        check(name, False, f'raised {type(exc).__name__}: {exc}')
        return
    check(name, False, 'accepted')


def look(slot='vivid', **extra):
    base = dict(slot=slot, matrix_q13=[[8000, 150, 42], [120, 8010, 62], [30, 90, 8072]],
                curves=[IDENTITY, IDENTITY, IDENTITY])
    base.update(extra)
    return base


def main():
    # -- the data the engine carries ---------------------------------------------------------------
    check('the firmware data loads', isinstance(g.SLOT_DATA, dict) and 'slots' in g.SLOT_DATA)
    check('every slot in the order has data', sorted(g.SLOT_ORDER) == sorted(g.SLOT_DATA['slots']))
    check('the default slot exists', g.DEFAULT_SLOT in g.SLOT_DATA['slots'])
    check('two official files are known', len(g.OFFICIAL) == 2,
          f'{len(g.OFFICIAL)} known')
    check('the base tone curves are 256 points',
          all(len(g.DATA['curves'][k]) == 256 for k in ('neutral', 'slot')))
    check('every slot has a name and an icon place',
          all(s in g.SLOT_DATA['icons']['slots'] for s in g.SLOT_ORDER)
          and all(s in g.SLOT_DATA['names']['slots'] for s in g.SLOT_ORDER if s != 'standard'))
    out = io.StringIO()
    with redirect_stdout(out):
        g.list_slots()
    check('"slots" lists every slot', all(s in out.getvalue() for s in g.SLOT_ORDER))

    # -- the extra tone-curve step ----------------------------------------------------------------
    h = g.SLOT_DATA['hook']
    at = 0x53002100
    code = g.hook_code(at)
    check('the extra step is the expected length', len(code) == h['code_len'])
    for k, cond, target in h['relocs']:
        word = struct.unpack_from('<I', code, 4 * k)[0]
        step = word & 0xFFFFFF
        back = at + 4 * k + 8 + ((step - (1 << 24) if step & 0x800000 else step) << 2)
        check(f'the step reaches its place ({k})', back == target and (word >> 28) == cond)
    word = struct.unpack_from('<I', g.hook_word(at), 0)[0]
    step = word & 0xFFFFFF
    check('the firmware word reaches the step',
          h['site_va'] + 8 + ((step - (1 << 24) if step & 0x800000 else step) << 2) == at)
    rows = [('vivid', 0x11112222, [IDENTITY] * 3), ('negative-film', 0x33334444, [IDENTITY] * 3)]
    blob = g.hook_blob(rows, at)
    check('the step and its data are the size that was reserved', len(blob) == g.hook_size(rows))
    keys = [struct.unpack_from('<I', blob, h['code_len'] + h['entry_len'] * k)[0] for k in range(3)]
    check('the table holds one row per slot and then stops', keys == [0x11112222, 0x33334444, 0])
    first = struct.unpack_from('<I', blob, h['code_len'] + 4)[0]
    check('a row points inside the block', at < first < at + len(blob))
    check('curves are not shared between slots',
          struct.unpack_from('<I', blob, h['code_len'] + h['entry_len'] + 4)[0] != first)

    # -- room -------------------------------------------------------------------------------------
    one = g.room([look()])
    check('a colour slot takes the expected room', one['need'] == 64 + 16 + 512, f'{one["need"]} bytes')
    check('the room is the free region', one['free'] == len(bytes.fromhex(g.SLOT_DATA['pool'][0][1])))
    colour = [look(s, curves=[[min(64 * i + k, 16383) for i in range(256)]] * 3)
              for k, s in enumerate(['vivid', 'positive-film', 'standard', 'negative-film'])]
    check('four slots with their own curves fit', g.room(colour)['need'] <= one['free'],
          f'{g.room(colour)["need"]} bytes')
    big = dict(look('positive-film'),
               hue_sectors=dict(matrices_q10=[[[1016, 4, 4], [4, 1016, 4], [4, 4, 1016]]] * 12),
               post_matrix_q9=[[504, 4, 4], [4, 504, 4], [4, 4, 504]])
    check('one slot with every lever fits', g.room([big])['need'] <= one['free'],
          f'{g.room([big])["need"]} bytes')

    # -- what the engine refuses ------------------------------------------------------------------
    refuses('a matrix whose rows do not sum right', dict(look(), matrix_q13=[[8000, 0, 0]] * 3))
    refuses('a matrix that is not 3x3', dict(look(), matrix_q13=[[8192, 0], [0, 8192]]))
    refuses('a curve value out of range', dict(look(), curves=[[70000] * 256] * 3))
    refuses('a curve of the wrong length', dict(look(), curves=[[0] * 255] * 3))
    refuses('four channel curves', dict(look(), curves=[IDENTITY] * 4))
    refuses('an unknown slot', dict(look('no-such-slot')))
    for other in g.NOT_OFFERED:                # the camera would not show the look as fitted there
        refuses(f'a look on {other}', dict(look(other)))
    refuses('hue sectors of the wrong shape', dict(look(), hue_sectors=dict(matrices_q10=[[[1024, 0, 0]] * 3] * 11)))
    refuses('hue sector rows that do not sum right',
            dict(look(), hue_sectors=dict(matrices_q10=[[[1000, 0, 0]] * 3] * 12)))
    refuses('colour after the curves of the wrong scale', dict(look(), post_matrix_q9=[[256, 0, 0]] * 3))
    try:
        g.all_changes([look('vivid'), look('vivid')])
        check('two looks for one slot', False, 'accepted')
    except g.Refused:
        check('two looks for one slot', True)
    try:
        g.all_changes([dict(look(s), hue_sectors=dict(matrices_q10=[[[1016, 4, 4], [4, 1016, 4], [4, 4, 1016]]] * 12))
                       for s in ('vivid', 'positive-film', 'negative-film')])
        check('more than the room holds', False, 'accepted')
    except g.Refused as exc:
        check('more than the room holds', 'room' in str(exc).lower() or 'bytes' in str(exc))

    # -- the change set itself --------------------------------------------------------------------
    for slots in (['vivid'], [g.DEFAULT_SLOT], ['negative-film'], ['standard'], ['positive-film'],
                  ['vivid', 'positive-film', g.DEFAULT_SLOT, 'negative-film']):
        items = g.all_changes([look(s) for s in slots])
        label = '+'.join(slots)
        sizes = [(o, a['bytes'] if isinstance(a, dict) else len(bytes.fromhex(a)), len(bytes.fromhex(b)))
                 for o, a, b, _w in items]
        check(f'{label}: every change replaces as many bytes as it reads',
              all(x == y for _o, x, y in sizes))
        spans = sorted((o, o + x) for o, x, _y in sizes)
        check(f'{label}: no two changes overlap',
              all(b0 >= a1 for (_a0, a1), (b0, _b1) in zip(spans, spans[1:])))
        check(f'{label}: something carries the look', len(items) >= len(slots))

    # -- the self-test and a real build, when an official file is given ---------------------------
    paths = [p for p in os.environ.get('GR3_FIRMWARE', '').split(os.pathsep) if p]
    if not paths:
        print('(set GR3_FIRMWARE to also build from the official file)')
    for p in paths:
        source = Path(p).read_bytes()
        rec = g.official_of(source)
        check(f'{Path(p).name} is a known official file', bool(rec))
        if not rec:
            continue
        built = g.make_firmware(source, g.load_look(ROOT / g.SELFTEST_LOOK), log=lambda *a: None)
        check(f'{rec["camera"]}: the self-test image is reproduced',
              g.sha256(built) == g.SELFTEST_SHA256[rec['camera']], g.sha256(built))
        for slot in g.SLOT_ORDER:
            made = g.make_firmware(source, [look(slot, name=None)], log=lambda *a: None)
            r = g.inspect_firmware(made)
            got = [lk['slot'] for lk in r['looks']]
            check(f'{rec["camera"]}: {slot} builds and reads back',
                  r['kind'] == 'lutsmith' and got == [slot] and r['looks'][0]['curves'], f'{r["kind"]} {got}')
        with tempfile.TemporaryDirectory() as d:
            target = g.write_atomic(built, Path(d) / 'out', rec['file'])
            check(f'{rec["camera"]}: the file is written under the official name', target.name == rec['file'])
    print(f'\n{len(failures)} failed' if failures else '\nall checks passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
