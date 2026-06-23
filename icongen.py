#!/usr/bin/env python3
"""Generate modern 3D-style PWA icons for ClassRally. Zero dependencies."""

import math
import struct
import zlib
from pathlib import Path


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    chunk = chunk_type + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


def save_png(path: str, pixels: list[list[tuple[int, int, int, int]]], width: int, height: int) -> None:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    raw = b""
    for row in pixels:
        raw += b"\x00"
        for r, g, b, a in row:
            raw += struct.pack("BBBB", r, g, b, a)
    idat = _png_chunk(b"IDAT", zlib.compress(raw, 9))
    iend = _png_chunk(b"IEND", b"")
    Path(path).write_bytes(sig + ihdr + idat + iend)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_color(c1: tuple, c2: tuple, t: float) -> tuple:
    return tuple(int(lerp(c1[i], c2[i], min(1, max(0, t)))) for i in range(len(c1)))


def ease_in_out(t: float) -> float:
    return t * t * (3 - 2 * t)


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def dist_to_polygon_edge(x: float, y: float, poly: list[tuple[float, float]]) -> float:
    """Minimum distance from point to polygon edges."""
    min_d = float("inf")
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq == 0:
            d = math.sqrt((x - x1) ** 2 + (y - y1) ** 2)
        else:
            t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / seg_len_sq))
            px, py = x1 + t * dx, y1 + t * dy
            d = math.sqrt((x - px) ** 2 + (y - py) ** 2)
        min_d = min(min_d, d)
    return min_d


