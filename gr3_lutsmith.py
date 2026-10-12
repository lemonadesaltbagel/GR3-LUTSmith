#!/usr/bin/env python3
"""GR3-LUTSmith: put the look of a .cube LUT into your RICOH GR III or GR IIIx.

Fits the LUT to the camera's own look processing (a colour matrix, tone
curves, per-hue colour sectors and, with --after-curves, a colour mix after
the curves) and writes a firmware file in which one Image Control look of
your choosing carries the result; every other look stays exactly as it was.
With --turns, several fits from different starting points are run and the
good ones kept. Starts from the official RICOH firmware file you downloaded
yourself (GR III 2.10 or GR IIIx 1.60; the same tool serves both, and the HDF
models and editions). A file is written only if every check passes.

Status of this version: checked offline, no severe issue known; not yet
tested on a real camera. Going back is always possible: install the official
RICOH file again.

Usage
  python3 gr3_lutsmith.py make     LUT.cube OFFICIAL_FIRMWARE [--space srgb|adobe] [--slot SLOT] [--photo DNG] [--no-hue] [--after-curves] [--turns N] [--seed S] [-o DIR]
  python3 gr3_lutsmith.py fit      LUT.cube [--space srgb|adobe] [--slot SLOT] [--photo DNG] [--no-hue] [--after-curves] [--turns N] [--seed S] [-o DIR]   (no firmware needed)
  python3 gr3_lutsmith.py build    DIR/look.json [DIR2/look.json ...] OFFICIAL_FIRMWARE [-o DIR]
                                   (one look per slot; see "slots")
  python3 gr3_lutsmith.py preview  DIR/look.json PHOTO.DNG [-o sheet.jpg]   (needs rawpy, pillow)
  python3 gr3_lutsmith.py slots                                   (the looks yours can replace)
  python3 gr3_lutsmith.py check    ANY_FIRMWARE
  python3 gr3_lutsmith.py selftest OFFICIAL_FIRMWARE

Python 3.8 or newer with numpy. --photo and preview also need rawpy and pillow.
"""
import argparse
import base64
import binascii
import hashlib
import json
import math
import struct
import sys
import zlib
from pathlib import Path

VERSION = '1.0.0'
# Printed after every firmware this version writes.
STATUS = ('Status of this version: checked offline, no severe issue known; not yet tested on a real camera.\n'
          'If anything looks wrong on the camera, install the official RICOH file again to go back.')

# The two official files this works on. They are one program: the camera they serve differs in a handful of
# identity bytes, and every place a look goes is the same in both, so one set of changes serves both cameras
# (and their HDF versions and editions).
OFFICIAL = {
    'd6a3c9c288d784080e7b283222ecdd8752adfeed6faa36dab44f73115354bfd4':
        dict(camera='GR III', version='2.10', file='fwdc239b.bin', bytes=30391780),
    '708e79f3b7b96a7e1581b4acf4c412233050a1832718ef7f06c8397db1b431c7':
        dict(camera='GR IIIx', version='1.60', file='fwdc243b.bin', bytes=30391788),
}


def official_of(data):
    """The record for an official file, or None."""
    r = OFFICIAL.get(sha256(data))
    return r if r and len(data) == r['bytes'] else None


OTHER_KNOWN = {
    'ded5b96faa54d0eeff902fe924f5e66e5123443efab4944224d1ca215973fe9a': 'the official RICOH GR III 2.00 file (an older version)',
    'bf7dbd2b99d37b08fcec0752be39a38921aa6fc7d6c4bb85b6478c200181c782': 'the official RICOH GR III 1.70 file (an older version)',
    'd2547ef4c5500a5637cdf5d9fd6f6a1c328d987b283d4832e73853e5f0cb5e21': 'the official RICOH GR III 1.61 file (an older version)',
    '7f843aaf5d5e48651a229f724cc47383dc14a26910eb1e71645500e27945780e': 'the official RICOH GR IIIx 1.20 file (another camera)',
    '4b29d9f6bbeb3397582d9631661d8af3e5f02d4d85fd6d2ecabca1dfbc9adb9a': 'the official RICOH GR IIIx 1.11 file (another camera)',
    'a2f664dfca034059eb0fd6e18ab08684c326b4a034d85c164dad7e1ec9b5655f': 'the official RICOH GR IV 1.11 file (another camera)',
}

# Built-in example: examples/film-1d.look.json must build to exactly this image (selftest).
SELFTEST_LOOK = 'examples/film-1d.look.json'
SELFTEST_SHA256 = {          # camera -> the image examples/film-1d.look.json must build to
    'GR III': 'e627de366d85d6eb998054b145dc3ad18d0176aa2dda296d31385cd5dcf6892e',
    'GR IIIx': '1da3b1a3acfc17bde7989247af965e7528780efd8998bdf90f86b18848f25717',
}

HEADER, MAGIC, FOOTER_MAGIC = 128, b'RICOH\0\0\0', b'\xa5\x5a\x5a\xa5'
MAX_DISTANCE = 0x2000
LITERAL_CHUNK = 0x6000


class Refused(Exception):
    pass


class Stopped(Exception):
    """A fit stopped on request (STOP)."""


STOP = None             # a callable that returns True to stop a running fit (used by the app; the CLI leaves it None)


def check_stop():
    if STOP is not None and STOP():
        raise Stopped('stopped')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def word_sum(data):
    if len(data) % 4:
        raise Refused('data is not 4-byte aligned')
    return sum(w for (w,) in struct.iter_unpack('<I', data)) & 0xffffffff


