"""Minimal QR code generator producing SVG strings. Pure Python, no dependencies."""


def generate_qr_svg(data: str, module_size: int = 8, margin: int = 4) -> str:
    """Generate a QR code as an SVG string.

    Args:
        data: The string to encode (up to ~100 chars, byte mode).
        module_size: Pixel size of each module (dark/light square).
        margin: Number of quiet-zone modules around the code.

    Returns:
        An SVG element string.
    """
    matrix = _encode_qr(data)
    size = len(matrix)
    full = size + 2 * margin
    px = full * module_size
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {full} {full}" '
             f'width="{px}" height="{px}" shape-rendering="crispEdges">',
             f'<rect width="{full}" height="{full}" fill="#fff"/>']
    for y, row in enumerate(matrix):
        for x, val in enumerate(row):
            if val:
                parts.append(f'<rect x="{x + margin}" y="{y + margin}" '
                             f'width="1" height="1" fill="#000"/>')
    parts.append('</svg>')
    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# QR internals
# ---------------------------------------------------------------------------

# Error correction level M constants per version (1-indexed, index 0 unused).
# Each entry: (total_codewords, ec_codewords_per_block, num_blocks_group1,
#               data_cw_per_block_g1, num_blocks_group2, data_cw_per_block_g2)
_EC_TABLE = [
    None,
    # V1  total=26  ec_per_block=10  1 block 16 data
    (26, 10, 1, 16, 0, 0),
    # V2  total=44  ec=16  1 block 28
    (44, 16, 1, 28, 0, 0),
    # V3  total=70  ec=26  1 block 44
    (70, 26, 1, 44, 0, 0),
    # V4  total=100  ec=18  2 blocks 32
    (100, 18, 2, 32, 0, 0),
    # V5  total=134  ec=24  2 blocks 43
    (134, 24, 2, 43, 0, 0),
    # V6  total=172  ec=16  4 blocks 27
    (172, 16, 4, 27, 0, 0),
    # V7  total=196  ec=18  4 blocks 31
    (196, 18, 4, 31, 0, 0),
    # V8  total=242  ec=22  2 blocks 38, 2 blocks 39
    (242, 22, 2, 38, 2, 39),
    # V9  total=292  ec=22  3 blocks 36, 2 blocks 37
    (292, 22, 3, 36, 2, 37),
    # V10 total=346  ec=26  4 blocks 43, 1 block 44
    (346, 26, 4, 43, 1, 44),
]

# Data capacity in *bytes* (byte mode) for each version at EC level M.
_BYTE_CAPACITY = [0, 14, 26, 42, 62, 84, 106, 122, 152, 180, 213]

# Alignment pattern center positions per version (2-40, only need up to 10).
_ALIGN_POS = [
    None,
    [],            # V1
    [6, 18],       # V2
    [6, 22],       # V3
    [6, 26],       # V4
    [6, 30],       # V5
    [6, 34],       # V6
    [6, 22, 38],   # V7
    [6, 24, 42],   # V8
    [6, 26, 46],   # V9
    [6, 28, 50],   # V10
]

_FORMAT_BITS = [
    # Mask 0-7 for EC level M (binary 00)
    0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0,
]


def _pick_version(data_len: int) -> int:
    for v in range(1, len(_BYTE_CAPACITY)):
        if _BYTE_CAPACITY[v] >= data_len:
            return v
    raise ValueError(f"Data too long ({data_len} bytes) for supported versions")