def generate_icon(size: int) -> list[list[tuple[int, int, int, int]]]:
    s = size
    pixels = [[None] * s for _ in range(s)]

    # Colors
    bg_top = (7, 20, 50)       # Deep navy
    bg_mid = (2, 100, 180)     # Rich blue
    bg_bot = (6, 182, 212)     # Cyan
    accent = (250, 204, 21)    # Gold/yellow
    white = (255, 255, 255)

    # Corner radius (relative)
    radius = s * 0.22
    margin = s * 0.02  # Outer padding for shadow

    # Lightning bolt polygon (centered, normalized 0-1)
    bolt_raw = [
        (0.42, 0.15),
        (0.28, 0.52),
        (0.44, 0.52),
        (0.35, 0.85),
        (0.72, 0.42),
        (0.52, 0.42),
        (0.62, 0.15),
    ]

    # Scale bolt to icon
    bolt_margin = s * 0.12
    bolt = [(x * (s - 2 * bolt_margin) + bolt_margin, y * (s - 2 * bolt_margin) + bolt_margin) for x, y in bolt_raw]

    # Shadow bolt (offset down-right)
    shadow_off = s * 0.02
    bolt_shadow = [(x + shadow_off, y + shadow_off) for x, y in bolt]

    # Glow bolt (slightly larger)
    cx = sum(p[0] for p in bolt) / len(bolt)
    cy = sum(p[1] for p in bolt) / len(bolt)
    glow_scale = 1.08
    bolt_glow = [(cx + (x - cx) * glow_scale, cy + (y - cy) * glow_scale) for x, y in bolt]

    # Trophy cup polygon (small, at bottom)
    trophy_w = s * 0.16
    trophy_h = s * 0.08
    trophy_cx = s * 0.5
    trophy_cy = s * 0.82
    trophy = [
        (trophy_cx - trophy_w / 2, trophy_cy - trophy_h / 2),
        (trophy_cx + trophy_w / 2, trophy_cy - trophy_h / 2),
        (trophy_cx + trophy_w / 3, trophy_cy + trophy_h / 2),
        (trophy_cx - trophy_w / 3, trophy_cy + trophy_h / 2),
    ]

    # Star sparkle positions
    sparkles = [
        (s * 0.18, s * 0.22, s * 0.035),
        (s * 0.82, s * 0.18, s * 0.025),
        (s * 0.78, s * 0.72, s * 0.03),
        (s * 0.15, s * 0.65, s * 0.02),
    ]

    aa = 1.5  # Anti-alias width in pixels

    for y in range(s):
        for x in range(s):
            # --- Rounded rectangle mask ---
            # Distance from nearest edge considering rounded corners
            in_rect = True
            rect_alpha = 1.0

            # Check corners
            corners = [
                (margin + radius, margin + radius),           # top-left
                (s - margin - radius, margin + radius),       # top-right
                (margin + radius, s - margin - radius),       # bottom-left
                (s - margin - radius, s - margin - radius),   # bottom-right
            ]

            px, py = x + 0.5, y + 0.5  # Pixel center

            if px < margin or px > s - margin or py < margin or py > s - margin:
                in_rect = False
                rect_alpha = 0.0
            else:
                # Corner rounding
                for cx_c, cy_c in corners:
                    dx = abs(px - cx_c)
                    dy = abs(py - cy_c)
                    is_corner_region = (
                        (cx_c < s / 2 and px < cx_c and py < cy_c if cy_c < s / 2 else False) or
                        (cx_c > s / 2 and px > cx_c and py < cy_c if cy_c < s / 2 else False) or
                        (cx_c < s / 2 and px < cx_c and py > cy_c if cy_c > s / 2 else False) or
                        (cx_c > s / 2 and px > cx_c and py > cy_c if cy_c > s / 2 else False)
                    )
                    if is_corner_region:
                        d = math.sqrt(dx * dx + dy * dy)
                        if d > radius + aa:
                            in_rect = False
                            rect_alpha = 0.0
                        elif d > radius:
                            rect_alpha = 1.0 - (d - radius) / aa
                        break

            if not in_rect and rect_alpha <= 0:
                pixels[y][x] = (0, 0, 0, 0)
                continue

            # --- Background gradient (diagonal, top-left to bottom-right) ---
            t = (px + py) / (2 * s)
            t = ease_in_out(t)
            if t < 0.5:
                bg = lerp_color(bg_top, bg_mid, t * 2)
            else:
                bg = lerp_color(bg_mid, bg_bot, (t - 0.5) * 2)

            # Subtle radial highlight (glassmorphism effect)
            highlight_cx, highlight_cy = s * 0.35, s * 0.3
            hd = math.sqrt((px - highlight_cx) ** 2 + (py - highlight_cy) ** 2)
            highlight_r = s * 0.45
            if hd < highlight_r:
                ht = 1.0 - hd / highlight_r
                ht = ht * ht * 0.25  # Subtle
                bg = lerp_color(bg, (180, 220, 255), ht)

            # Inner edge highlight (top/left brighter, bottom/right darker for 3D)
            edge_w = s * 0.04
            # Top edge highlight
            if py < margin + edge_w:
                et = 1.0 - (py - margin) / edge_w
                bg = lerp_color(bg, (255, 255, 255), et * 0.15)
            # Left edge highlight
            if px < margin + edge_w:
                et = 1.0 - (px - margin) / edge_w
                bg = lerp_color(bg, (255, 255, 255), et * 0.1)
            # Bottom edge shadow
            if py > s - margin - edge_w:
                et = 1.0 - (s - margin - py) / edge_w
                bg = lerp_color(bg, (0, 0, 0), et * 0.2)
            # Right edge shadow
            if px > s - margin - edge_w:
                et = 1.0 - (s - margin - px) / edge_w
                bg = lerp_color(bg, (0, 0, 0), et * 0.15)

            r, g, b = bg

            # --- Shadow layer ---
            if point_in_polygon(px, py, bolt_shadow):
                d = dist_to_polygon_edge(px, py, bolt_shadow)
                shadow_alpha = min(1.0, d / (s * 0.03)) * 0.4
                r = int(r * (1 - shadow_alpha))
                g = int(g * (1 - shadow_alpha))
                b = int(b * (1 - shadow_alpha))

            # --- Glow layer ---
            if point_in_polygon(px, py, bolt_glow) and not point_in_polygon(px, py, bolt):
                d = dist_to_polygon_edge(px, py, bolt)
                glow_t = max(0, 1.0 - d / (s * 0.04))
                r, g, b = lerp_color((r, g, b), accent, glow_t * 0.6)

            # --- Lightning bolt ---
            in_bolt = point_in_polygon(px, py, bolt)
            bolt_edge_d = dist_to_polygon_edge(px, py, bolt)

            if in_bolt:
                # Bolt fill: gold gradient with 3D shading
                # Normalize position within bolt bounds
                bolt_min_y = min(p[1] for p in bolt)
                bolt_max_y = max(p[1] for p in bolt)
                bt = (py - bolt_min_y) / (bolt_max_y - bolt_min_y) if bolt_max_y > bolt_min_y else 0

                # Gold to bright yellow gradient
                bolt_top = (255, 230, 80)
                bolt_bot = (245, 170, 20)
                bolt_color = lerp_color(bolt_top, bolt_bot, bt)

                # 3D highlight on left edge
                bolt_min_x = min(p[0] for p in bolt)
                bolt_max_x = max(p[0] for p in bolt)
                bx_t = (px - bolt_min_x) / (bolt_max_x - bolt_min_x) if bolt_max_x > bolt_min_x else 0
                if bx_t < 0.3:
                    hl = (1.0 - bx_t / 0.3) * 0.3
                    bolt_color = lerp_color(bolt_color, white, hl)

                # Anti-alias at edges
                if bolt_edge_d < aa:
                    edge_t = bolt_edge_d / aa
                    r, g, b = lerp_color((r, g, b), bolt_color, edge_t)
                else:
                    r, g, b = bolt_color

            # --- Trophy/base ---
            if point_in_polygon(px, py, trophy):
                td = dist_to_polygon_edge(px, py, trophy)
                trophy_color = (220, 190, 60)
                if td < aa:
                    r, g, b = lerp_color((r, g, b), trophy_color, td / aa)
                else:
                    r, g, b = trophy_color

            # Trophy stem
            stem_w = s * 0.03
            stem_top = trophy_cy - trophy_h / 2
            stem_bot = min(p[1] for p in trophy)
            bolt_bot_y = max(p[1] for p in bolt)
            stem_top_actual = bolt_bot_y + s * 0.01
            if abs(px - trophy_cx) < stem_w and stem_top_actual < py < stem_top:
                stem_d = min(abs(px - trophy_cx + stem_w), abs(px - trophy_cx - stem_w))
                r, g, b = lerp_color((r, g, b), (200, 170, 50), min(1, stem_d / aa))

            # --- Sparkle stars ---
            for sx, sy, sr in sparkles:
                sd = math.sqrt((px - sx) ** 2 + (py - sy) ** 2)
                if sd < sr * 2:
                    # 4-point star shape
                    angle = math.atan2(py - sy, px - sx)
                    star_r = sr * (0.4 + 0.6 * abs(math.cos(2 * angle)))
                    if sd < star_r:
                        st = 1.0 - sd / star_r
                        r, g, b = lerp_color((r, g, b), white, st * 0.9)

            # Apply rounded rect alpha
            alpha = int(rect_alpha * 255)
            pixels[y][x] = (min(255, max(0, r)), min(255, max(0, g)), min(255, max(0, b)), alpha)

    return pixels


def generate_favicon(size: int = 32) -> list[list[tuple[int, int, int, int]]]:
    """Smaller, simpler icon for favicon."""
    return generate_icon(size)


def main():
    static = Path(__file__).parent / "static"
    static.mkdir(exist_ok=True)

    print("Generating icon-512.png ...")
    px512 = generate_icon(512)
    save_png(str(static / "icon-512.png"), px512, 512, 512)
    print("  Done (512x512)")

    # Downsample 512 -> 192
    print("Generating icon-192.png ...")
    px192 = generate_icon(192)
    save_png(str(static / "icon-192.png"), px192, 192, 192)
    print("  Done (192x192)")

    # Favicon 32x32
    print("Generating favicon.png ...")
    px32 = generate_icon(32)
    save_png(str(static / "favicon.png"), px32, 32, 32)
    print("  Done (32x32)")

    print("\nAll icons generated!")


if __name__ == "__main__":
    main()