def decode(data, limit=256 * 1024 * 1024):
    if len(data) < 160 or data[:8] != MAGIC:
        raise Refused('not a RICOH firmware container')
    out = bytearray()
    frames = []
    cursor = HEADER
    while True:
        start = cursor
        if cursor + 2 > len(data):
            raise Refused('container: missing frame terminator')
        descriptor = int.from_bytes(data[cursor:cursor + 2], 'big')
        cursor += 2
        if descriptor == 0:
            return bytes(out), frames, cursor
        size = descriptor & 0x7fff
        end = cursor + size
        if not size or end > len(data):
            raise Refused(f'container: invalid frame at {start:#x}')
        output_start = len(out)
        if descriptor & 0x8000:
            out += data[cursor:end]
            cursor = end
        else:
            while cursor < end:
                if cursor + 2 > end:
                    raise Refused(f'container: truncated flags at {cursor:#x}')
                flags = int.from_bytes(data[cursor:cursor + 2], 'big')
                cursor += 2
                for bit in range(15, -1, -1):
                    if cursor == end:
                        break
                    if not flags & (1 << bit):
                        out.append(data[cursor])
                        cursor += 1
                        continue
                    if cursor + 2 > end:
                        raise Refused('container: truncated reference')
                    a, b = data[cursor], data[cursor + 1]
                    cursor += 2
                    distance = ((a >> 3) << 8) | b
                    length = a & 7
                    if length == 7:
                        while True:
                            if cursor >= end:
                                raise Refused('container: truncated length')
                            extension = data[cursor]
                            cursor += 1
                            length += extension
                            if extension != 255:
                                break
                    if distance == 0:
                        break
                    if distance > len(out):
                        raise Refused('container: reference before start of output')
                    count = length + 3
                    if distance >= count:
                        out += out[-distance:len(out) - distance + count]
                    else:
                        pattern = bytes(out[-distance:])
                        out += (pattern * ((count + distance - 1) // distance))[:count]
                if len(out) > limit:
                    raise Refused('container: decoded size limit exceeded')
        frames.append((start, end, output_start, len(out)))


def check_container(data, payload=None, consumed=None):
    if payload is None:
        payload, _, consumed = decode(data)
    if data[-20:-16] != FOOTER_MAGIC:
        raise Refused('container: footer magic missing')
    enc, dec = struct.unpack_from('<II', data, len(data) - 12)
    if enc != consumed - HEADER or dec != len(payload):
        raise Refused('container: size fields do not match the content')
    if word_sum(data) or word_sum(payload):
        raise Refused('container: checksum mismatch')
    return payload


def sections(payload):
    out, pos = [], 0
    while pos + 16 <= len(payload):
        tag = payload[pos:pos + 8].rstrip(b'\0')
        if not tag or not tag.isalnum() or not tag.isupper():
            break
        size = struct.unpack_from('<I', payload, pos + 12)[0]
        if pos + 16 + size > len(payload):
            break
        out.append((tag.decode(), pos + 16, pos + 16 + size))
        pos += 16 + size
    return out


def differences(a, b, block=1 << 16):
    if len(a) != len(b):
        raise Refused('length mismatch')
    out = []
    for pos in range(0, len(a), block):
        x, y = a[pos:pos + block], b[pos:pos + block]
        if x != y:
            out += [pos + i for i in range(len(x)) if x[i] != y[i]]
    return out


def rezero(payload):
    buf = bytearray(payload)
    last = struct.unpack_from('<I', buf, len(buf) - 4)[0]
    struct.pack_into('<I', buf, len(buf) - 4, (last - word_sum(buf)) & 0xffffffff)
    return bytes(buf)


def literal_frames(data):
    out = bytearray()
    for pos in range(0, len(data), LITERAL_CHUNK):
        chunk = data[pos:pos + LITERAL_CHUNK]
        out += struct.pack('>H', 0x8000 | len(chunk)) + chunk
    return bytes(out)


def rebuild(source, frames, consumed, patched, changed):
    body = bytearray()
    reemitted = 0
    for (c0, c1, d0, d1) in frames:
        if any(d0 <= c < d1 or c < d0 < c + MAX_DISTANCE for c in changed):
            body += literal_frames(patched[d0:d1])
            reemitted += 1
        else:
            body += source[c0:c1]
    body += b'\0\0'
    trailer = bytearray(source[consumed:])
    struct.pack_into('<II', trailer, len(trailer) - 12, len(body), len(patched))
    struct.pack_into('<I', trailer, len(trailer) - 4, 0)
    padding = bytes((-HEADER - len(body) - len(trailer)) % 4)
    out = bytearray(source[:HEADER] + body + padding + trailer)
    struct.pack_into('<I', out, len(out) - 4, (-word_sum(out)) & 0xffffffff)
    return bytes(out), reemitted


# ------------------------------------------------------------------ firmware data
# Where a personal look's colours go on each Image Control slot, the room the look data is placed in, the menu
# name and icon of every slot, and the camera's own base tone curve and colour model. All of it was read out of
# the official RICOH files and checked by emulating the camera's own look processing.
_GR3 = '''
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
'''
GR3 = json.loads(zlib.decompress(base64.b64decode(''.join(_GR3.split()))))
# Slots the data covers but this version does not offer, because the camera would not show the look as fitted:
# the monochrome looks stay black and white whatever the look's colours, Retro keeps its own tone effect and colour
# cast on top of the look, and Bleach Bypass adds its Toning colour cast (it opens with Toning on).
NOT_OFFERED = {'monotone': 'the camera shows that look in black and white, whatever your colours',
               'soft-monotone': 'the camera shows that look in black and white, whatever your colours',
               'hard-monotone': 'the camera shows that look in black and white, whatever your colours',
               'retro': 'the camera keeps Retro\'s own tone effect and colour cast on top of your look',
               'bleach-bypass': 'the camera adds its Toning colour cast on top of your look'}
for _k in NOT_OFFERED:
    GR3['slots'].pop(_k)
    GR3['order'].remove(_k)
SLOT_DATA = GR3
DATA = {'curves': GR3['curves'], 'camera_matrix': GR3['camera_matrix']}
POST = {'hue': GR3['post_hue']}
SLOT_NAMES = {k: v['shown'] for k, v in GR3['slots'].items()}
SLOT_ORDER = list(GR3['order'])      # looks are placed in this order, so a set of looks always builds the same file
# what each slot replaces, whether it works in movie mode, and whether its tone curves need the extra step
SLOT_INFO = {k: (v['replaces'], v['movie'], 'cross_curves' not in v) for k, v in GR3['slots'].items()}
DEFAULT_SLOT = 'cross-processing'    # the one slot whose own tone curves the camera already has: a data-only build


def slot_line(name):
    return f'{SLOT_NAMES[name]} (replaces {SLOT_INFO[name][0]})'


def list_slots(log=print):
    log(f'{"--slot":18s} {"shown as":27s} {"replaces":52s} {"movie":5s}  {"--name up to":13s} '
        f'{"--icon":6s} tone curves')
    for name in SLOT_ORDER:
        what, movie, extra = SLOT_INFO[name]
        shown = SLOT_NAMES[name] + (' (default)' if name == DEFAULT_SLOT else '')
        ns = SLOT_DATA['names']['slots'].get(name)
        most = f'{max(r[5] for r in ns["languages"])} characters' if ns else '- (fixed)'
        own_icon = 'yes' if name in SLOT_DATA['icons']['slots'] else 'no'
        log(f'{name:18s} {shown:27s} {what:52s} {"yes" if movie else "no":5s}  {most:13s} {own_icon:6s} '
            + ('extra step' if extra else 'built in'))
    log('Your look opens with every slider at 0 in the slot you choose; every slot you do not use keeps its look.')
    log('"extra step": the build adds a small processing step that gives the slot its own tone curves. On the')
    log('default slot the camera already has them, so a look there changes settings data only.')


def matrix_hex(look):
    mat = look['matrix_q13']
    if any(abs(sum(r) - 8192) > 2 for r in mat) or max(abs(x) for r in mat for x in r) > 0x7fff:
        raise Refused('look matrix must be 3x3, Q13, rows summing to 8192')
    return struct.pack('<9h', *[x for r in mat for x in r]).hex()


def distinct_curves(curves):
    if len(curves) != 3 or any(len(c) != 256 or min(c) < 0 or max(c) > 16383 for c in curves):
        raise Refused('curves must be three lists of 256 values in 0..16383')
    distinct = []
    for c in curves:
        if list(c) not in distinct:
            distinct.append(list(c))
    if len(distinct) > 3:
        raise Refused('at most three different channel curves are supported')
    return distinct


def hue_table_hex(q, old):
    """The look's hue-sector matrices in the size of the place that takes them."""
    import numpy as np
    q = np.array(q, int)
    if q.shape != (12, 3, 3) or np.abs(q).max() > 2047 or (q.sum(2) != 1024).any():
        raise Refused('hue sectors must be 12 matrices of 3x3 (Q10, |value| <= 2047, rows summing to 1024)')
    copies = len(old) // 2 // (12 * 9 * 4)
    table = np.repeat(q[:, None], copies, axis=1).astype('<i4').tobytes()
    if len(table) * 2 != len(old):
        raise Refused('internal: hue-sector data size')
    return table.hex()


def post_matrix_rows(target):
    """The look's values for a target matrix, exact after the camera's own rounding."""
    import itertools
    import numpy as np
    Y = np.array(HUE_YCC, np.int64)
    Yi = np.linalg.inv(Y.astype(float))
    rows = []
    for t in target:
        r0 = np.rint(8192 * np.asarray(t, float) @ Yi).astype(np.int64)
        for d in itertools.product(range(-2, 3), repeat=3):
            r = r0 + np.array(d)
            if (((r @ Y + 4096) >> 13) == t).all():
                rows.append(r)
                break
        else:
            raise Refused('internal: colour matrix after the curves cannot be represented')
    return np.array(rows)


def post_check(p):
    import numpy as np
    p = np.array(p, int)
    if p.shape != (3, 3) or (p.sum(1) != 512).any():
        raise Refused('colour matrix after the curves must be 3x3 with rows summing to 512')
    t = post_targets(p)
    if t.min() < -256 or t.max() > 255:
        raise Refused('colour matrix after the curves is out of the camera\'s range')
    return p


def post_data(p):
    """The look's nine camera matrices for colour after the curves (one per Hue step)."""
    return b''.join(struct.pack('<9i', *post_matrix_rows(t).ravel().tolist()) for t in post_targets(p))


class Space:
    """Room for the looks' data, filled in order; changes() gives the edits."""

    def __init__(self, segments):
        self.segs = [dict(off=o, old=bytes.fromhex(h), at=va, data=bytearray.fromhex(h), used=0)
                     for o, h, va in segments]

    def free(self):
        return sum(len(g['old']) - g['used'] for g in self.segs)

    def put(self, data, what='data'):
        n = (len(data) + 3) // 4 * 4
        for g in self.segs:
            if g['used'] + n <= len(g['old']):
                at = g['at'] + g['used']
                g['data'][g['used']:g['used'] + len(data)] = data
                g['used'] += n
                return at
        raise Refused(f'not enough room for these looks together ({what}: {len(data)} bytes, '
                      f'{self.free()} left); use fewer slots, or looks without hue sectors or colour after the curves')

    def put_at(self, at, data):
        g = next(g for g in self.segs if g['at'] <= at < g['at'] + g['used'])
        g['data'][at - g['at']:at - g['at'] + len(data)] = data

    def changes(self):
        return [(g['off'], g['old'][:g['used']].hex(), bytes(g['data'][:g['used']]).hex(), 'data for the looks')
                for g in self.segs if g['used']]


def hook_code(at):
    """The extra processing step, built for the place in the room it is put."""
    h = SLOT_DATA['hook']
    code = bytearray.fromhex(h['code_hex'])
    for k, cond, target in h['relocs']:
        struct.pack_into('<I', code, 4 * k,
                         (cond << 28) | (0xa << 24) | (((target - (at + 4 * k + 8)) >> 2) & 0xffffff))
    return bytes(code)


def hook_word(at):
    """The one firmware word that reaches the extra step."""
    h = SLOT_DATA['hook']
    return struct.pack('<I', 0xea000000 | (((at - (h['site_va'] + 8)) >> 2) & 0xffffff))


def hook_rows(name, key, look):
    """The table rows the extra step needs for a look on this slot (one per tuning row of the slot)."""
    s = SLOT_DATA['slots'][name]
    if not look.get('curves') or 'cross_curves' in s:
        return []
    return [(name, key[r], list(look['curves'])) for r in s['rows']]


def hook_size(rows):
    """Bytes the extra step and its tone curves take together."""
    h, z, seen = SLOT_DATA['hook'], SLOT_DATA['templates']['sizes'], set()
    for slot, _key, curves in rows:
        seen |= {(slot, tuple(int(v) for v in c)) for c in curves}
    return h['code_len'] + 4 + h['entry_len'] * len(rows) + z['curve'] * len(seen)


def hook_blob(rows, at):
    """The extra step, its per-slot table and the slots' tone curves as one block placed at `at`. Channel curves
    that are equal within one slot share one copy; curves are never shared between slots."""
    h, body, where = SLOT_DATA['hook'], bytearray(), {}

    def put(slot, curve):
        k = (slot, tuple(int(v) for v in curve))
        if k not in where:
            where[k] = len(body)
            body.extend(struct.pack('<256H', *k[1]))
        return where[k]
    placed = [(key, [put(slot, c) for c in curves]) for slot, key, curves in rows]
    curves_at = at + h['code_len'] + h['entry_len'] * len(rows) + 4
    table = b''.join(struct.pack('<4I', k, *[curves_at + o for o in offs]) for k, offs in placed)
    return hook_code(at) + table + b'\0\0\0\0' + bytes(body)


def hue_entry_at(q, space):
    """The look's hue sectors as the camera reads them; returns where the entry sits."""
    t = SLOT_DATA['templates']['hue']
    at = {}
    for k in t['order']:
        at[k] = space.put(bytes.fromhex(hue_table_hex(q, '00' * t['mtab_len']) if k == 'mtab'
                                        else t['blobs'][k]), 'hue sectors')
    sub3 = space.put(struct.pack('<10I', *([at['sub3tab']] * 8 + [int(t['sub3_identity'], 16)] * 2)), 'hue sectors')
    sub2 = space.put(struct.pack('<8I', *([at['mtab']] * 8)), 'hue sectors')
    return space.put(struct.pack('<6I', at['sub0'], at['sub1'], sub2, sub3, at['sub4'], int(t['sub5'], 16)),
                     'hue sectors')


def post_entry_at(p, space):
    """The look's colour after the tone curves; returns where the entry sits."""
    t = SLOT_DATA['templates']['after']
    data = post_data(post_check(p))
    at = space.put(bytes(t['head'] + len(data)), 'colour after the curves')
    space.put_at(at, struct.pack(f'<{len(t["links"])}I', *[at + d for d in t['links']]) + data)
    return at


def slot_space(look, name):
    """Bytes of room a look in this slot needs, apart from the extra step (which is counted once)."""
    s, z = SLOT_DATA['slots'][name], SLOT_DATA['templates']['sizes']
    n = 0
    for r in s['rows']:
        m = s['matrix'][str(r)]
        if not m['in_place']:
            n += z['matrix'] + (0 if m['pointer'] else z['descriptor'])
    if (look.get('hue_sectors') or {}).get('matrices_q10'):
        n += z['hue']
    if look.get('post_matrix_q9'):
        n += z['after']
    return n


def slot_changes(look, name, space):
    """(offset, old, new, what) for a personal look on one slot, and the rows the extra step needs:
    neutral slider defaults, the look's colour matrix, its tone curves, hue sectors and colour after the curves.
    A slot with several tuning settings (Bleach Bypass has three) gets the look written on every one of them."""
    s, label = SLOT_DATA['slots'][name], SLOT_NAMES[name]
    out = [(o, a, b, f'{label}: neutral slider defaults') for o, a, b in s['defaults']]
    if s['offset_word']:
        off, old = s['offset_word']
        out.append((off, old, '00000000', f'{label}: no hidden level of its own'))
    mat = matrix_hex(look)
    key = {}
    for r in s['rows']:
        d, m = s['descriptor'][str(r)], s['matrix'][str(r)]
        key[r] = int(d['va'], 16)
        if m['in_place']:
            out.append((m['in_place'][0], m['in_place'][1], mat, f'{label}: look colour matrix'))
        elif m['pointer']:                           # the stock matrix is shared: give this slot its own copy
            at = space.put(bytes.fromhex(mat) + b'\0\0', 'colour matrix')
            out.append((m['pointer'][0], m['pointer'][1], struct.pack('<I', at).hex(),
                        f'{label}: look colour matrix of its own'))
        else:                                        # the stock colour processing is shared: own copy of that too
            at = space.put(bytes.fromhex(mat) + b'\0\0', 'colour matrix')
            copy = bytearray.fromhex(d['bytes'])
            struct.pack_into('<I', copy, 4, at)
            key[r] = space.put(copy, 'colour processing')
            out.append((d['table_a'][0], d['table_a'][1], struct.pack('<I', key[r]).hex(),
                        f'{label}: look colour processing of its own'))
    curves = look.get('curves')
    if curves and 'cross_curves' in s:               # this slot has the camera's own per-look tone curves
        for (off, n, digest), c in zip(s['cross_curves'], list(curves) * 3):
            out.append((off, dict(sha256=digest, bytes=n), struct.pack('<256H', *[int(v) for v in c]).hex(),
                        f'{label}: tone curve'))
    elif curves:
        distinct_curves(curves)
    q = (look.get('hue_sectors') or {}).get('matrices_q10')
    if q:
        at = hue_entry_at(q, space)
        for r in s['rows']:
            off, old = s['hue_row'][str(r)]
            out.append((off, old, struct.pack('<I', at).hex(), f'{label}: hue sectors'))
    p = look.get('post_matrix_q9')
    if p:
        at = post_entry_at(p, space)
        for r in s['rows']:
            off, old = s['post_row'][str(r)]
            out.append((off, old, struct.pack('<I', at).hex(), f'{label}: colour after the curves'))
    return out, hook_rows(name, key, look)


# The camera's menu languages and the characters each one shows. ASCII shows in all of them; other characters show
# when the camera is set to a language that uses them.
CAMERA_LANGUAGES = ('Czech', 'Danish', 'Dutch', 'English', 'Finnish', 'French', 'German', 'Greek', 'Hungarian',
                    'Italian', 'Japanese', 'Korean', 'Polish', 'Portuguese', 'Russian', 'Chinese-S', 'Chinese-T',
                    'Spanish', 'Swedish', 'Thai', 'Turkish')
LATIN_LANGUAGES = ('Czech', 'Danish', 'Dutch', 'English', 'Finnish', 'French', 'German', 'Hungarian', 'Italian',
                   'Polish', 'Portuguese', 'Spanish', 'Swedish', 'Turkish')
CJK = ('Japanese', 'Chinese-S', 'Chinese-T')
SCRIPTS = (((0x00a0, 0x024f), LATIN_LANGUAGES, 'accented Latin'), ((0x0370, 0x03ff), ('Greek',), 'Greek'),
           ((0x0400, 0x04ff), ('Russian',), 'Cyrillic'), ((0x0e00, 0x0e7f), ('Thai',), 'Thai'),
           ((0x3040, 0x30ff), ('Japanese',), 'Japanese kana'), ((0x31f0, 0x31ff), ('Japanese',), 'Japanese kana'),
           ((0xff66, 0xff9f), ('Japanese',), 'Japanese kana'), ((0x3400, 0x4dbf), CJK, 'Chinese characters'),
           ((0x4e00, 0x9fff), CJK, 'Chinese characters'), ((0x1100, 0x11ff), ('Korean',), 'Korean'),
           ((0x3130, 0x318f), ('Korean',), 'Korean'), ((0xac00, 0xd7a3), ('Korean',), 'Korean'),
           ((0x3000, 0x303f), CJK + ('Korean',), 'Asian punctuation'),
           ((0xff00, 0xff65), CJK + ('Korean',), 'full-width characters'))
LOCALES = {'cs': 'Czech', 'da': 'Danish', 'nl': 'Dutch', 'en': 'English', 'fi': 'Finnish', 'fr': 'French',
           'de': 'German', 'el': 'Greek', 'hu': 'Hungarian', 'it': 'Italian', 'ja': 'Japanese', 'ko': 'Korean',
           'pl': 'Polish', 'pt': 'Portuguese', 'ru': 'Russian', 'es': 'Spanish', 'sv': 'Swedish', 'th': 'Thai',
           'tr': 'Turkish'}


def system_language():
    """The camera language to assume: the computer's language when the camera has it, else English."""
    import locale
    import os
    code = (os.environ.get('LC_ALL') or os.environ.get('LANG') or (locale.getlocale()[0] or '')).replace('-', '_')
    low = code.lower()
    if low.startswith('zh'):
        return 'Chinese-T' if any(t in low for t in ('tw', 'hk', 'mo', 'hant')) else 'Chinese-S'
    return LOCALES.get(low[:2], 'English')


def scan_name(name):
    """({script label: camera languages that show it} for every non-ASCII character, characters no camera language
    shows)."""
    found, bad = {}, []
    for c in name:
        if ' ' <= c <= '~':
            continue
        hit = next((sc for sc in SCRIPTS if sc[0][0] <= ord(c) <= sc[0][1]), None)
        if hit is None:
            bad.append(c)
        else:
            found.setdefault(hit[2], set()).update(hit[1])
    return found, ''.join(sorted(set(bad)))


def name_scripts(name):
    """{script label: camera languages that show it} for every non-ASCII character; refuses characters no camera
    language shows."""
    found, bad = scan_name(name)
    if bad:
        raise Refused(f'the camera cannot show {bad!r} in any of its languages; use letters '
                      'and characters of one of the camera\'s languages')
    return found


def name_slot_limit(slot):
    """The longest name the slot takes, or None when it cannot be renamed."""
    s = SLOT_DATA['names']['slots'].get(slot)
    return max(r[5] for r in s['languages']) if s else None


def name_report(name, slot, camera_language='English'):
    """The name check as data (the app words it in the user's language): problem (a refusal: 'fixed', 'space',
    'characters', 'length') with its detail, the scripts used and the camera languages showing all of them, whether
    it needs the user's confirmation (question) and whether it may be cut off (long)."""
    most = name_slot_limit(slot)
    out = dict(problem=None, detail=None, most=most, scripts=[], languages=[], question=False, long=False)
    if most is None:
        return dict(out, problem='fixed')
    if not name or name != name.strip():
        return dict(out, problem='space')
    found, bad = scan_name(name)
    if bad:
        return dict(out, problem='characters', detail=bad)
    if len(name) > most:
        return dict(out, problem='length', detail=len(name))
    width = sum(2 if 0x1100 <= ord(c) and not 0xff61 <= ord(c) <= 0xffdc else 1 for c in name)
    both = set(CAMERA_LANGUAGES)
    for langs in found.values():
        both &= langs
    return dict(out, scripts=list(found), languages=[l for l in CAMERA_LANGUAGES if l in both] if found else [],
                question=bool(found) and camera_language not in both, long=width > SLOT_DATA['names']['display_hint'])


def check_name(name, slot, camera_language='English'):
    """(notes, question) for naming a slot. A question means the name may not show with this camera language and
    needs the user's confirmation; problems are refused."""
    r = name_report(name, slot, camera_language)
    if r['problem'] == 'fixed':
        raise Refused(f'{SLOT_NAMES[slot]} cannot be renamed (its name is shared with another look)')
    if r['problem'] == 'space':
        raise Refused('the name must not be empty or start/end with a space')
    if r['problem'] == 'characters':
        name_scripts(name)
    if r['problem'] == 'length':
        raise Refused(f'{SLOT_NAMES[slot]} takes names of up to {r["most"]} characters ({name!r} has {len(name)})')
    notes, question = [], None
    if r['long']:
        notes.append(f'long names may be cut off on some screens')
    if r['scripts']:
        kinds = ', '.join(r['scripts'])
        if r['question']:
            where = ('no single camera language shows all of them' if not r['languages'] else
                     'they show when the camera is set to ' + ', '.join(r['languages']))
            question = (f'This name uses {kinds}. Your camera language is {camera_language}, so they may not show; '
                        f'{where}.')
        else:
            notes.append(f'uses {kinds}: shown with the camera set to {camera_language}; '
                         'if you switch to a language that does not use them, they may not show')
    return notes, question


def settle_name(name, slot, camera_language, accepted, log=print):
    """Show the notes on a name and, when it may not show with this camera language, get the user's answer
    (accepted beforehand, or asked at the terminal). Returns True when the name was accepted after a question."""
    notes, question = check_name(name, slot, camera_language)
    for n in notes:
        log(f'note on the name: {n}')
    if not question:
        return False
    log(question)
    if accepted:
        log('using it anyway, as accepted')
        return True
    if sys.stdin.isatty():
        try:
            ans = input('Use this name anyway? Type y to continue: ').strip().lower()
        except EOFError:
            ans = ''
        if ans in ('y', 'yes'):
            return True
    raise Refused('name not used; choose another name, set --camera-language to the camera\'s language, '
                  'or add --accept-name to use it anyway')


def name_changes(look, slot):
    """(offset, old, new, what) giving the slot the look's name in every language."""
    name = look.get('name')
    if not name:
        return []
    check_name(name, slot)                       # refusals only; the question was answered when the look was made
    nd, label = SLOT_DATA['names'], SLOT_NAMES[slot]
    s = nd['slots'][slot]
    host = next(r for r in s['languages'] if r[0] == s['host'])
    out = []
    for lang, rec_off, rec_old, text_off, text_old, room, text_at in s['languages']:
        here = len(name) <= room
        body = name.encode('utf-16-le')
        if here:
            out.append((text_off, text_old, (body + bytes(len(text_old) // 2 - len(body))).hex(), f'{label}: name ({lang})'))
        at = text_at if here else host[6]
        out.append((rec_off, rec_old, struct.pack('<II', nd['length_base'] + len(name), at).hex(), f'{label}: name ({lang})'))
    return out


ICON_WHAT = 'own icon'


def make_icon(src, size):
    """Any picture -> an RGBA icon of exactly `size` (w, h): the centre of a non-square picture, resized with clean
    transparent edges. Needs Pillow."""
    try:
        from PIL import Image
    except ImportError:
        raise Refused('--icon needs pillow: uv sync --extra photo (or python3 -m pip install pillow)')
    try:
        img = Image.open(src).convert('RGBA')
    except (OSError, ValueError) as exc:
        raise Refused(f'cannot read the icon picture {src}: {exc}')
    w, h = size
    scale = max(w / img.width, h / img.height)        # cover the icon, then keep its centre
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    big = img.convert('RGBa').resize((nw, nh), Image.LANCZOS)   # premultiplied: no dark fringes
    left, top = (nw - w) // 2, (nh - h) // 2
    return big.crop((left, top, left + w, top + h)).convert('RGBA')


def icon_record(src, slot):
    """The look's icon for this slot, as stored in look.json."""
    place = SLOT_DATA['icons']['slots'].get(slot)
    if place is None:
        raise Refused(f'{SLOT_NAMES[slot]} cannot take an own icon (its icon is shared with another look)')
    icon = make_icon(src, (place[1], place[2]))
    return dict(w=place[1], h=place[2], source=Path(src).name, rgba=base64.b64encode(icon.tobytes()).decode())


def icon_image(record):
    from PIL import Image
    return Image.frombytes('RGBA', (record['w'], record['h']), base64.b64decode(record['rgba']))


def icon_changes(look, slot):
    """(offset, fingerprint of the original, new, what) repainting the slot's menu icon."""
    rec = look.get('icon')
    if not rec:
        return []
    place = SLOT_DATA['icons']['slots'].get(slot)
    if place is None:
        raise Refused(f'{SLOT_NAMES[slot]} cannot take an own icon (its icon is shared with another look)')
    off, w, h, digest = place
    data = base64.b64decode(rec['rgba'])
    if (rec['w'], rec['h']) != (w, h) or len(data) != w * h * 4:
        raise Refused(f'the icon must be {w}x{h} for {SLOT_NAMES[slot]}; make the look again with --icon')
    return [(off, dict(sha256=digest, bytes=w * h * 4), data.hex(), f'{SLOT_NAMES[slot]}: {ICON_WHAT}')]


def look_slot(look):
    name = look.get('slot') or DEFAULT_SLOT
    if name in NOT_OFFERED:
        raise Refused(f'{name} is not offered: {NOT_OFFERED[name]}; choose one of: {", ".join(SLOT_NAMES)}')
    if name not in SLOT_NAMES:
        raise Refused(f'unknown slot {name!r}; choose one of: {", ".join(SLOT_NAMES)}')
    return name


def space_room():
    """The room in the firmware that the looks' data is placed in."""
    return Space(SLOT_DATA['pool'])


def room(looks):
    """{'need': bytes these looks take from the room, 'free': bytes available} (looks: look dicts with 'slot')."""
    looks = [looks] if isinstance(looks, dict) else list(looks)
    names = [look_slot(lk) for lk in looks]
    rows = [r for n, lk in zip(names, looks)
            for r in hook_rows(n, {k: 0 for k in SLOT_DATA['slots'][n]['rows']}, lk)]
    need = sum(slot_space(lk, n) for n, lk in zip(names, looks)) + (hook_size(rows) if rows else 0)
    return dict(need=need, free=space_room().free())


def all_changes(looks):
    """Every change for one look, or several looks in different slots."""
    looks = [looks] if isinstance(looks, dict) else list(looks)
    names = [look_slot(lk) for lk in looks]
    if len(set(names)) != len(names):
        raise Refused('two looks for the same slot: ' + ', '.join(sorted({n for n in names if names.count(n) > 1})))
    space = space_room()
    by_name = dict(zip(names, looks))
    need = room(looks)['need']
    if need > space.free():
        raise Refused(f'these looks need {need} bytes of room together, {space.free()} are available; '
                      'use fewer slots, or looks without hue sectors or colour after the curves')
    out, rows = [], []
    for name in SLOT_ORDER:
        lk = by_name.get(name)
        if lk is None:
            continue
        changes, hooked = slot_changes(lk, name, space)
        out += changes
        rows += hooked
    if rows:
        at = space.put(bytes(hook_size(rows)), 'tone curves')
        space.put_at(at, hook_blob(rows, at))
        off, old = SLOT_DATA['hook']['site']
        out.append((off, old, hook_word(at).hex(), "the extra step for the slots' own tone curves"))
    out += space.changes()
    for name in SLOT_ORDER:
        if name in by_name:
            out += name_changes(by_name[name], name) + icon_changes(by_name[name], name)
    return out


def make_firmware(source, looks, log=print):
    digest = sha256(source)
    official = official_of(source)
    if not official:
        if digest in OTHER_KNOWN:
            raise Refused(f'this is {OTHER_KNOWN[digest]}; use one of the official RICOH files')
        raise Refused('input is not one of the official RICOH files this works on ('
                      + ', '.join(f'{r["camera"]} {r["version"]} {r["file"]}' for r in OFFICIAL.values())
                      + f'); its SHA-256 is {digest}')
    log(f'input: official RICOH {official["camera"]} {official["version"]} firmware (SHA-256 verified)')
    log('decoding container ...')
    payload, frames, consumed = decode(source)
    check_container(source, payload, consumed)
    secs = sections(payload)
    rtos = next((s for s in secs if s[0] == 'RTOS'), None)
    if rtos is None:
        raise Refused('payload: RTOS section not found')
    buf = bytearray(payload)
    items = []
    for off, old, new, what in all_changes(looks):
        if isinstance(old, dict):                    # checked by fingerprint: take the original bytes from the file
            have = bytes(buf[off:off + old['bytes']])
            if hashlib.sha256(have).hexdigest() != old['sha256']:
                raise Refused(f'unexpected original bytes at {off:#x} ({what})')
            old = have.hex()
        items.append((off, old, new, what))
    icon_sec = next((s for s in secs if s[0] == SLOT_DATA['icons']['section']), None)
    spans = sorted((o, o + len(bytes.fromhex(a))) for o, a, _, _ in items)
    if any(b0 < a1 for (_, a1), (b0, _) in zip(spans, spans[1:])):
        raise Refused('internal: overlapping changes')
    for off, old, new, what in items:
        old, new = bytes.fromhex(old), bytes.fromhex(new)
        have = bytes(buf[off:off + len(old)])
        if have != old or len(new) != len(old):
            raise Refused(f'unexpected original bytes at {off:#x} ({what})')
        icon = what.endswith(ICON_WHAT) and icon_sec and icon_sec[1] <= off and off + len(new) <= icon_sec[2]
        if not (rtos[1] <= off and off + len(new) <= rtos[2]) and not icon:
            raise Refused(f'change at {off:#x} is outside the RTOS section')
        buf[off:off + len(new)] = new
    log(f'  {len(items)} changes applied')
    patched = rezero(bytes(buf))
    log('rebuilding container ...')
    changed = differences(payload, patched)
    out, reemitted = rebuild(source, frames, consumed, patched, changed)
    log('verifying result ...')
    again, _, used = decode(out)
    if again != patched:
        raise Refused('verification: rebuilt container does not decode to the patched firmware')
    check_container(out, again, used)
    if out[:HEADER] != source[:HEADER]:
        raise Refused('verification: header changed')
    allowed = {i for off, old, _, _ in items for i in range(off, off + len(bytes.fromhex(old)))}
    allowed |= set(range(len(payload) - 4, len(payload)))
    stray = [i for i in changed if i not in allowed]
    if stray:
        raise Refused(f'verification: unexpected changes at {[hex(i) for i in stray[:8]]}')
    if sections(again) != secs:
        raise Refused('verification: section layout changed')
    open_tags = {'RTOS'} | ({icon_sec[0]} if icon_sec and any(w.endswith(ICON_WHAT) for *_, w in items) else set())
    for tag, a, b in secs:
        if tag not in open_tags and again[a:b] != payload[a:b]:
            raise Refused(f'verification: section {tag} changed')
    log(f'verified: {len(changed)} payload bytes changed, {reemitted} frames re-encoded, '
        f'all other sections identical' + (' (icons changed in the icon section only)' if len(open_tags) > 1 else ''))
    return out


def write_atomic(data, out_dir, name):
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    if target.exists():
        raise Refused(f'{target} already exists; choose another output directory or remove it')
    tmp = target.with_name('.' + name + '.tmp')
    tmp.write_bytes(data)
    if sha256(tmp.read_bytes()) != sha256(data):
        tmp.unlink()
        raise Refused('write verification failed')
    tmp.rename(target)
    return target


def inspect_firmware(data):
    """What a firmware file is, as data: sha256, bytes, valid (container) and error, kind ('official', 'known',
    'unknown' or 'lutsmith'), name (for official/known), and for a GR3-LUTSmith file its looks: one dict per slot
    with slot, hue (hue sectors), post (colour after the curves), name (shown name when renamed), icon (own icon) and
    matrix (the look's colour matrix)."""
    digest = sha256(data)
    names = {k: f'official RICOH {r["camera"]} {r["version"]} (unmodified)' for k, r in OFFICIAL.items()}
    names.update(OTHER_KNOWN)
    out = dict(sha256=digest, bytes=len(data), valid=False, error=None, kind=None, name=None, looks=[])
    try:
        payload = check_container(data)
    except Refused as exc:
        return dict(out, error=str(exc))
    out['valid'] = True
    if digest in names:
        return dict(out, kind='official' if digest in OFFICIAL else 'known', name=names[digest])

    def has(changes):
        return all(payload[o:o + len(bytes.fromhex(b))] == bytes.fromhex(b) for o, _, b in changes)

    def moved(place):
        off, old = place[0], place[1]
        return payload[off:off + len(old) // 2].hex() != old
    hook, keys = SLOT_DATA['hook'], set()
    if moved(hook['site']):                          # the extra step is in: read which slots it serves
        step = struct.unpack_from('<I', payload, hook['site'][0])[0] & 0xffffff
        at = hook['site_va'] + 8 + ((step - (1 << 24) if step & 0x800000 else step) << 2)
        poff, pold, pva = SLOT_DATA['pool'][0]
        if 0 <= at - pva < len(pold) // 2:
            base = poff + at - pva
            for k in range(64):
                key = struct.unpack_from('<I', payload, base + hook['code_len'] + hook['entry_len'] * k)[0]
                if not key:
                    break
                keys.add(key)
    looks = []
    for name, sd in SLOT_DATA['slots'].items():
        marks = [has(sd['defaults'])] if sd['defaults'] else []
        for r in sd['rows']:
            m, d = sd['matrix'][str(r)], sd['descriptor'][str(r)]
            marks.append(moved(m['in_place'] or m['pointer'] or d['table_a']))
        if not any(marks):
            continue
        lk = dict(slot=name, hue=any(moved(sd['hue_row'][str(r)]) for r in sd['rows']),
                  post=any(moved(sd['post_row'][str(r)]) for r in sd['rows']))
        if 'cross_curves' in sd:
            lk['curves'] = any(hashlib.sha256(payload[o:o + n]).hexdigest() != dg
                               for o, n, dg in sd['cross_curves'])
        else:
            lk['curves'] = any(struct.unpack_from('<I', payload, sd['descriptor'][str(r)]['table_a'][0])[0] in keys
                               for r in sd['rows'])
        first = sd['matrix'][str(sd['rows'][0])]
        if first['in_place']:
            mm = struct.unpack_from('<9h', payload, first['in_place'][0])
            lk['matrix'] = [list(mm[i:i + 3]) for i in (0, 3, 6)]
        looks.append(lk)
    if not looks:
        return dict(out, kind='unknown')

    def shown_name(slot):
        """The slot's English name as stored, when it was renamed."""
        ns = SLOT_DATA['names']['slots'].get(slot)
        if not ns:
            return None
        rows = {r[6]: r for r in ns['languages']}
        en = next(r for r in ns['languages'] if r[0] == 'English')
        if payload[en[1]:en[1] + 8].hex() == en[2]:
            return None
        length, at = struct.unpack_from('<II', payload, en[1])
        if at not in rows:
            return '?'
        off = rows[at][3]
        return payload[off:off + 2 * (length - SLOT_DATA['names']['length_base'])].decode('utf-16-le', 'replace')

    def own_icon(slot):
        place = SLOT_DATA['icons']['slots'].get(slot)
        if not place:
            return False
        off, w, h, digest = place
        return hashlib.sha256(payload[off:off + w * h * 4]).hexdigest() != digest
    for lk in looks:
        lk['name'], lk['icon'] = shown_name(lk['slot']), own_icon(lk['slot'])
    return dict(out, kind='lutsmith', looks=looks)


def identify(data, log=print):
    r = inspect_firmware(data)
    log(f'SHA-256  {r["sha256"]}')
    log(f'bytes    {r["bytes"]}')
    if not r['valid']:
        log(f'container: INVALID - {r["error"]}')
        return 2
    log('container: valid (size fields, footer, both checksums)')
    if r['kind'] in ('official', 'known'):
        log(f'file     {r["name"]}')
        return 0
    if r['kind'] == 'unknown':
        log('file     unknown - not the official 2.10 file and not made by GR3-LUTSmith')
        return 1

    def yn(x):
        return 'yes' if x else 'no'
    found = []
    for lk in r['looks']:
        f = (f'{SLOT_NAMES[lk["slot"]]} (' + (f'look matrix {lk["matrix"]}; ' if 'matrix' in lk else '')
             + f'tone curves: {yn(lk["curves"])}; hue sectors: {yn(lk["hue"])}; '
             + f'colour after the curves: {yn(lk["post"])})')
        found.append(f + (f' named "{lk["name"]}"' if lk['name'] else '') + (' with its own icon' if lk['icon'] else ''))
    log('file     a GR3-LUTSmith firmware; personal looks in: ' + '; '.join(found))
    return 0


# ------------------------------------------------------------------ LUT files
class Lut:
    """A .cube LUT (3D, or 1D expanded per channel), values as an (n,n,n,3) array [b][g][r]."""

    def __init__(self, path):
        import numpy as np
        text = Path(path).read_text(errors='replace')
        self.sha256 = hashlib.sha256(text.encode()).hexdigest()
        head, rows, self.ignored = {}, [], []
        for line in text.splitlines():
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            tok = line.split()
            if tok[0][0].isalpha():
                key = tok[0].upper()
                if key == 'TITLE':
                    head['TITLE'] = line[5:].strip().strip('"')
                elif key in ('LUT_3D_SIZE', 'LUT_1D_SIZE'):
                    head[key] = int(tok[1])
                elif key in ('DOMAIN_MIN', 'DOMAIN_MAX'):
                    head[key] = tuple(map(float, tok[1:4]))
                elif key in ('LUT_3D_INPUT_RANGE', 'LUT_1D_INPUT_RANGE'):
                    head['DOMAIN_MIN'], head['DOMAIN_MAX'] = (float(tok[1]),) * 3, (float(tok[2]),) * 3
                else:
                    self.ignored.append(tok[0])
                continue
            if len(tok) != 3:
                raise Refused(f'{path}: invalid data row {line[:40]!r}')
            rows.append(tuple(map(float, tok)))
        v = np.array(rows, float)
        if not len(v) or not np.isfinite(v).all():
            raise Refused(f'{path}: no data or non-finite values')
        self.lo = np.array(head.get('DOMAIN_MIN', (0., 0., 0.)))
        self.hi = np.array(head.get('DOMAIN_MAX', (1., 1., 1.)))
        if 'LUT_3D_SIZE' in head and 'LUT_1D_SIZE' in head:
            raise Refused(f'{path}: combined 1D + 3D LUTs are not supported')
        if 'LUT_3D_SIZE' in head:
            n = head['LUT_3D_SIZE']
            if not 2 <= n <= 129 or len(v) != n ** 3:
                raise Refused(f'{path}: LUT_3D_SIZE {n} but {len(v)} rows')
            self.kind, self.n, self.table = '3D', n, v.reshape(n, n, n, 3)
        elif 'LUT_1D_SIZE' in head:
            n = head['LUT_1D_SIZE']
            if not 2 <= n <= 65536 or len(v) != n:
                raise Refused(f'{path}: LUT_1D_SIZE {n} but {len(v)} rows')
            g = np.linspace(0, 1, 33)
            xs = np.linspace(0, 1, n)
            x = grid(33)
            out = np.stack([np.interp(x[:, c], xs, v[:, c]) for c in range(3)], 1)
            self.kind, self.n, self.table = '1D', 33, out.reshape(33, 33, 33, 3)
        else:
            raise Refused(f'{path}: no LUT_3D_SIZE or LUT_1D_SIZE')
        self.title = head.get('TITLE', '')
        if v.min() < -0.05 or v.max() > 1.05:
            raise Refused(f'{path}: output range {v.min():.2f}..{v.max():.2f} is not 0..1; '
                          'log or scene-referred LUTs are not supported')

    def __call__(self, rgb):
        """Trilinear lookup; inputs clamped to the LUT's domain."""
        import numpy as np
        n = self.n
        p = np.clip((rgb - self.lo) / (self.hi - self.lo), 0, 1) * (n - 1)
        a = np.minimum(np.floor(p).astype(int), n - 2)
        f = p - a
        out = 0
        for dr in (0, 1):
            for dg in (0, 1):
                for db in (0, 1):
                    w = ((f[:, 0] if dr else 1 - f[:, 0]) * (f[:, 1] if dg else 1 - f[:, 1])
                         * (f[:, 2] if db else 1 - f[:, 2]))
                    out = out + w[:, None] * self.table[a[:, 2] + db, a[:, 1] + dg, a[:, 0] + dr]
        return np.clip(out, 0, 1)


def grid(n):
    import numpy as np
    g = np.linspace(0, 1, n)
    b, gg, r = np.meshgrid(g, g, g, indexing='ij')
    return np.stack([r.ravel(), gg.ravel(), b.ravel()], 1)


# ------------------------------------------------------------------ colour
SRGB_TO_ADOBE = [[366 / 512, 146 / 512, 0], [0, 1, 0], [0, 21 / 512, 491 / 512]]
ADOBE_GAMMA = 563 / 256
XYZ = [[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750], [0.0193339, 0.1191920, 0.9503041]]
WHITE = [0.95047, 1.0, 1.08883]


def srgb_dec(x):
    import numpy as np
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def srgb_enc(x):
    import numpy as np
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * x ** (1 / 2.4) - 0.055)


def adobe_lut_on_srgb(lut, y):
    """Apply an Adobe RGB LUT to sRGB-encoded colours, result in sRGB (colorimetric, via linear light)."""
    import numpy as np
    m = np.array(SRGB_TO_ADOBE)
    ya = np.clip(srgb_dec(y) @ m.T, 0, 1) ** (1 / ADOBE_GAMMA)
    lin = (lut(ya) ** ADOBE_GAMMA) @ np.linalg.inv(m).T
    keep = (lin >= -1e-3).all(1) & (lin <= 1 + 1e-3).all(1)
    return srgb_enc(lin), keep


def lab(rgb):
    import numpy as np
    xyz = srgb_dec(rgb) @ np.array(XYZ).T / np.array(WHITE)
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def de00(l1, l2):
    """CIEDE2000 (Sharma, Wu & Dalal 2005)."""
    import numpy as np
    L1, a1, b1 = l1.T
    L2, a2, b2 = l2.T
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cb ** 7 / (Cb ** 7 + 25 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0, dh)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lbp, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    hs = h1p + h2p
    hbp = np.where(C1p * C2p == 0, hs, np.where(np.abs(h1p - h2p) <= 180, hs / 2,
                                                np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2)))
    T = (1 - 0.17 * np.cos(np.radians(hbp - 30)) + 0.24 * np.cos(np.radians(2 * hbp))
         + 0.32 * np.cos(np.radians(3 * hbp + 6)) - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    dth = 30 * np.exp(-((hbp - 275) / 25) ** 2)
    Rc = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25 ** 7))
    Sl = 1 + 0.015 * (Lbp - 50) ** 2 / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc, Sh = 1 + 0.045 * Cbp, 1 + 0.015 * Cbp * T
    Rt = -np.sin(np.radians(2 * dth)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh))


# ------------------------------------------------------------------ camera model
def base_curve(name):
    import numpy as np
    return np.array(DATA['curves'][name], float) / 16384


def inverse(c, y):
    """Inverse of a base curve on its rising part: output (0..1) -> curve index."""
    import numpy as np
    top = int(np.argmax(c >= 1.0)) if (c >= 1.0).any() else 255
    xs, ys = c[:top + 1], np.arange(256.)[:top + 1]
    keep = np.concatenate([[True], np.diff(xs) > 0])
    return np.interp(np.clip(y, 0, 1), xs[keep], ys[keep])


def to_index(y):
    """Camera output of its neutral rendering (0..1) -> linear signal in curve-index units."""
    return inverse(base_curve('neutral'), y)


def compose(curve, base):
    """The camera's composition of a look curve after the base curve (integer, as the firmware)."""
    out = []
    for v in base:
        i, f = v >> 6, v & 0x3f
        if i > 0xfe:
            out.append(int(curve[255]))
        else:
            p = (int(curve[i + 1]) - int(curve[i])) * f
            out.append(int(curve[i]) + ((p + 0x3f) >> 6 if p < 0 else p >> 6))
    return out


def exported(curves):
    """The camera's final per-channel tone curves (10-bit) for a look."""
    import numpy as np
    base = [int(x) for x in DATA['curves']['slot']]
    src = [base] * 3 if not curves else [compose(c, base) for c in curves]
    return [np.minimum(np.array(s) >> 4, 1023) for s in src]


def render(v, look, bounds=None):
    """Linear signal (N,3, index units) -> what the camera writes (0..1), firmware-exact curves."""
    import numpy as np
    M = np.array(look['matrix_q13'], float) / 8192
    z = v @ M.T
    q = (look.get('hue_sectors') or {}).get('matrices_q10')
    if q:
        z = hue_apply_q(z, np.array(q), HUE_BOUNDS if bounds is None else bounds)
    ex = exported(look.get('curves'))
    out = np.stack([np.interp(np.clip(z[:, c], 0, 255), np.arange(256.), ex[c]) / 1023 for c in range(3)], 1)
    p = look.get('post_matrix_q9')
    if p:
        out = np.clip(out @ post_effective(p).T, 0, 1)
    return out


# ------------------------------------------------------------------ colour after the tone curves
# The camera can mix the colours once more after the tone curves: a 3x3 matrix P whose rows sum to 1, so greys
# stay grey. Film looks often treat colours differently in shadows and highlights; a mix after the curves can
# follow that where the colour matrix before them cannot. Every Hue step is set, each with the camera's own Hue
# change on top of the look.
POST_LIMIT = 0.98         # the camera's values beyond this are penalised while fitting (keeps them in range)
POST_PHOTO_WEIGHTS = (0.5, 2.0, 8.0)    # photo and grey weights tried, so everyday content can count for more
POST_WARM_LR = 1e-3                     # smaller steps when continuing from a fitted look


def post_hue(h):
    """The camera's own values for Hue step h (0..8, 4 = Hue 0)."""
    import numpy as np
    a, s, d = POST['hue'][h]
    return np.array([[8192, 0, 0], [0, a, s], [0, -s, d]], np.int64)


def post_targets(p_q9):
    """The camera's nine matrices (one per Hue step) for P; the look is shown at Hue 0 (step 4), the other
    steps are kept within the camera's range."""
    import numpy as np
    P = np.array(p_q9, float) / 512
    Y = np.array(HUE_YCC, np.int64)
    out = []
    for h in range(9):
        H = post_hue(h) @ Y / 8192                             # the camera's own matrix at this Hue step
        exact = H @ P
        t = np.rint(exact).astype(int)
        for r in range(3):                                     # rows keep their sums: greys stay grey
            d = int(np.rint(H[r].sum())) - t[r].sum()
            while d:
                j = int(np.argmax((exact[r] - t[r]) * np.sign(d)))
                t[r, j] += np.sign(d)
                d -= np.sign(d)
        out.append(t if h == 4 else np.clip(t, -256, 255))
    return np.array(out)


def post_effective(p_q9):
    """P as the camera really applies it at Hue 0, after rounding."""
    import numpy as np
    return np.linalg.solve(np.array(HUE_YCC, float), post_targets(p_q9)[4].astype(float))


def post_quantise(P):
    import numpy as np
    q = np.rint(np.asarray(P) * 512).astype(int)
    for r in range(3):
        q[r, r] += 512 - q[r].sum()
    t = post_targets(q)
    if t.min() < -256 or t.max() > 255:
        raise Refused(f'colour matrix after the curves is out of the camera\'s range ({t.min()}..{t.max()})')
    return q


# ------------------------------------------------------------------ fitting
START_SPREAD = dict(matrix=0.1, curve=0.25, after=0.03)   # how far a random start (turns, --seed) may lie
CHANNELS = {'single': [0, 0, 0], 'rg-b': [0, 0, 1], 'r-gb': [0, 1, 1], 'rb-g': [0, 1, 0], 'rgb': [0, 1, 2]}


def _softplus(x):
    import numpy as np
    return np.logaddexp(0, x)


class Fitter:
    """Gradient descent (Adam) on a perceptual colour difference.

    Model: out = P . C[ T(M v) ] with M a white-preserving 3x3 matrix, T the slot's
    base curve, C one, two or three free non-decreasing curves (curve mode) and, with
    post, P a colour mix after the curves (rows sum to 1; otherwise P = I). Loss per colour: a
    CIE94/CIEDE2000-style weighted Lab distance in the target's lightness/chroma/hue
    frame, sqrt'ed so it behaves like ΔE; plus a smoothness penalty on the curves.
    """

    def __init__(self, v, target, mode='single', smooth=50.0, weight=None, post=False):
        import numpy as np
        self.v, self.mode, self.smooth, self.post = v, mode, smooth, post
        w = np.ones(len(v)) if weight is None else np.asarray(weight, float)
        self.w = w / w.sum()                                          # per-colour weights, sum 1
        self.chan = CHANNELS[mode]
        lt = lab(target)
        self.lt = lt
        C = np.hypot(lt[:, 1], lt[:, 2])
        self.u = np.where(C[:, None] > 1e-6, lt[:, 1:] / np.maximum(C, 1e-6)[:, None], [1.0, 0.0])
        self.h = np.stack([-self.u[:, 1], self.u[:, 0]], 1)
        self.SL = 1 + 0.015 * (lt[:, 0] - 50) ** 2 / np.sqrt(20 + (lt[:, 0] - 50) ** 2)
        self.SC, self.SH = 1 + 0.045 * C, 1 + 0.015 * C
        self.T = base_curve('slot')
        self.Tslope = np.diff(self.T)

    def curves(self, raw):
        import numpy as np
        return np.cumsum(_softplus(raw), 1)

    def loss_grad(self, theta, raw, phi=None):
        import numpy as np
        v, N = self.v, len(self.v)
        M = np.concatenate([theta, 1 - theta.sum(1, keepdims=True)], 1)
        z = v @ M.T
        zc = np.clip(z, 0, 255)
        i = np.minimum(np.floor(zc).astype(int), 254)
        f = zc - i
        b = self.T[i] * (1 - f) + self.T[i + 1] * f
        mz = (z > 0) & (z < 255)
        Ck = self.curves(raw)
        Cc = Ck[self.chan]                                           # (3,256)
        idx = np.clip(b * 256, 0, 255)
        j = np.minimum(np.floor(idx).astype(int), 254)
        g = idx - j
        rows = np.arange(3)[None, :]
        c0, c1 = Cc[rows, j], Cc[rows, j + 1]
        out = c0 * (1 - g) + c1 * g
        mi = (b * 256 > 0) & (b * 256 < 255)
        cur = out
        if phi is not None:                                          # colour matrix after the curves
            P = np.concatenate([phi, 1 - phi.sum(1, keepdims=True)], 1)
            out = cur @ P.T
        # Lab of the prediction
        x = np.clip(out, 0, 1)
        lin = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
        dlin = np.where(x <= 0.04045, 1 / 12.92, 2.4 / 1.055 * ((x + 0.055) / 1.055) ** 1.4) * ((out > 0) & (out < 1))
        t = lin @ np.array(XYZ).T / np.array(WHITE)
        big = t > 216 / 24389
        F = np.where(big, np.cbrt(np.maximum(t, 1e-12)), (24389 / 27 * t + 16) / 116)
        dF = np.where(big, 1 / 3 * np.maximum(t, 1e-12) ** (-2 / 3), 24389 / 27 / 116)
        lp = np.stack([116 * F[:, 1] - 16, 500 * (F[:, 0] - F[:, 1]), 200 * (F[:, 1] - F[:, 2])], 1)
        d = lp - self.lt
        dL = d[:, 0]
        dC = (d[:, 1:] * self.u).sum(1)
        dH = (d[:, 1:] * self.h).sum(1)
        q = (dL / self.SL) ** 2 + (dC / self.SC) ** 2 + (dH / self.SH) ** 2
        e = np.sqrt(q + 1e-8)
        loss = (self.w * e).sum()
        w = self.w / (2 * e)
        gL = w * 2 * dL / self.SL ** 2
        gab = (w * 2)[:, None] * ((dC / self.SC ** 2)[:, None] * self.u + (dH / self.SH ** 2)[:, None] * self.h)
        gLab = np.concatenate([gL[:, None], gab], 1)
        J = np.array([[0, 116, 0], [500, -500, 0], [0, 200, -200]], float)
        gF = gLab @ J
        gt = gF * dF / np.array(WHITE)
        glin = gt @ np.array(XYZ)
        gout = glin * dlin
        gphi = None
        if phi is not None:
            gphi = np.stack([(gout * (cur[:, 0:1] - cur[:, 2:3])).sum(0), (gout * (cur[:, 1:2] - cur[:, 2:3])).sum(0)], 1)
            Y = np.array(HUE_YCC, float) / 256
            YP = Y @ P
            over = np.maximum(np.abs(YP) - POST_LIMIT, 0) * np.sign(YP)
            gP = Y.T @ (2 * HUE_RANGE * over)
            gphi += gP[:, :2] - gP[:, 2:3]
            loss_pen = HUE_RANGE * (over ** 2).sum()
            gout = gout @ P
        # through the look curves
        gC = np.zeros_like(Ck)
        for c in range(3):
            k = self.chan[c]
            np.add.at(gC[k], j[:, c], gout[:, c] * (1 - g[:, c]))
            np.add.at(gC[k], j[:, c] + 1, gout[:, c] * g[:, c])
        gidx = gout * (c1 - c0) * mi
        gz = gidx * 256 * self.Tslope[i] * mz
        gtheta = np.stack([(gz * (v[:, 0:1] - v[:, 2:3])).sum(0), (gz * (v[:, 1:2] - v[:, 2:3])).sum(0)], 1)
        # smoothness penalty
        D = Ck[:, 2:] - 2 * Ck[:, 1:-1] + Ck[:, :-2]
        pen = self.smooth * (D ** 2).sum(1).mean()
        gD = 2 * self.smooth * D / len(Ck)
        gC[:, 2:] += gD
        gC[:, 1:-1] -= 2 * gD
        gC[:, :-2] += gD
        sig = 1 / (1 + np.exp(-raw))
        graw = sig * np.cumsum(gC[:, ::-1], 1)[:, ::-1]
        if phi is not None:
            pen += loss_pen
        return loss + pen, loss, gtheta, graw, gphi

    def run(self, target, iters=1500, lr=5e-3, log=None, init=None, rng=None):
        """init: continue from a fitted look (same curve mode); rng: a random start (a different
        starting point for each seed) instead of the classic one; neither: the classic start."""
        import numpy as np
        v = self.v
        if init is not None:                         # continue from a fitted look (same curve mode)
            M0 = np.array(init['matrix_q13'], float) / 8192
            theta = M0[:, :2].copy()
            first = [self.chan.index(k) for k in range(max(self.chan) + 1)]
            C0 = np.array(init['curves'], float)[first] / 16384
            dd = np.clip(np.diff(np.concatenate([np.zeros((len(C0), 1)), C0], 1), axis=1), 1e-6, None)
            raw = np.log(np.expm1(dd))
            return self._descend(theta, raw, iters, lr, log)
        # least-squares start for the matrix, identity start for the curves
        inv_t = inverse(self.T, target)
        theta = np.zeros((3, 2))
        xr = v[:, :2] - v[:, 2:3]
        for c in range(3):
            theta[c] = np.linalg.lstsq(xr, inv_t[:, c] - v[:, 2], rcond=None)[0]
        if not np.isfinite(theta).all() or np.abs(theta).max() > 3:
            theta = np.array([[1., 0.], [0., 1.], [0., 0.]])
        ident = np.arange(256) / 256.0
        dd = np.clip(np.diff(np.concatenate([[0.0], ident])), 1e-6, None)
        raw = np.tile(np.log(np.expm1(dd)), (max(self.chan) + 1, 1))
        phi = None
        if rng is not None:                      # a random start around the classic one
            theta = theta + rng.normal(0, START_SPREAD['matrix'], theta.shape)
            n = max(self.chan) + 1
            g = np.exp(rng.normal(0, START_SPREAD['curve'], n))[:, None]      # a random gamma per curve
            dd = np.clip(np.diff(np.concatenate([np.zeros((n, 1)), np.tile(ident, (n, 1)) ** g], 1), axis=1), 1e-6, None)
            raw = np.log(np.expm1(dd))
            if self.post:
                phi = np.array([[1., 0.], [0., 1.], [0., 0.]]) + rng.normal(0, START_SPREAD['after'], (3, 2))
        return self._descend(theta, raw, iters, lr, log, phi)

    def _descend(self, theta, raw, iters, lr, log, phi=None):
        import numpy as np
        if self.post and phi is None:
            phi = np.array([[1., 0.], [0., 1.], [0., 0.]])
        elif not self.post:
            phi = None
        params = (theta, raw) + ((phi,) if self.post else ())
        state = [[np.zeros_like(p), np.zeros_like(p)] for p in params]
        hist = []
        for it in range(iters):
            if it % 25 == 0:
                check_stop()
            total, loss, gt, gr, gp_ = self.loss_grad(theta, raw, phi)
            a = lr * 0.5 * (1 + math.cos(math.pi * it / iters))
            for p, gp, st in zip(params, (gt, gr, gp_), state):
                st[0] = 0.9 * st[0] + 0.1 * gp
                st[1] = 0.999 * st[1] + 0.001 * gp ** 2
                mh, vh = st[0] / (1 - 0.9 ** (it + 1)), st[1] / (1 - 0.999 ** (it + 1))
                p -= a * mh / (np.sqrt(vh) + 1e-8)
            if it % max(1, iters // 10) == 0 or it == iters - 1:
                hist.append([it, round(float(loss), 4)])
                if log:
                    log(f'    step {it:5d}  loss {loss:.4f}')
        M = np.concatenate([theta, 1 - theta.sum(1, keepdims=True)], 1)
        q = np.round(M * 8192).astype(int)
        q[:, 2] = 8192 - q[:, 0] - q[:, 1]
        Ck = np.clip(np.round(self.curves(raw) * 16384), 0, 16383).astype(int)
        Ck = np.maximum.accumulate(Ck, axis=1)
        out = dict(matrix_q13=q.tolist(), curve_mode=self.mode,
                   curves=[Ck[k].tolist() for k in self.chan], history=hist)
        if self.post:
            out['post_matrix_q9'] = post_quantise(np.concatenate([phi, 1 - phi.sum(1, keepdims=True)], 1)).tolist()
        return out


# ------------------------------------------------------------------ hue sectors
# The camera can change colours separately in 12 ranges of hue, each with its own 3x3 matrix, after the
# look matrix and before the tone curves. The fit keeps neighbouring sectors joined (no gaps between
# colours) and greys unchanged, and keeps colours near a sector edge smooth (HUE_BUDGET).
HUE_BOUNDS = [359.8, 27.4, 62.2, 92.6, 112.0, 142.8, 179.2, 211.4, 238.6, 267.2, 306.0, 326.5]
HUE_SIGMA = [1.0, 2.5, 2.5, 1.0, 1.0, 2.5, 2.5, 2.0, 1.0, 2.0, 5.0, 3.0]
HUE_YCC = [[77, 150, 29], [-43, -85, 128], [128, -107, -21]]          # /256; also for colour after the curves
HUE_KINKS = (0.3, 1, 3, 10, 30, 100)          # seam penalty, weakest first; the first within budget is used
HUE_BUDGET = dict(photo_p99=0.3, photo_max=1.0, lattice_p99=1.0)
HUE_PHOTO_WEIGHTS = (0.5, 2.0, 8.0)          # weights tried for the photo
HUE_RIDGE, HUE_LIMIT, HUE_RANGE = 0.02, 1.85, 1e3   # size penalty; matrix range limit


def _ycc():
    import numpy as np
    S = np.array(HUE_YCC, float) / 256
    return S, np.linalg.inv(S)


def hue_coords(z, bounds):
    """Sector index and position within the sector for each colour z (N,3)."""
    import numpy as np
    S, _ = _ycc()
    bounds = np.asarray(bounds, float)
    ycc = z @ S.T
    ang = np.degrees(np.arctan2(ycc[:, 2], ycc[:, 1])) % 360
    g = np.argmin((ang[:, None] - bounds[None, :]) % 360, 1)
    th0, th1 = np.radians(bounds[g]), np.radians(bounds[(g + 1) % 12])
    det = np.cos(th0) * np.sin(th1) - np.sin(th0) * np.cos(th1)
    cb, cr = ycc[:, 1], ycc[:, 2]
    return g, (cb * np.sin(th1) - cr * np.cos(th1)) / det, (cr * np.cos(th0) - cb * np.sin(th0)) / det


def hue_matrices(D, bounds=None):
    """Per-sector 3x3 matrices for the parameters D (12,3)."""
    import numpy as np
    S, _ = _ycc()
    bounds = HUE_BOUNDS if bounds is None else bounds
    out = []
    for g in range(12):
        th0, th1 = np.radians(bounds[g]), np.radians(bounds[(g + 1) % 12])
        U = np.array([[np.cos(th0), np.cos(th1)], [np.sin(th0), np.sin(th1)]])
        out.append(np.eye(3) + np.stack([D[g], D[(g + 1) % 12]], 1) @ np.linalg.inv(U) @ S[1:])
    return np.array(out)


def hue_quantise(mats):
    import numpy as np
    q = np.rint(mats * 1024).astype(int)
    for g in range(12):
        for r in range(3):
            q[g, r, r] += 1024 - q[g, r].sum()
    if np.abs(q).max() > 2047:
        raise Refused(f'hue-sector matrix value {np.abs(q).max()} is out of range')
    return q


def hue_apply_q(z, q, bounds):
    import numpy as np
    g, _, _ = hue_coords(z, bounds)
    return np.einsum('nij,nj->ni', q[g] / 1024, z)


def hue_kinks():
    """Linear map from the parameters to the smoothness penalty at each sector edge."""
    import numpy as np
    _, S_inv = _ycc()
    th = np.radians(HUE_BOUNDS)
    perp = (S_inv @ np.stack([np.zeros_like(th), -np.sin(th), np.cos(th)])).T
    cols = []
    for k in range(36):
        e = np.zeros(36)
        e[k] = 1
        M = hue_matrices(e.reshape(12, 3))
        cols.append(np.concatenate([(M[b] - M[b - 1]) @ perp[b] for b in range(12)]))
    return np.array(cols).T * np.repeat(HUE_SIGMA, 3)[:, None]


class HueFitter:
    """Adam on the same loss as Fitter, with the look's matrix and curves fixed; parameters D (12,3)."""

    def __init__(self, v, target, weight, look):
        import numpy as np
        self.f = Fitter(v, target, look['curve_mode'] if look.get('curves') else 'single', weight=weight)
        M = np.array(look['matrix_q13'], float) / 8192
        self.z = v @ M.T
        self.g, self.a, self.b = hue_coords(self.z, HUE_BOUNDS)
        cv = look.get('curves')
        self.C = np.array(cv, float) / 16384 if cv else np.tile(np.arange(256) / 256.0, (3, 1))
        pq = look.get('post_matrix_q9')
        self.P = post_effective(pq) if pq else None
        K = hue_kinks()
        self.KtK = K.T @ K
        self.B = np.array([hue_matrices(e.reshape(12, 3)).ravel() - hue_matrices(np.zeros((12, 3))).ravel()
                           for e in np.eye(36)]).T
        self.M0 = hue_matrices(np.zeros((12, 3))).ravel()
        self.kink = HUE_KINKS[0]

    def loss_grad(self, D):
        import numpy as np
        f = self.f
        z = self.z + self.a[:, None] * D[self.g] + self.b[:, None] * D[(self.g + 1) % 12]
        zc = np.clip(z, 0, 255)
        i = np.minimum(np.floor(zc).astype(int), 254)
        fr = zc - i
        bb = f.T[i] * (1 - fr) + f.T[i + 1] * fr
        mz = (z > 0) & (z < 255)
        idx = np.clip(bb * 256, 0, 255)
        j = np.minimum(np.floor(idx).astype(int), 254)
        gg = idx - j
        rows = np.arange(3)[None, :]
        c0, c1 = self.C[rows, j], self.C[rows, j + 1]
        out = c0 * (1 - gg) + c1 * gg
        mi = (bb * 256 > 0) & (bb * 256 < 255)
        if self.P is not None:
            out = out @ self.P.T
        x = np.clip(out, 0, 1)
        lin = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
        dlin = np.where(x <= 0.04045, 1 / 12.92, 2.4 / 1.055 * ((x + 0.055) / 1.055) ** 1.4) * ((out > 0) & (out < 1))
        t = lin @ np.array(XYZ).T / np.array(WHITE)
        big = t > 216 / 24389
        F = np.where(big, np.cbrt(np.maximum(t, 1e-12)), (24389 / 27 * t + 16) / 116)
        dF = np.where(big, 1 / 3 * np.maximum(t, 1e-12) ** (-2 / 3), 24389 / 27 / 116)
        lp = np.stack([116 * F[:, 1] - 16, 500 * (F[:, 0] - F[:, 1]), 200 * (F[:, 1] - F[:, 2])], 1)
        d = lp - f.lt
        dL, dC, dH = d[:, 0], (d[:, 1:] * f.u).sum(1), (d[:, 1:] * f.h).sum(1)
        e = np.sqrt((dL / f.SL) ** 2 + (dC / f.SC) ** 2 + (dH / f.SH) ** 2 + 1e-8)
        loss = (f.w * e).sum()
        w = f.w / (2 * e)
        gLab = np.concatenate([(w * 2 * dL / f.SL ** 2)[:, None],
                               (w * 2)[:, None] * ((dC / f.SC ** 2)[:, None] * f.u + (dH / f.SH ** 2)[:, None] * f.h)], 1)
        gout = ((gLab @ np.array([[0, 116, 0], [500, -500, 0], [0, 200, -200]], float)) * dF / np.array(WHITE)
                @ np.array(XYZ)) * dlin
        if self.P is not None:
            gout = gout @ self.P
        gz = gout * (c1 - c0) * mi * 256 * f.Tslope[i] * mz
        gD = np.zeros((12, 3))
        np.add.at(gD, self.g, self.a[:, None] * gz)
        np.add.at(gD, (self.g + 1) % 12, self.b[:, None] * gz)
        dv = D.ravel()
        pen = self.kink * (dv @ self.KtK @ dv) / 12 + HUE_RIDGE * (dv @ dv) / 12
        gpen = self.kink * 2 / 12 * (self.KtK @ dv) + HUE_RIDGE * 2 / 12 * dv
        mv = self.M0 + self.B @ dv
        over = np.maximum(np.abs(mv) - HUE_LIMIT, 0)
        pen += HUE_RANGE * (over ** 2).sum()
        gpen += self.B.T @ (HUE_RANGE * 2 * over * np.sign(mv))
        return loss + pen, loss, gD + gpen.reshape(12, 3)

    def run(self, iters=800, lr=0.02):
        import numpy as np
        D = np.zeros((12, 3))
        m1, m2 = np.zeros_like(D), np.zeros_like(D)
        for it in range(iters):
            if it % 25 == 0:
                check_stop()
            total, loss, gD = self.loss_grad(D)
            a = lr * 0.5 * (1 + math.cos(math.pi * it / iters))
            m1 = 0.9 * m1 + 0.1 * gD
            m2 = 0.999 * m2 + 0.001 * gD ** 2
            D -= a * (m1 / (1 - 0.9 ** (it + 1))) / (np.sqrt(m2 / (1 - 0.999 ** (it + 1))) + 1e-8)
        return D


def hue_seam_risk(look, y, held):
    """How much colours near sector edges could change (ΔE00), to keep them smooth."""
    import numpy as np
    out = {}
    for name, pts in (('lattice', y), ('photo', held[0] if held else None)):
        if pts is None:
            continue
        v = to_index(pts)
        nom = lab(np.clip(render(v, look), 0, 1))
        e = np.max([de00(nom, lab(np.clip(render(v, look, np.array(HUE_BOUNDS) + sg * np.array(HUE_SIGMA)), 0, 1)))
                    for sg in (-1, 1)], 0)
        out[name] = dict(p99=round(float(np.percentile(e, 99)), 3), max=round(float(e.max()), 3))
    return out


def hue_within_budget(sr):
    ok = sr['lattice']['p99'] <= HUE_BUDGET['lattice_p99']
    if 'photo' in sr:
        ok = ok and sr['photo']['p99'] <= HUE_BUDGET['photo_p99'] and sr['photo']['max'] <= HUE_BUDGET['photo_max']
    return ok


def more_levers_ok(new, old):
    """Accept more levers only for a clear gain (whole range or ColorChecker) with nothing worse."""
    def f(s):
        return dict(e=s['e'], cc=s['cc'], grey=s['grey'], h=s.get('h'))
    n, o = f(new), f(old)
    gain = n['e'] < o['e'] * (1 - PAIR_GAIN) or n['cc'] < o['cc'] * (1 - PAIR_GAIN)
    no_worse = ((o['h'] is None or n['h'] <= o['h'] + HELD_TOL) and n['grey'] <= o['grey'] + GREY_TOL
                and n['cc'] <= o['cc'] + CC_TOL and n['e'] <= o['e'] * (1 + RANGE_TOL))
    return gain and no_worse


def fit_hue(look, v, t, weight, n_lut, y, ty, held, lut, space, iters=800, log=print):
    """Hue sectors on top of a fitted look (matrix and curves kept). Returns the hue_sectors data
    to add to the look, or None when they do not clearly help."""
    import numpy as np

    def summary(lk):
        ev = everyday(lk, lut, space)
        return dict(e=evaluate(lk, y, ty)['de00_mean'], cc=ev['colorchecker']['de00_mean'],
                    grey=ev['greys']['de00_mean'], h=evaluate(lk, *held)['de00_mean'] if held else None)
    base = summary(look)
    candidates = []
    for pw in (HUE_PHOTO_WEIGHTS if held else HUE_PHOTO_WEIGHTS[:1]):
        w = np.asarray(weight, float).copy()
        w[n_lut:] *= pw / PHOTO_WEIGHT
        hf = HueFitter(v, t, w / w.sum(), look)
        for kink in HUE_KINKS:
            hf.kink = kink
            D = hf.run(iters)
            try:
                q = hue_quantise(hue_matrices(D))
            except Refused:
                continue
            cand = dict(look, hue_sectors=dict(matrices_q10=q.tolist()))
            sr = hue_seam_risk(cand, y, held)
            if not hue_within_budget(sr):
                continue
            s = summary(cand)
            ok = more_levers_ok(s, base)
            log(f'  hue     ΔE00 {s["e"]:.3f}' + (f'  photo {s["h"]:.3f}' if s['h'] is not None else '')
                + f'  ColorChecker {s["cc"]:.3f}  greys {s["grey"]:.3f}' + ('' if ok else '  (not better)'))
            if ok:
                candidates.append((s['e'], dict(matrices_q10=q.tolist(), photo_weight=pw, seam_weight=kink,
                                                seam_risk=sr)))
            break
    if not candidates:
        log('  hue sectors: no clear gain, not used')
        return None
    return min(candidates, key=lambda c: c[0])[1]


# ------------------------------------------------------------------ scoring
HUES = {'red': (330, 30), 'orange/skin': (30, 75), 'yellow': (75, 105), 'green': (105, 180),
        'cyan': (180, 240), 'blue': (240, 290), 'purple/magenta': (290, 330)}


def score(pred, target):
    import numpy as np
    lt, lp = lab(target), lab(np.clip(pred, 0, 1))
    e = de00(lt, lp)
    C = np.hypot(lt[:, 1], lt[:, 2])
    h = np.degrees(np.arctan2(lt[:, 2], lt[:, 1])) % 360

    def band(mk):
        return round(float(e[mk].mean()), 3) if mk.sum() >= 20 else None
    r = dict(colours=int(len(e)), de00_mean=round(float(e.mean()), 3), de00_p95=round(float(np.percentile(e, 95)), 3),
             brightness_shift=round(float((lp[:, 0] - lt[:, 0]).mean()), 2),
             hue={k: band((((h >= a) | (h < b)) if a > b else ((h >= a) & (h < b))) & (C >= 10))
                  for k, (a, b) in HUES.items()})
    r['verdict'] = 'good' if r['de00_mean'] < 1 and r['de00_p95'] < 2 else \
        'approximate' if r['de00_mean'] < 2 and r['de00_p95'] < 4 else 'poor'
    return r


def evaluate(look, y, target):
    return score(render(to_index(y), look), target)


def unchanged(y, target):
    """How far the LUT is from the camera's neutral rendering (no look at all)."""
    return score(neutral(to_index(y)), target)


# ------------------------------------------------------------------ photos (optional)
def photo_signal(dng, max_side=400):
    """Linear signal of a DNG in the camera model's units (needs rawpy)."""
    import numpy as np
    try:
        import rawpy
    except ImportError:
        raise Refused('--photo / preview need rawpy and pillow: python3 -m pip install rawpy pillow')
    with rawpy.imread(str(dng)) as r:
        img = r.raw_image_visible.astype(np.float32)
        s = r.sizes
        black = np.array(r.black_level_per_channel, np.float32)
        white = float(r.white_level)
        cl = r.raw_colors_visible
        wb = np.array(r.camera_whitebalance[:3], float)
    if s.crop_height and s.crop_width:
        t, l = s.crop_top_margin, s.crop_left_margin
        img, cl = img[t:t + s.crop_height, l:l + s.crop_width], cl[t:t + s.crop_height, l:l + s.crop_width]
    img = (img - black[cl]) / (white - black[cl])
    h, w = img.shape[0] // 2 * 2, img.shape[1] // 2 * 2
    q = img[:h, :w].reshape(h // 2, 2, w // 2, 2).transpose(0, 2, 1, 3).reshape(h // 2, w // 2, 4)
    qc = cl[:2, :2].reshape(4)
    rgb = np.stack([q[..., [k for k in range(4) if qc[k] in idx]].mean(-1) for idx in ([0], [1, 3], [2])], -1)
    f = max(1, int(np.ceil(max(rgb.shape[:2]) / max_side)))
    H, W = rgb.shape[0] // f * f, rgb.shape[1] // f * f
    rgb = rgb[:H, :W].reshape(H // f, f, W // f, f, 3).mean((1, 3))
    yy, xx = np.mgrid[0:rgb.shape[0], 0:rgb.shape[1]]
    r2 = ((yy - rgb.shape[0] / 2) / (rgb.shape[0] / 2)) ** 2 + ((xx - rgb.shape[1] / 2) / (rgb.shape[1] / 2)) ** 2
    cam = rgb * (wb / wb[1]) * np.exp(0.18 * r2)[..., None]
    return cam @ (np.array(DATA['camera_matrix']) / 8192).T * 100.0


def neutral(v):
    """The camera's neutral rendering of a linear signal (no look)."""
    import numpy as np
    T = base_curve('neutral')
    return np.stack([np.interp(np.clip(v[..., c], 0, 255), np.arange(256.), T) for c in range(3)], -1)


def apply_lut(lut, y, space):
    if space == 'adobe':
        return adobe_lut_on_srgb(lut, y)[0]
    return lut(y)


# ------------------------------------------------------------------ fit command
PAIR_GAIN, HELD_TOL, PHOTO_GAIN = 0.10, 0.02, 0.5
PHOTO_WEIGHT = 0.5      # the photo counts half as much as the LUT's own colours, however many pixels it has
GREY_WEIGHT = 0.5       # neutral greys count half as much too: a colour cast on greys is the most visible error
GREY_TOL, CC_TOL = 0.10, 0.10            # more levers must not make greys or the ColorChecker worse by more;
                                         # these, RANGE_TOL and HELD_TOL are also the margins when comparing results
GREY_GAIN, RANGE_TOL = 0.5, 0.05         # ... and count as a gain when greys improve by 0.5 (range within 5 %)
EVERYDAY_CC, EVERYDAY_GREY, EVERYDAY_CAST = 2.0, 2.0, 2.5
# "good on everyday colours": ColorChecker mean < 2 and greys < 2 (a small visible difference at most),
# and no visible colour cast on any grey (largest a*b* difference < 2.5)

# X-Rite ColorChecker Classic, widely published sRGB values (8-bit): everyday reference colours
COLORCHECKER = [
    ('dark skin', 115, 82, 68), ('light skin', 194, 150, 130), ('blue sky', 98, 122, 157),
    ('foliage', 87, 108, 67), ('blue flower', 133, 128, 177), ('bluish green', 103, 189, 170),
    ('orange', 214, 126, 44), ('purplish blue', 80, 91, 166), ('moderate red', 193, 90, 99),
    ('purple', 94, 60, 108), ('yellow green', 157, 188, 64), ('orange yellow', 224, 163, 46),
    ('blue', 56, 61, 150), ('green', 70, 148, 73), ('red', 175, 54, 60), ('yellow', 231, 199, 31),
    ('magenta', 187, 86, 149), ('cyan', 8, 133, 161), ('white', 243, 243, 242), ('neutral 8', 200, 200, 200),
    ('neutral 6.5', 160, 160, 160), ('neutral 5', 122, 122, 121), ('neutral 3.5', 85, 85, 85), ('black', 52, 52, 52)]


def greys(n=64):
    import numpy as np
    x = np.linspace(0.02, 1.0, n)
    return np.stack([x, x, x], 1)


def everyday(look, lut, space):
    """Scores on the ColorChecker and on a neutral grey ramp (with the largest colour cast on the greys)."""
    import numpy as np
    out = {}
    for name, y in (('colorchecker', np.array([c[1:] for c in COLORCHECKER], float) / 255), ('greys', greys())):
        t = apply_lut(lut, y, space)
        lt, lp = lab(t), lab(np.clip(render(to_index(y), look), 0, 1))
        e = de00(lt, lp)
        r = dict(de00_mean=round(float(e.mean()), 3), de00_max=round(float(e.max()), 3))
        if name == 'colorchecker':
            r['worst'] = COLORCHECKER[int(e.argmax())][0]
            r['skin'] = round(float(e[:2].mean()), 3)
        else:
            r['cast_max'] = round(float(np.hypot(*(lp[:, 1:] - lt[:, 1:]).T).max()), 2)
        out[name] = r
    return out


def fit_lut(path, space='srgb', curve='auto', photo=None, iters=1500, log=print, hue=True, post=False, seed=None):
    """One turn of fitting. seed None: the classic start; a number: a random start (reproducible).
    With post, the result also carries 'after_curves': the version with colour after the curves, when it
    is better than the usual one on at least one measure."""
    import numpy as np
    rng = None if seed is None else np.random.default_rng(seed)
    lut = Lut(path)
    if lut.lo.tolist() == [0, 0, 0] and lut.hi.tolist() == [1, 1, 1]:
        y = grid(lut.n)
    else:
        y = grid(33)
    if space == 'adobe':
        t, keep = adobe_lut_on_srgb(lut, y)
        y, t = y[keep], t[keep]
    else:
        t = lut(y)
    log(f'{Path(path).name}: {lut.kind} LUT, {len(y)} colours, {space}'
        + (f' (ignored: {", ".join(sorted(set(lut.ignored)))})' if lut.ignored else ''))
    ty = t
    gy = greys()
    v = np.concatenate([to_index(y), to_index(gy)])
    t = np.concatenate([t, apply_lut(lut, gy, space)])
    weight = np.concatenate([np.ones(len(y)) / len(y), np.full(len(gy), GREY_WEIGHT / len(gy))])
    held = None
    n_fixed = len(v)
    if photo:
        img = photo_signal(photo)
        top = np.zeros(img.shape[:2], bool)
        top[: img.shape[0] // 2] = True
        pv, top = img.reshape(-1, 3), top.ravel()
        yb = neutral(pv)
        ok = (yb.max(1) < 0.98) & (yb.min(1) > 0.01)
        tb = apply_lut(lut, yb, space)
        n_photo = int((ok & top).sum())
        v = np.concatenate([v, pv[ok & top]])
        t = np.concatenate([t, tb[ok & top]])
        weight = np.concatenate([weight, np.full(n_photo, PHOTO_WEIGHT / max(n_photo, 1))])
        held = (yb[ok & ~top], tb[ok & ~top])
        log(f'  photo: {int((ok & top).sum())} pixels join the fit, {int((ok & ~top).sum())} are held out')

    def one(mode, post=False, photo_weight=PHOTO_WEIGHT, init=None):
        w = weight
        if photo_weight != PHOTO_WEIGHT:                 # the photo and the greys together (everyday content)
            w = np.asarray(weight, float).copy()
            w[n_fixed - len(gy):] *= photo_weight / PHOTO_WEIGHT
        try:
            look = Fitter(v, t, mode, weight=w, post=post).run(t, iters=iters, init=init, rng=None if init else rng,
                                                              lr=5e-3 if init is None else POST_WARM_LR)
        except Refused as exc:
            log(f'  {mode} + colour after the curves: {exc}')
            return None
        e = evaluate(look, y, ty)['de00_mean']
        h = evaluate(look, *held)['de00_mean'] if held else None
        ev = everyday(look, lut, space)
        log(f'  {mode + (f" + after (photo {photo_weight}{"" if init else ", from scratch"})" if post else ""):7s} ΔE00 {e:.3f}' + (f'  photo {h:.3f}' if h is not None else '')
            + f'  ColorChecker {ev["colorchecker"]["de00_mean"]:.3f}  greys {ev["greys"]["de00_mean"]:.3f}')
        return dict(e=e, h=h, cc=ev['colorchecker']['de00_mean'], grey=ev['greys']['de00_mean'], look=look)
    def better(new, old):
        """More levers (curves, colour after the curves) only when they clearly help (whole range,
        everyday colours or greys) and make nothing worse (photo, greys, ColorChecker, whole range)."""
        gain = (new['e'] < old['e'] * (1 - PAIR_GAIN) or new['cc'] < old['cc'] * (1 - PAIR_GAIN)
                or new['grey'] <= old['grey'] - GREY_GAIN)
        no_worse = ((old['h'] is None or new['h'] <= old['h'] + HELD_TOL) and new['grey'] <= old['grey'] + GREY_TOL
                    and new['cc'] <= old['cc'] + CC_TOL and new['e'] <= old['e'] * (1 + RANGE_TOL))
        return gain and no_worse
    if curve == 'auto':
        use = one('single')
        p = min((one(m) for m in ('rg-b', 'r-gb', 'rb-g')), key=lambda r: r['e'])
        if better(p, use):
            use = p
        t3 = one('rgb')
        if better(t3, use):
            use = t3
    elif curve == 'pair':
        use = min((one(m) for m in ('rg-b', 'r-gb', 'rb-g')), key=lambda r: r['e'])
    else:
        use = one(curve)

    def with_hue(lk):
        if hue:
            sectors = fit_hue(lk, v, t, weight, n_fixed, y, ty, held, lut, space, log=log)
            if sectors:
                lk = dict(lk, hue_sectors=sectors)
        return lk

    def summary(lk):
        ev = everyday(lk, lut, space)
        return dict(e=evaluate(lk, y, ty)['de00_mean'], h=evaluate(lk, *held)['de00_mean'] if held else None,
                    cc=ev['colorchecker']['de00_mean'], grey=ev['greys']['de00_mean'], look=lk)
    look = with_hue(use['look'])
    alt = None
    if post:                    # a colour matrix after the curves: a second version when it improves anything
        found = []
        starts = [(pw, use['look']) for pw in (POST_PHOTO_WEIGHTS if held else POST_PHOTO_WEIGHTS[:1])]
        for pw, init in starts + [(PHOTO_WEIGHT, None)]:      # from the fitted look, and once from scratch
            p = one(use['look']['curve_mode'], post=True, photo_weight=pw, init=init)
            if p:
                found.append(p)
        pool = [p for p in found if better(p, use)] or [p for p in found if changes(p, use)[0]]
        if pool:
            final, base = summary(with_hue(min(pool, key=lambda r: r['e'])['look'])), summary(look)
            gained, lost = changes(final, base)
            if gained:
                alt = dict(look=final['look'], recommended=better(final, base), better=gained, worse=lost)
        if alt:
            log(f'  colour after the curves: better {", ".join(alt["better"])}'
                + (f'; worse {", ".join(alt["worse"])}' if alt['worse'] else '')
                + ('  (recommended)' if alt['recommended'] else ''))
        else:
            log('  colour after the curves: no gain, not kept')

    def record(lk):
        scores = dict(fit=evaluate(lk, y, ty), unchanged=unchanged(y, ty), **everyday(lk, lut, space))
        if held:
            scores['photo'] = evaluate(lk, *held)
            scores['photo_unchanged'] = unchanged(*held)
        scores['fit_score'] = fit_score(lk, v, t, weight)
        ok, why = decide(scores)
        return dict(tool=f'GR3-LUTSmith {VERSION}', seed=seed,
                    lut=dict(file=Path(path).name, sha256=lut.sha256,
                             kind=lut.kind, title=lut.title, space=space),
                    matrix_q13=lk['matrix_q13'], curve_mode=lk['curve_mode'], curves=lk['curves'],
                    hue_sectors=lk.get('hue_sectors'), post_matrix_q9=lk.get('post_matrix_q9'),
                    scores=scores, decision=dict(build=ok, reason=why), fit=dict(iters=iters, history=lk['history']))
    out = record(look)
    if alt:
        out['after_curves'] = dict(record(alt['look']), recommended=alt['recommended'],
                                   compared=dict(better=alt['better'], worse=alt['worse']))
    return out


def fit_score(look, v, t, weight):
    """What the fit itself aims at: the weighted average ΔE00 over the LUT's colours, the greys and the part
    of the photo used for fitting (the other part of the photo stays a fair check)."""
    import numpy as np
    w = np.asarray(weight, float)
    return round(float((w * de00(lab(t), lab(np.clip(render(v, look), 0, 1)))).sum() / w.sum()), 4)


MEASURES = (('e', 'whole range'), ('cc', 'ColorChecker'), ('grey', 'greys'), ('h', 'photo'))


def changes(new, old):
    """Measures that got clearly better / worse: by more than RANGE_TOL (relative), CC_TOL, GREY_TOL or HELD_TOL."""
    tol = dict(e=old['e'] * RANGE_TOL, cc=CC_TOL, grey=GREY_TOL, h=HELD_TOL)
    better = [name for k, name in MEASURES if old.get(k) is not None and new[k] < old[k] - tol[k]]
    worse = [name for k, name in MEASURES if old.get(k) is not None and new[k] > old[k] + tol[k]]
    return better, worse


def decide(scores):
    """Build when the LUT's whole colour range fits, or when everyday colours do."""
    f = scores['fit']
    if f['verdict'] != 'poor':
        return True, f'fit {f["verdict"]}'
    cc, gr = scores['colorchecker'], scores['greys']
    p, p0 = scores.get('photo'), scores.get('photo_unchanged')
    photo_ok = p is None or (p['verdict'] != 'poor' and p['de00_mean'] <= p0['de00_mean'])
    no_cast = gr['de00_mean'] < EVERYDAY_GREY and gr['cast_max'] < EVERYDAY_CAST
    if cc['de00_mean'] < EVERYDAY_CC and no_cast and photo_ok:
        return True, (f'everyday colours fit (ColorChecker {cc["de00_mean"]}, greys {gr["de00_mean"]}, '
                      f'largest cast {gr["cast_max"]}); only extreme saturated colours deviate')
    if p and p['verdict'] == 'good' and p['de00_mean'] <= PHOTO_GAIN * p0['de00_mean'] and no_cast:
        return True, f'fit poor on extreme colours, but good on your photo ({p["de00_mean"]} vs {p0["de00_mean"]})'
    return False, (f'fit poor; ColorChecker {cc["de00_mean"]}, greys {gr["de00_mean"]} '
                   f'(largest cast {gr["cast_max"]})'
                   + (f'; photo {p["de00_mean"]} vs unchanged {p0["de00_mean"]}' if p else ''))


def describe(look, log=print):
    s = look['scores']
    f = s['fit']
    log(f'  result: {f["verdict"].upper()}  (ΔE00 {f["de00_mean"]}, p95 {f["de00_p95"]}; '
        f'without the look {s["unchanged"]["de00_mean"]})')
    if 'photo' in s:
        log(f'  on your photo: ΔE00 {s["photo"]["de00_mean"]} (without the look {s["photo_unchanged"]["de00_mean"]})')
    worst = sorted(((v, k) for k, v in f['hue'].items() if v is not None), reverse=True)[:3]
    log('  hardest colours: ' + ', '.join(f'{k} {v}' for v, k in worst))
    cc, gr = s.get('colorchecker'), s.get('greys')
    if cc and gr:
        log(f'  everyday colours: ColorChecker ΔE00 {cc["de00_mean"]} (skin {cc["skin"]}, worst {cc["worst"]}); '
            f'greys {gr["de00_mean"]} (largest cast {gr["cast_max"]})')
    hs = look.get('hue_sectors')
    log(f'  for: {SLOT_NAMES.get(look.get("slot") or DEFAULT_SLOT, "?")}'
        + (f' (shown as "{look["name"]}")' if look.get('name') else '')
        + (f', own icon from {look["icon"].get("source", "a picture")}' if look.get('icon') else '')
        + f'; curves: {look["curve_mode"]}; '
        f'hue sectors: {"yes" if hs else "no"}; '
        f'colour after the curves: {"yes" if look.get("post_matrix_q9") else "no"}; decision: {look["decision"]["reason"]}')


TURNS_DIR = 'turns'           # every kept result gets a folder here when there is more than one


def measures(rec):
    sc = rec['scores']
    return dict(e=sc['fit']['de00_mean'], cc=sc['colorchecker']['de00_mean'], grey=sc['greys']['de00_mean'],
                h=sc['photo']['de00_mean'] if 'photo' in sc else None)


def keep(kept, new):
    """The list of results worth keeping. A new result joins unless a kept one is at least as good on every
    measure; kept results that the new one beats on every measure leave the list.
    Returns (list, joined, results that left)."""
    m = measures(new)
    if any(not changes(m, measures(k))[0] for k in kept):
        return kept, False, []
    stay = [k for k in kept if changes(measures(k), m)[0]]
    return stay + [new], True, [k for k in kept if not any(k is x for x in stay)]


def result_name(rec):
    return f'turn-{rec["turn"]}' + ('-after-curves' if rec.get('post_matrix_q9') else '')


def recommended(kept):
    """The kept result the fit itself scores best (see fit_score)."""
    return min(kept, key=lambda r: r['scores']['fit_score'])


def show_kept(kept, log=print):
    best = recommended(kept)
    log(f'\n  kept results ({len(kept)}), ΔE00, lower is closer to the LUT:')
    log(f'    {"result":24s} {"whole range":>11s} {"ColorChecker":>12s} {"greys":>6s} {"photo":>6s} {"fit score":>9s}')
    for r in sorted(kept, key=lambda r: r['scores']['fit_score']):
        m = measures(r)
        log(f'    {result_name(r):24s} {m["e"]:11.2f} {m["cc"]:12.2f} {m["grey"]:6.2f} '
            + (f'{m["h"]:6.2f}' if m['h'] is not None else f'{"-":>6s}')
            + f' {r["scores"]["fit_score"]:9.3f}' + ('   recommended' if r is best else ''))
    if len(kept) > 1:
        log('  each kept result is better than every other one on at least one measure; the recommended one has\n'
            '  the best fit score. Preview them on your own photo and pick the one you like.')


def turn_seed(a, k):
    """Turn k's start: the classic one for turn 1, else a random seed (or --seed, counted on per turn)."""
    import secrets
    if a.seed is not None:
        return a.seed + k - 1
    return None if k == 1 else secrets.randbelow(2 ** 31)


def turn_header(k, seed, post):
    return (f'\nturn {k} ({"classic start" if seed is None else f"random start, seed {seed}"}'
            + (', with colour after the curves)' if post else ')'))


def record_turn(a, state, k, seed, post, res, log=print):
    """Add one finished turn to the state (kept list, history, seeds). Turns are recorded in turn order."""
    alt = res.pop('after_curves', None)
    state['history_turns'].append(dict(seed=seed, after_curves=post))
    for rec in (res, alt):
        if rec is None:
            continue
        for x in ('recommended', 'compared'):
            rec.pop(x, None)
        rec['turn'] = k
        rec['slot'] = a.slot
        if a.name:
            rec['name'] = a.name
            rec['camera_language'] = a.camera_language
            if state.get('name_accepted'):
                rec['name_accepted'] = True
        if state.get('icon'):
            rec['icon'] = state['icon']
        state['kept'], joined, left = keep(state['kept'], rec)
        state['history'].append(dict(result=result_name(rec), seed=seed, kept_when_found=joined,
                                     fit_score=rec['scores']['fit_score'], **measures(rec)))
        log(f'  {result_name(rec)}: ' + ('kept' if joined else 'not kept (an earlier result is at least as good '
                                                               'on every measure)'))
        for x in left:
            log(f'  {result_name(x)} leaves the list ({result_name(rec)} is better on every measure)')


def run_turns(a, n, state, post, log=print):
    """Run n more turns one after another (post: with colour after the curves) and update state (kept list,
    history, seeds)."""
    for _ in range(n):
        k = len(state['history_turns']) + 1
        seed = turn_seed(a, k)
        log(turn_header(k, seed, post))
        res = fit_lut(a.lut, a.space, a.curve, a.photo, a.iters, log=log, hue=not a.no_hue, post=post, seed=seed)
        record_turn(a, state, k, seed, post, res, log)


# ------------------------------------------------------------------ turns side by side
JOB_THREADS = ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'MKL_NUM_THREADS')
_JOB = {}


def _job_init(queue, stop):
    """In a helper process: report progress to the batch, and stop when it says so."""
    global STOP
    _JOB.update(queue=queue)
    STOP = stop.is_set


def _run_job(job):
    """One turn in a helper process; progress lines go to the batch as (turn, line), None when the turn starts."""
    queue, k, lines = _JOB.get('queue'), job['turn'], []

    def log(line):
        lines.append(line)
        if queue is not None:
            queue.put((k, line))
    if queue is not None:
        queue.put((k, None))
    res = fit_lut(job['lut'], job['space'], job['curve'], job['photo'], job['iters'], log=log, hue=job['hue'],
                  post=job['post'], seed=job['seed'])
    return dict(job, result=res, lines=lines)


class Batch:
    """Turns of one fit run side by side, each in a helper process using one core. Every turn gets its number and
    start when it is added, and turns are recorded in turn order, so the outcome is the same as running them one
    after another. More turns can be added while it runs; stop() ends it (finished turns stay)."""

    def __init__(self, a, state, jobs, log=print):
        import threading
        self.a, self.state, self.jobs, self.log = a, state, max(1, int(jobs)), log
        self.lock = threading.Lock()
        self.pending, self.finished = [], {}
        self.next_turn = self.next_record = len(state['history_turns']) + 1
        self.stopping = False

    def add(self, n, post):
        """Queue n turns; returns [(turn, seed)] (seed None: the classic start)."""
        a, out = self.a, []
        with self.lock:
            for _ in range(n):
                k = self.next_turn
                self.next_turn += 1
                self.pending.append(dict(turn=k, seed=turn_seed(a, k), post=bool(post), lut=str(a.lut), space=a.space,
                                         curve=a.curve, photo=str(a.photo) if a.photo else None, iters=a.iters,
                                         hue=not a.no_hue))
                out.append((k, self.pending[-1]['seed']))
        return out

    def stop(self):
        self.stopping = True

    def run(self, progress=None):
        """Run every turn added (also those added meanwhile). progress(turn, line) receives each turn's progress
        (line None when it starts). Returns when every turn is recorded, or after stop()."""
        import concurrent.futures as cf
        import multiprocessing as mp
        import os
        import queue as queues
        import time
        ctx = mp.get_context('spawn')
        q, stopper = ctx.Queue(), ctx.Event()
        saved = {v: os.environ.get(v) for v in JOB_THREADS}
        os.environ.update({v: '1' for v in JOB_THREADS})           # one core per turn
        running = {}

        def drain():
            while True:
                try:
                    k, line = q.get_nowait()
                except queues.Empty:
                    return
                if progress:
                    progress(k, line)
        try:
            with cf.ProcessPoolExecutor(self.jobs, mp_context=ctx, initializer=_job_init,
                                        initargs=(q, stopper)) as ex:
                while True:
                    with self.lock:
                        if self.stopping:
                            stopper.set()
                            for job in self.pending:
                                self.finished[job['turn']] = dict(job, result=None)
                            self.pending.clear()
                        while self.pending and len(running) < self.jobs:
                            job = self.pending.pop(0)
                            running[ex.submit(_run_job, job)] = job
                    drain()
                    for f in [f for f in running if f.done()]:
                        job = running.pop(f)
                        try:
                            self.finished[job['turn']] = f.result()
                        except Stopped:
                            self.finished[job['turn']] = dict(job, result=None)
                    self.ready()
                    with self.lock:
                        if not running and not self.pending:
                            break
                    time.sleep(0.05)
                drain()
        finally:
            for v, old in saved.items():
                if old is None:
                    os.environ.pop(v, None)
                else:
                    os.environ[v] = old

    def ready(self):
        """Turns have finished. Records them here; a window records them on its own thread instead."""
        self.record()

    def record(self):
        """Record finished turns in turn order (a stopped turn keeps its number, with no result). Returns the
        turns recorded."""
        done = []
        while self.next_record in self.finished:
            r = self.finished.pop(self.next_record)
            if r['result'] is None:
                self.state['history_turns'].append(dict(seed=r['seed'], after_curves=r['post'], stopped=True))
            else:
                self.log(turn_header(r['turn'], r['seed'], r['post']))
                for line in r['lines']:
                    self.log(line)
                record_turn(self.a, self.state, r['turn'], r['seed'], r['post'], r['result'], self.log)
            done.append(r['turn'])
            self.next_record += 1
        return done


def ask_more_turns(post):
    """Ask for more turns when someone is at the terminal. Returns (number, with colour after the curves);
    0 means stop. A plain number runs turns as the run started (post); 'a' adds turns with colour after the curves,
    'b' turns without it (before the curves only)."""
    if not sys.stdin.isatty():
        return 0, post
    while True:
        try:
            ans = input('\nMore turns? Type a number (e.g. 2), add "a" for turns with colour after the curves (2a) '
                        'or "b" for turns without it (2b); or press Enter to keep these results: ').strip().lower()
        except EOFError:
            return 0, post
        if not ans:
            return 0, post
        kind = ans[-1] if ans[-1] in 'ab' else ''
        num = ans[:-1].strip() if kind else ans
        if num.isdigit():
            return int(num), (post if not kind else kind == 'a')
        print('please type a whole number, optionally followed by a or b, or press Enter')


def check_look(look):
    """Refuse a look whose values the camera cannot take (a look.json may come from someone else)."""
    matrix_hex(look)
    if look.get('curves'):
        distinct_curves(look['curves'])
        struct.pack('<768H', *[x for c in look['curves'] for x in c])
    q = (look.get('hue_sectors') or {}).get('matrices_q10')
    if q:
        hue_table_hex(q, '00' * SLOT_DATA['templates']['hue']['mtab_len'])
    if look.get('post_matrix_q9'):
        post_check(look['post_matrix_q9'])
    slot = look_slot(look)
    if look.get('name') is not None:
        if not isinstance(look['name'], str):
            raise TypeError('the name must be text')
        check_name(look['name'], slot)
    if look.get('icon'):
        rec = look['icon']
        if len(base64.b64decode(rec['rgba'], validate=True)) != int(rec['w']) * int(rec['h']) * 4:
            raise ValueError('the icon has the wrong size')
        icon_changes(look, slot)


def load_look(path):
    try:
        look = json.loads(Path(path).read_text(encoding='utf-8'))
        if isinstance(look, dict) and 'look' in look and 'matrix_q13' not in look:   # files of earlier tools
            look = dict(look['look'], scores=look.get('scores'))
        if not isinstance(look, dict) or 'matrix_q13' not in look:
            raise Refused(f'{path}: not a look file')
        check_look(look)
    except Refused as exc:
        raise Refused(str(exc) if str(exc).startswith(str(path)) else f'{path}: {exc}')
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, struct.error, binascii.Error) as exc:
        raise Refused(f'{path}: not a valid look file ({exc})')
    return look


def adapt_look(look, slot=None, name=None, no_name=False, icon=None, no_icon=False):
    """A look (e.g. one someone shared) with your own slot, name and icon. The colour data stays as it is."""
    look = dict(look)
    if (look.get('lut') or {}).get('file'):                  # earlier versions kept the maker's folders
        look['lut'] = dict(look['lut'], file=Path(look['lut']['file']).name)
    if slot:
        look['slot'] = slot
    slot = look_slot(look)
    if no_name or name is not None:
        for k in ('name', 'camera_language', 'name_accepted'):
            look.pop(k, None)
    if name:
        look['name'] = name
    if no_icon:
        look.pop('icon', None)
    elif icon:
        look['icon'] = icon_record(icon, slot)
    if look.get('icon') and slot not in SLOT_DATA['icons']['slots']:
        raise Refused(f'{SLOT_NAMES[slot]} cannot take an own icon (its icon is shared with another look); '
                      'add --no-icon')
    return look


def find_lut(look, near):
    """The look's LUT on this computer, if it is there (same file name and contents): next to look.json, in the
    current folder, or where earlier versions recorded it."""
    lut = look.get('lut') or {}
    name = lut.get('file')
    if not name:
        return None
    for cand in (Path(near).parent / Path(name).name, Path(Path(name).name), Path(name)):
        try:
            if cand.is_file() and (not lut.get('sha256') or Lut(cand).sha256 == lut['sha256']):
                return cand
        except (Refused, OSError, ValueError):
            pass                                     # a different or unreadable file of that name
    return None


# ------------------------------------------------------------------ preview
def preview(look, dng, out, lut_path=None, space='srgb'):
    import numpy as np
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise Refused('preview needs pillow: python3 -m pip install rawpy pillow')
    v = photo_signal(dng, max_side=1200)
    flat = v.reshape(-1, 3)
    panels = [neutral(flat)]
    labels = ['camera, no look']
    if lut_path and Path(lut_path).exists():
        panels.append(apply_lut(Lut(lut_path), panels[0], space))
        labels.append('LUT')
    panels.append(render(flat, look))
    labels.append('camera with the look')
    ims = [Image.fromarray((np.clip(p.reshape(v.shape), 0, 1) * 255 + 0.5).astype(np.uint8)) for p in panels]
    w, h = ims[0].size
    sheet = Image.new('RGB', (len(ims) * w + 8 * (len(ims) - 1), h + 28), (24, 24, 24))
    d = ImageDraw.Draw(sheet)
    for k, (im, text) in enumerate(zip(ims, labels)):
        sheet.paste(im, (k * (w + 8), 28))
        d.text((k * (w + 8) + 6, 8), text, fill=(230, 230, 230))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=92)
    return out


# ------------------------------------------------------------------ main
def need_numpy():
    try:
        import numpy  # noqa: F401
    except ImportError:
        raise Refused('this command needs numpy: python3 -m pip install numpy')


def main(argv=None):
    ap = argparse.ArgumentParser(prog='gr3_lutsmith.py', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--version', action='version',
                    version=f'GR3-LUTSmith {VERSION} (checked offline; not yet tested on a real camera)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    def lut_args(p):
        p.add_argument('lut', type=Path, help='.cube file (3D or 1D)')
        p.add_argument('--space', choices=('srgb', 'adobe'), default='srgb',
                       help='colour space the LUT was made for (default srgb)')
        p.add_argument('--photo', type=Path, help='a DNG from your GR III: judges the fit on real content (needs rawpy)')
        p.add_argument('--curve', choices=('auto', 'single', 'pair', 'rgb'), default='auto', help=argparse.SUPPRESS)
        p.add_argument('--no-hue', action='store_true', help='do not use hue sectors (colour changes per hue)')
        p.add_argument('--slot', choices=tuple(SLOT_NAMES), default=DEFAULT_SLOT,
                       help='which Image Control look to replace with yours (default cross-processing)')
        p.add_argument('--camera-language', choices=CAMERA_LANGUAGES, default=system_language(),
                       help='the language your camera is set to (default: this computer\'s language, else English); '
                            'used to check that --name shows')
        p.add_argument('--accept-name', action='store_true', help='use --name even if it may not show with the '
                                                                  'camera language')
        p.add_argument('--name', help='show the slot under this name; plain ASCII shows in every camera language '
                                     '(see `slots` for the longest name each slot takes)')
        p.add_argument('--icon', type=Path, help='a picture for the slot\'s menu icon (needs pillow); the centre of a '
                                                'non-square picture is used')
        p.add_argument('--turns', type=int, default=1,
                       help='how many fits to run; each extra turn starts from a new random point (default 1)')
        p.add_argument('--seed', type=int, help='run turns from this seed (repeats a turn shown earlier)')
        p.add_argument('--jobs', type=int, default=1,
                       help='run this many turns side by side, one processor core each (default 1)')
        p.add_argument('--after-curves', action='store_true',
                       help='also try a colour mix after the tone curves; keeps a second version when it helps')
        p.add_argument('--iters', type=int, default=1500, help=argparse.SUPPRESS)
    m = sub.add_parser('make', help='LUT + official firmware -> firmware with the look')
    lut_args(m)
    m.add_argument('input', type=Path, help='official RICOH firmware file (GR III 2.10 or GR IIIx 1.60)')
    m.add_argument('--force', action='store_true', help='build even if the fit is poor')
    m.add_argument('-o', '--output-dir', type=Path)
    f = sub.add_parser('fit', help='fit a LUT only (writes look.json)')
    lut_args(f)
    f.add_argument('-o', '--output-dir', type=Path)
    b = sub.add_parser('build', help='look.json file(s) + official firmware -> firmware with the look(s)')
    b.add_argument('looks', type=Path, nargs='+', metavar='look.json',
                   help='one look per slot (each look.json says its slot)')
    b.add_argument('input', type=Path, help='official RICOH firmware file (GR III 2.10 or GR IIIx 1.60)')
    b.add_argument('--slot', choices=tuple(SLOT_NAMES), help='put the look on this slot instead of the one its '
                                                              'look.json says (one look only)')
    b.add_argument('--name', help='show the slot under this name instead of the look.json\'s (one look only)')
    b.add_argument('--no-name', action='store_true', help='keep the slot\'s own name (one look only)')
    b.add_argument('--icon', type=Path, help='use this picture as the slot\'s icon (one look only; needs pillow)')
    b.add_argument('--no-icon', action='store_true', help='keep the slot\'s own icon (one look only)')
    b.add_argument('--camera-language', choices=CAMERA_LANGUAGES,
                   help='the language your camera is set to (default: the one saved with the look, else this '
                        'computer\'s)')
    b.add_argument('--accept-name', action='store_true', help='use the looks\' names even if they may not show '
                                                              'with the camera language')
    b.add_argument('-o', '--output-dir', type=Path)
    p = sub.add_parser('preview', help='render a DNG with the look (needs rawpy, pillow)')
    p.add_argument('look', type=Path)
    p.add_argument('photo', type=Path)
    p.add_argument('--lut', type=Path, help='the .cube file, to show it next to the camera (found automatically '
                                            'when it is next to look.json)')
    p.add_argument('-o', '--output', type=Path)
    sub.add_parser('slots', help='list the Image Control looks yours can replace (--slot)')
    c = sub.add_parser('check', help='identify a firmware file and validate its container')
    c.add_argument('input', type=Path)
    s = sub.add_parser('selftest', help='build the bundled example and compare with the verified result')
    s.add_argument('input', type=Path)
    a = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    if sys.version_info < (3, 8):
        print('Python 3.8 or newer is required', file=sys.stderr)
        return 2
    try:
        if a.cmd == 'check':
            return identify(a.input.read_bytes())
        if a.cmd == 'slots':
            list_slots()
            return 0
        print(f'GR3-LUTSmith {VERSION}')
        if a.cmd == 'selftest':
            look = load_look(Path(__file__).resolve().parent / SELFTEST_LOOK)
            source = a.input.read_bytes()
            official = official_of(source)
            if not official:
                make_firmware(source, look)          # raises, naming the files this works on
            out = make_firmware(source, look)
            want = SELFTEST_SHA256.get(official['camera'])
            ok = sha256(out) == want
            print(f'selftest ({official["camera"]} {official["version"]}): '
                  f'{"PASSED" if ok else "FAILED"} (SHA-256 {sha256(out)}'
                  + ('' if want else ', no reference image for this camera') + ')')
            return 0 if ok else 1
        need_numpy()
        if a.cmd == 'preview':
            look = load_look(a.look)
            src = a.lut or find_lut(look, a.look)
            if not src:
                print('LUT not found next to the look; add --lut my_look.cube to compare with it')
            out = preview(look, a.photo, a.output or a.look.with_name('preview.jpg'), src,
                          (look.get('lut') or {}).get('space', 'srgb'))
            print(f'wrote {out}')
            return 0
        if a.cmd in ('make', 'fit'):
            out_dir = a.output_dir or Path('output') / a.lut.stem
            if a.cmd == 'make':
                source = a.input.read_bytes()
                if not official_of(source):
                    make_firmware(source, dict(matrix_q13=[[8192, 0, 0], [0, 8192, 0], [0, 0, 8192]]))
                if (out_dir / official_of(source)['file']).exists() or (out_dir / TURNS_DIR).exists():
                    raise Refused(f'{out_dir} already has results; choose another output directory')
            if a.turns < 1:
                raise Refused('--turns must be at least 1')
            print(f'slot: {slot_line(a.slot)}' + (f', shown as "{a.name}"' if a.name else ''))
            name_accepted = settle_name(a.name, a.slot, a.camera_language, a.accept_name) if a.name else False
            icon = icon_record(a.icon, a.slot) if a.icon else None
            if icon:
                print(f'icon: {a.icon.name} -> {icon["w"]}x{icon["h"]}')
            state = dict(kept=[], history=[], history_turns=[], icon=icon, name_accepted=name_accepted)
            n, post = a.turns, a.after_curves
            while n:
                if a.jobs > 1 and n > 1:
                    batch = Batch(a, state, min(a.jobs, n))
                    batch.add(n, post)
                    batch.run(progress=lambda k, line: line is None and print(f'turn {k} started'))
                else:
                    run_turns(a, n, state, post)
                show_kept(state['kept'])
                n, post = ask_more_turns(a.after_curves)
            kept, best = state['kept'], recommended(state['kept'])
            print(f'\nrecommended: {result_name(best)}')
            describe(best)
            places = [(best, out_dir)]
            if len(kept) > 1:
                places += [(r, out_dir / TURNS_DIR / result_name(r)) for r in kept]
            for r, d in places:
                d.mkdir(parents=True, exist_ok=True)
                (d / 'look.json').write_text(json.dumps(r, indent=1) + '\n')
                if r.get('icon'):
                    icon_image(r['icon']).save(d / 'icon.png')
            if len(state['history']) > 1:
                (out_dir / 'turns.json').write_text(json.dumps(dict(
                    turns=[dict(turn=i + 1, **t) for i, t in enumerate(state['history_turns'])],
                    results=state['history'], kept=[result_name(r) for r in kept],
                    recommended=result_name(best)), indent=1) + '\n')
            print(f'wrote {out_dir / "look.json"}' + (f' (and {len(kept)} kept results in {out_dir / TURNS_DIR})'
                                                     if len(kept) > 1 else ''))
            if a.cmd == 'fit':
                return 0
            build = [(r, d) for r, d in places if r['decision']['build'] or a.force]
            if not build:
                raise Refused('the LUT cannot be reproduced well by the camera (see above); '
                              'use --force to build anyway')
            for r, d in places:
                if not (r['decision']['build'] or a.force):
                    print(f'not built: {d} (the camera cannot reproduce this result well; --force builds it)')
            made = {}
            for r, d in build:
                if id(r) not in made:                          # the recommended result is also in turns/
                    made[id(r)] = make_firmware(source, r)
                data = made[id(r)]
                target = write_atomic(data, d, official_of(source)['file'])
                print(f'wrote {target}  (SHA-256 {sha256(data)})')
            print(('Copy one of these files' if len(build) > 1 else 'Copy this file')
                  + ' to the root of the SD card and run the camera\'s firmware update.\n'
                  + f'Then choose {a.name or SLOT_NAMES[a.slot]} in Image Control: your look is there.')
            print(STATUS)
            return 0
        if a.cmd == 'build':
            looks = [load_look(x) for x in a.looks]
            adapt = a.slot or a.name is not None or a.no_name or a.icon or a.no_icon
            if adapt and len(looks) > 1:
                raise Refused('--slot, --name and --icon change one look; build each look alone with them first '
                              '(each gets its own look.json), then build those together')
            if a.name is not None and a.no_name or a.icon and a.no_icon:
                raise Refused('choose either --name or --no-name, and either --icon or --no-icon')
            if adapt:
                looks = [adapt_look(looks[0], a.slot, a.name, a.no_name, a.icon, a.no_icon)]
            for x, lk in zip(a.looks, looks):
                print(f'{x}: {slot_line(look_slot(lk))}' + (f', shown as "{lk["name"]}"' if lk.get('name') else '')
                      + (', own icon' if lk.get('icon') else ''))
                try:
                    describe(lk)
                except (KeyError, TypeError, AttributeError):
                    pass                                     # a look file without its fit results
                if lk.get('name'):
                    lang = a.camera_language or lk.get('camera_language') or system_language()
                    saved = lk.get('name_accepted') and lang == lk.get('camera_language')
                    yes = settle_name(lk['name'], look_slot(lk), lang, a.accept_name or saved,
                                      log=lambda x: print('  ' + x))
                    if adapt:                                # saved with the adapted look
                        lk['camera_language'] = lang
                        lk.pop('name_accepted', None)
                        if yes:
                            lk['name_accepted'] = True
            title = (looks[0].get('lut') or {}).get('title') or a.looks[0].stem
            out_dir = a.output_dir or (Path('output') / title if adapt else a.looks[0].parent)
            if adapt:
                if (out_dir / 'look.json').resolve() == a.looks[0].resolve():
                    raise Refused('that would replace the look.json you started from; choose another folder with -o')
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / 'look.json').write_text(json.dumps(looks[0], indent=1) + '\n')
                if looks[0].get('icon'):
                    icon_image(looks[0]['icon']).save(out_dir / 'icon.png')
                print(f'wrote {out_dir / "look.json"} (the look with your slot, name and icon)')
            source = a.input.read_bytes()
            data = make_firmware(source, looks)
            target = write_atomic(data, out_dir, official_of(source)['file'])
            print(f'wrote {target}  (SHA-256 {sha256(data)})')
            shown = {look_slot(lk): lk.get('name') or SLOT_NAMES[look_slot(lk)] for lk in looks}
            names = [shown[n] for n in SLOT_ORDER if n in shown]
            print('Copy this file to the root of the SD card and run the camera\'s firmware update.\n'
                  + ('Then choose ' + names[0] + ' in Image Control: your look is there.' if len(names) == 1 else
                     'Your looks are on: ' + ', '.join(names) + '.'))
            print(STATUS)
            return 0
    except Refused as exc:
        print(f'refused: {exc}', file=sys.stderr)
        print('nothing was written.' if a.cmd in ('make', 'build', 'selftest') else '', file=sys.stderr)
        return 1
    except OSError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