def _encode_data(data: bytes, version: int) -> list[int]:
    """Encode data into codeword bytes (byte mode)."""
    ec = _EC_TABLE[version]
    total_data_cw = ec[2] * ec[3] + ec[4] * ec[5]
    bits: list[int] = []

    def add(val: int, length: int):
        for i in range(length - 1, -1, -1):
            bits.append((val >> i) & 1)

    # Mode indicator: byte = 0100
    add(0b0100, 4)
    # Character count (8 bits for V1-9, 16 bits for V10+)
    cc_bits = 16 if version >= 10 else 8
    add(len(data), cc_bits)
    for b in data:
        add(b, 8)
    # Terminator (up to 4 zeros)
    remaining = total_data_cw * 8 - len(bits)
    add(0, min(4, remaining))
    # Pad to byte boundary
    while len(bits) % 8:
        bits.append(0)
    # Pad codewords
    pad = [0xEC, 0x11]
    idx = 0
    while len(bits) < total_data_cw * 8:
        add(pad[idx % 2], 8)
        idx += 1

    codewords = []
    for i in range(0, len(bits), 8):
        codewords.append(int(''.join(str(b) for b in bits[i:i+8]), 2))
    return codewords[:total_data_cw]


# GF(256) arithmetic for Reed-Solomon
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256

def _init_gf():
    x = 1
    for i in range(255):
        _GF_EXP[i] = x
        _GF_LOG[x] = i
        x <<= 1
        if x >= 256:
            x ^= 0x11D
    for i in range(255, 512):
        _GF_EXP[i] = _GF_EXP[i - 255]

_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_encode(data: list[int], nsym: int) -> list[int]:
    """Compute Reed-Solomon EC codewords."""
    gen = [1]
    for i in range(nsym):
        new = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            new[j] ^= g
            new[j + 1] ^= _gf_mul(g, _GF_EXP[i])
        gen = new

    feedback = [0] * (len(data) + nsym)
    feedback[:len(data)] = data
    for i in range(len(data)):
        coef = feedback[i]
        if coef != 0:
            for j in range(1, len(gen)):
                feedback[i + j] ^= _gf_mul(gen[j], coef)
    return feedback[len(data):]


def _interleave(data_cw: list[int], version: int) -> list[int]:
    """Split into blocks, compute EC, and interleave."""
    ec = _EC_TABLE[version]
    _, ec_per, ng1, dcw1, ng2, dcw2 = ec

    blocks_data: list[list[int]] = []
    pos = 0
    for _ in range(ng1):
        blocks_data.append(data_cw[pos:pos + dcw1])
        pos += dcw1
    for _ in range(ng2):
        blocks_data.append(data_cw[pos:pos + dcw2])
        pos += dcw2

    blocks_ec = [_rs_encode(b, ec_per) for b in blocks_data]

    result: list[int] = []
    max_data = max(len(b) for b in blocks_data)
    for i in range(max_data):
        for b in blocks_data:
            if i < len(b):
                result.append(b[i])
    for i in range(ec_per):
        for b in blocks_ec:
            if i < len(b):
                result.append(b[i])
    return result


def _make_matrix(version: int) -> list[list[int | None]]:
    """Create empty matrix (None = not yet set)."""
    size = 17 + version * 4
    return [[None] * size for _ in range(size)]


def _place_finder(matrix, row, col):
    """Place a 7x7 finder pattern + separator."""
    size = len(matrix)
    for r in range(-1, 8):
        for c in range(-1, 8):
            rr, cc = row + r, col + c
            if 0 <= rr < size and 0 <= cc < size:
                if 0 <= r <= 6 and 0 <= c <= 6:
                    if (r in (0, 6) or c in (0, 6) or
                            (2 <= r <= 4 and 2 <= c <= 4)):
                        matrix[rr][cc] = 1
                    else:
                        matrix[rr][cc] = 0
                else:
                    matrix[rr][cc] = 0


def _place_alignment(matrix, version):
    """Place alignment patterns."""
    positions = _ALIGN_POS[version]
    if not positions:
        return
    coords = []
    for r in positions:
        for c in positions:
            coords.append((r, c))
    for r, c in coords:
        # Skip if overlapping finder patterns
        if matrix[r][c] is not None:
            continue
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                if abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0):
                    matrix[r + dr][c + dc] = 1
                else:
                    matrix[r + dr][c + dc] = 0


def _place_timing(matrix):
    """Place timing patterns."""
    size = len(matrix)
    for i in range(8, size - 8):
        if matrix[6][i] is None:
            matrix[6][i] = 1 if i % 2 == 0 else 0
        if matrix[i][6] is None:
            matrix[i][6] = 1 if i % 2 == 0 else 0


