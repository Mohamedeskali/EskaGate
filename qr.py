"""
Minimal QR code encoder (standard library only) for the phone-access link.

Byte mode, error correction level M, versions 1-10 (up to 213 bytes), best of
the 8 masks. Follows the ISO/IEC 18004 layout; the structure mirrors Project
Nayuki's reference encoder. Output is an SVG string.
"""

# Level M, per version: (EC codewords per block, [(block count, data codewords per block), ...])
_EC_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_FORMAT_M = 0          # level M's 2-bit indicator


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
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


def _data_codewords(data, version):
    ec_len, groups = _EC_M[version]
    capacity = sum(n * k for n, k in groups)
    bits = [0, 1, 0, 0]                                   # byte mode
    count_bits = 8 if version < 10 else 16
    bits += [(len(data) >> i) & 1 for i in reversed(range(count_bits))]
    for b in data:
        bits += [(b >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, capacity * 8 - len(bits))        # terminator
    bits += [0] * (-len(bits) % 8)
    out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(out) < capacity:
        out.append(pad)
        pad ^= 0xEC ^ 0x11
    # Split into blocks, add error correction, interleave.
    blocks, pos = [], 0
    divisor = _rs_divisor(ec_len)
    for n, k in groups:
        for _ in range(n):
            d = out[pos:pos + k]
            pos += k
            blocks.append((d, _rs_remainder(d, divisor)))
    result = []
    for i in range(max(len(d) for d, _ in blocks)):
        result += [d[i] for d, _ in blocks if i < len(d)]
    for i in range(ec_len):
        result += [e[i] for _, e in blocks]
    return result


class _Matrix:
    def __init__(self, version):
        self.version = version
        self.size = size = version * 4 + 17
        self.mod = [[False] * size for _ in range(size)]
        self.fn = [[False] * size for _ in range(size)]
        for i in range(size):                             # timing
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for x, y in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    xx, yy = x + dx, y + dy
                    if 0 <= xx < size and 0 <= yy < size:
                        self.set_fn(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[version]
        last = len(pos) - 1
        for i, a in enumerate(pos):
            for j, b in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        if version >= 7:
            rem = version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = version << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self.set_fn(a, b, bit)
                self.set_fn(b, a, bit)

    def set_fn(self, x, y, dark):
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def draw_format(self, mask):
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1
        size = self.size
        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, size - 15 + i, bit(i))
        self.set_fn(8, size - 8, True)                    # dark module

    def draw_codewords(self, data):
        size, i, total = self.size, 0, len(data) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < total:
                        self.mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        f = (lambda y, x: (x + y) % 2 == 0, lambda y, x: y % 2 == 0, lambda y, x: x % 3 == 0,
             lambda y, x: (x + y) % 3 == 0, lambda y, x: (x // 3 + y // 2) % 2 == 0,
             lambda y, x: x * y % 2 + x * y % 3 == 0, lambda y, x: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda y, x: ((x + y) % 2 + x * y % 3) % 2 == 0)[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(y, x):
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self):
        m, size, score = self.mod, self.size, 0
        lines = [row for row in m] + [[m[y][x] for y in range(size)] for x in range(size)]
        pat1 = [True, False, True, True, True, False, True, False, False, False, False]
        pat2 = pat1[::-1]
        for line in lines:
            run = 1
            for i in range(1, size + 1):
                if i < size and line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            for i in range(size - 10):
                seg = line[i:i + 11]
                if seg == pat1 or seg == pat2:
                    score += 40
        for y in range(size - 1):
            for x in range(size - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = size * size
        score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
        return score


def encode(text):
    """Returns the module matrix (list of rows of bools) for `text`."""
    data = text.encode("utf-8")
    version = next((v for v in range(1, 11)
                    if 4 + (8 if v < 10 else 16) + len(data) * 8 <= sum(n * k for n, k in _EC_M[v][1]) * 8), None)
    if version is None:
        raise ValueError("Text too long for a QR code here.")
    codewords = _data_codewords(data, version)
    best, best_score = None, None
    for mask in range(8):
        mx = _Matrix(version)
        mx.draw_codewords(codewords)
        mx.apply_mask(mask)
        mx.draw_format(mask)
        score = mx.penalty()
        if best_score is None or score < best_score:
            best, best_score = mx, score
    return best.mod


def svg(text, border=4):
    """QR code as an SVG string: dark modules on a white background (scanners need the contrast)."""
    mod = encode(text)
    n = len(mod) + border * 2
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(mod)
                   for x, dark in enumerate(row) if dark)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
