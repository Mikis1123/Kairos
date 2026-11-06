"""QR Code encoder (ISO/IEC 18004), byte mode, versions 1-40, no dependencies.

The iPhone pairing sheet (phone_pairing.py) shows the `kairos://pair?...` link as a QR code. One
link is about 160 bytes, which fits version 8-10 at level M. This module keeps the desktop free
of a QR package and its build-bundling step. It follows the reference algorithm: byte segment,
Reed-Solomon over GF(256) with polynomial 0x11D, block interleaving, function patterns, zig-zag
placement, and the lowest-penalty of the 8 masks.

    matrix = qr_code.encode("kairos://pair?...")   # list of rows, True = dark module
    image = qr_code.to_qimage(matrix, scale=6)     # optional Qt helper, 4-module quiet zone

test_ios_pairing.py decodes the output with OpenCV's QRCodeDetector across versions 1-40.
"""

ECC_LOW, ECC_MEDIUM, ECC_QUARTILE, ECC_HIGH = 0, 1, 2, 3
_FORMAT_BITS = {ECC_LOW: 1, ECC_MEDIUM: 0, ECC_QUARTILE: 3, ECC_HIGH: 2}

# Rows: ECC level L, M, Q, H. Columns: version 0 (unused) to 40.
_ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)
_NUM_ECC_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)

QUIET_ZONE = 4


class DataTooLong(ValueError):
    pass


# ── Reed-Solomon over GF(256) ────────────────────────────────────────────────

def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = ((z << 1) ^ ((z >> 7) * 0x11D)) & 0xFF
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


# ── capacity ─────────────────────────────────────────────────────────────────

def _raw_data_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver, ecl):
    return _raw_data_modules(ver) // 8 - _ECC_CODEWORDS_PER_BLOCK[ecl][ver] * _NUM_ECC_BLOCKS[ecl][ver]


def _count_bits(ver):
    return 8 if ver <= 9 else 16                      # byte mode


def _alignment_positions(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
    return [6] + sorted(size - 7 - i * step for i in range(n - 1))


def capacity(ver, ecl=ECC_MEDIUM):
    """Bytes that fit a byte-mode symbol of this version and level."""
    return (_data_codewords(ver, ecl) * 8 - 4 - _count_bits(ver)) // 8


# ── symbol ───────────────────────────────────────────────────────────────────

class _Symbol:
    def __init__(self, ver):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x, y, dark):
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def draw_function_patterns(self, ecl):
        n = self.size
        for i in range(n):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver)
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue                          # finder corners
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(ecl, 0)                      # reserve; redrawn with the real mask
        self.draw_version()

    def draw_format(self, ecl, mask):
        data = _FORMAT_BITS[ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412

        def bit(i):
            return (bits >> i) & 1 != 0

        n = self.size
        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, n - 15 + i, bit(i))
        self.set_fn(8, n - 8, True)                   # the dark module

    def draw_version(self):
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.ver << 12 | rem
        for i in range(18):
            dark = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self.set_fn(a, b, dark)
            self.set_fn(b, a, dark)

    def draw_codewords(self, data):
        n = self.size
        i = 0
        total = len(data) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5                             # skip the vertical timing column
            upward = ((right + 1) & 2) == 0
            for vert in range(n):
                y = n - 1 - vert if upward else vert
                for j in range(2):
                    x = right - j
                    if not self.fn[y][x] and i < total:
                        self.mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        f = _MASKS[mask]
        for y in range(self.size):
            row, fn = self.mod[y], self.fn[y]
            for x in range(self.size):
                if not fn[x] and f(x, y):
                    row[x] = not row[x]

    def penalty(self):
        n, m = self.size, self.mod
        score = 0
        lines = [row for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]
        finder_a = [True, False, True, True, True, False, True, False, False, False, False]
        finder_b = finder_a[::-1]
        for line in lines:
            run, prev = 0, None
            for v in line:                            # rule 1: runs of 5+ same colour
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, v
            if run >= 5:
                score += run - 2
            for k in range(n - 10):                   # rule 3: finder-like 1:1:3:1:1 + 4 light
                seg = line[k:k + 11]
                if seg == finder_a or seg == finder_b:
                    score += 40
        for y in range(n - 1):                        # rule 2: 2x2 blocks
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)             # rule 4: dark balance
        total = n * n
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        score += max(0, k) * 10
        return score


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _codewords(payload, ver, ecl):
    cap_bits = _data_codewords(ver, ecl) * 8
    bits = []

    def put(value, length):
        bits.extend((value >> i) & 1 for i in range(length - 1, -1, -1))

    put(0b0100, 4)                                    # byte mode
    put(len(payload), _count_bits(ver))
    for b in payload:
        put(b, 8)
    put(0, min(4, cap_bits - len(bits)))              # terminator
    put(0, (-len(bits)) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < cap_bits // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    # split into blocks, append ECC, interleave
    nblocks = _NUM_ECC_BLOCKS[ecl][ver]
    ecc_len = _ECC_CODEWORDS_PER_BLOCK[ecl][ver]
    raw = _raw_data_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    short_len = raw // nblocks
    div = _rs_divisor(ecc_len)
    blocks, k = [], 0
    for i in range(nblocks):
        dat = data[k:k + short_len - ecc_len + (0 if i < nshort else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, div)
        if i < nshort:
            dat = dat + [0]
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= nshort:
                out.append(blk[i])
    return out


def encode(text, ecl=ECC_MEDIUM, min_version=1, mask=None):
    """Smallest symbol holding `text` (UTF-8) as rows of booleans (True = dark), no quiet zone."""
    payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    for ver in range(max(1, min_version), 41):
        if len(payload) <= capacity(ver, ecl):
            break
    else:
        raise DataTooLong(f"{len(payload)} bytes do not fit a QR code")
    sym = _Symbol(ver)
    sym.draw_function_patterns(ecl)
    sym.draw_codewords(_codewords(payload, ver, ecl))
    if mask is None:
        best = None
        for m in range(8):
            sym.apply_mask(m)
            sym.draw_format(ecl, m)
            p = sym.penalty()
            if best is None or p < best[0]:
                best = (p, m)
            sym.apply_mask(m)                         # XOR undoes it
        mask = best[1]
    sym.apply_mask(mask)
    sym.draw_format(ecl, mask)
    return sym.mod


def version_of(matrix):
    return (len(matrix) - 17) // 4


def to_qimage(matrix, scale=6, dark=0xFF000000, light=0xFFFFFFFF, quiet=QUIET_ZONE):
    """Crisp QImage (ARGB32): `scale` px per module, `quiet` light modules around the symbol."""
    from PyQt6.QtGui import QImage
    n = len(matrix)
    side = (n + 2 * quiet) * scale
    img = QImage(side, side, QImage.Format.Format_ARGB32)
    img.fill(light)
    from PyQt6.QtGui import QColor, QPainter
    p = QPainter(img)
    p.setPen(QColor.fromRgba(0))
    color = QColor.fromRgba(dark)
    for y, row in enumerate(matrix):
        for x, v in enumerate(row):
            if v:
                p.fillRect((x + quiet) * scale, (y + quiet) * scale, scale, scale, color)
    p.end()
    return img