def _reserve_format(matrix):
    """Reserve format info areas (set to 0 temporarily)."""
    size = len(matrix)
    for i in range(9):
        if matrix[8][i] is None:
            matrix[8][i] = 0
        if matrix[i][8] is None:
            matrix[i][8] = 0
    for i in range(8):
        if matrix[8][size - 1 - i] is None:
            matrix[8][size - 1 - i] = 0
        if matrix[size - 1 - i][8] is None:
            matrix[size - 1 - i][8] = 0
    # Dark module
    matrix[size - 8][8] = 1


def _place_version_info(matrix, version):
    """Place version info for V7+."""
    if version < 7:
        return
    # Version info bits (18-bit) - precomputed for V7-10
    _VERSION_BITS = {
        7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3,
    }
    info = _VERSION_BITS.get(version)
    if info is None:
        return
    size = len(matrix)
    for i in range(6):
        for j in range(3):
            bit = (info >> (i * 3 + j)) & 1
            matrix[size - 11 + j][i] = bit
            matrix[i][size - 11 + j] = bit


def _place_data(matrix, bits: list[int]):
    """Place data bits in the matrix using the upward/downward zigzag."""
    size = len(matrix)
    bit_idx = 0
    # Columns go right-to-left in pairs
    col = size - 1
    while col >= 0:
        if col == 6:  # Skip timing column
            col -= 1
            continue
        # Determine direction
        # Even-numbered column-pair from right: up; odd: down
        # Actually the direction alternates starting upward
        pair_num = (size - 1 - col) // 2
        going_up = pair_num % 2 == 0

        rows = range(size - 1, -1, -1) if going_up else range(size)
        for row in rows:
            for dc in (0, -1):
                c = col + dc
                if c < 0:
                    continue
                if matrix[row][c] is None:
                    if bit_idx < len(bits):
                        matrix[row][c] = bits[bit_idx]
                    else:
                        matrix[row][c] = 0
                    bit_idx += 1
        col -= 2


def _apply_mask(matrix, mask_id: int) -> list[list[int]]:
    """Apply a mask pattern and return new matrix."""
    size = len(matrix)
    result = [row[:] for row in matrix]
    for r in range(size):
        for c in range(size):
            if _is_function_module(matrix, r, c, size):
                continue
            if _mask_fn(mask_id, r, c):
                result[r][c] ^= 1
    return result


def _is_function_module(matrix, r, c, size):
    """Quick check if (r,c) is a function pattern (not data).
    We check by seeing if it was set before data placement.
    Since we use a 'reserved' approach, we track function modules separately."""
    # This is called on the matrix that already has everything placed,
    # so we need to use the stored function module map.
    # We'll handle this differently - see _encode_qr.
    pass  # Placeholder - actual logic in _encode_qr


def _mask_fn(mask_id: int, r: int, c: int) -> bool:
    if mask_id == 0: return (r + c) % 2 == 0
    if mask_id == 1: return r % 2 == 0
    if mask_id == 2: return c % 3 == 0
    if mask_id == 3: return (r + c) % 3 == 0
    if mask_id == 4: return (r // 2 + c // 3) % 2 == 0
    if mask_id == 5: return (r * c) % 2 + (r * c) % 3 == 0
    if mask_id == 6: return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    if mask_id == 7: return ((r + c) % 2 + (r * c) % 3) % 2 == 0
    return False


def _place_format_info(matrix, mask_id: int):
    """Write the 15-bit format info into the matrix."""
    size = len(matrix)
    bits = _FORMAT_BITS[mask_id]

    # First copy: around top-left finder
    # Bits 0-5 go in row 8, columns 0-5 (skip col 6 = timing)
    # Bit 6 goes in row 8, column 7
    # Bit 7 goes in row 8, column 8
    # Bit 8 goes in row 7, column 8
    # Bits 9-14 go in rows 5 down to 0, column 8
    seq1 = [
        (8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5),  # bits 0-5
        (8, 7), (8, 8),  # bits 6-7
        (7, 8),  # bit 8
        (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8),  # bits 9-14
    ]

    # Second copy:
    # Bits 0-6 go in column 8, rows (size-1) down to (size-7)
    # Bits 7-14 go in row 8, columns (size-8) to (size-1)
    seq2 = [
        (size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8),
        (size - 5, 8), (size - 6, 8), (size - 7, 8),  # bits 0-6
        (8, size - 8), (8, size - 7), (8, size - 6), (8, size - 5),
        (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1),  # bits 7-14
    ]

    for i in range(15):
        bit = (bits >> (14 - i)) & 1
        r, c = seq1[i]
        matrix[r][c] = bit
        r, c = seq2[i]
        matrix[r][c] = bit


def _penalty(matrix) -> int:
    """Calculate penalty score for mask selection."""
    size = len(matrix)
    score = 0

    # Rule 1: runs of same color in row/col
    for r in range(size):
        run = 1
        for c in range(1, size):
            if matrix[r][c] == matrix[r][c - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2

    for c in range(size):
        run = 1
        for r in range(1, size):
            if matrix[r][c] == matrix[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2

    # Rule 2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            v = matrix[r][c]
            if v == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3

    # Rule 3: finder-like patterns
    pat1 = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    pat2 = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
    for r in range(size):
        for c in range(size - 10):
            if [matrix[r][c + i] for i in range(11)] in (pat1, pat2):
                score += 40
    for c in range(size):
        for r in range(size - 10):
            if [matrix[r + i][c] for i in range(11)] in (pat1, pat2):
                score += 40

    # Rule 4: proportion of dark modules
    dark = sum(sum(row) for row in matrix)
    total = size * size
    pct = dark * 100 // total
    prev5 = abs(pct - pct % 5 - 50) // 5
    next5 = abs(pct - pct % 5 + 5 - 50) // 5
    score += min(prev5, next5) * 10

    return score


def _encode_qr(data: str) -> list[list[int]]:
    """Full QR encoding pipeline, returns the final bit matrix."""
    data_bytes = data.encode('utf-8')
    version = _pick_version(len(data_bytes))
    size = 17 + version * 4

    # 1. Encode data into codewords
    data_cw = _encode_data(data_bytes, version)

    # 2. Interleave data + EC
    final_cw = _interleave(data_cw, version)

    # 3. Convert to bit stream
    bits = []
    for cw in final_cw:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    # Remainder bits (V2-6: 7 bits, V7+: 0 bits... actually depends on version)
    remainder_counts = [0, 0, 7, 7, 7, 7, 7, 0, 0, 0, 0]
    bits.extend([0] * remainder_counts[version])

    # 4. Build the matrix - place function patterns
    matrix = _make_matrix(version)
    _place_finder(matrix, 0, 0)
    _place_finder(matrix, 0, size - 7)
    _place_finder(matrix, size - 7, 0)
    _place_alignment(matrix, version)
    _place_timing(matrix)
    _reserve_format(matrix)
    _place_version_info(matrix, version)

    # Record which cells are function patterns
    func_map = [[matrix[r][c] is not None for c in range(size)] for r in range(size)]

    # 5. Place data
    _place_data(matrix, bits)

    # 6. Try all masks, pick the best
    best_mask = 0
    best_score = float('inf')
    for mask_id in range(8):
        candidate = [row[:] for row in matrix]
        # Apply mask only to data modules
        for r in range(size):
            for c in range(size):
                if not func_map[r][c] and _mask_fn(mask_id, r, c):
                    candidate[r][c] ^= 1
        _place_format_info(candidate, mask_id)
        s = _penalty(candidate)
        if s < best_score:
            best_score = s
            best_mask = mask_id

    # Apply best mask
    for r in range(size):
        for c in range(size):
            if not func_map[r][c] and _mask_fn(best_mask, r, c):
                matrix[r][c] ^= 1
    _place_format_info(matrix, best_mask)

    return matrix
