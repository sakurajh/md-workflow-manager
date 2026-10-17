# -*- coding: utf-8 -*-
"""生成 MD 主题应用图标（纯 Python 逐像素渲染，无第三方依赖）。

图标设计：蓝色对角渐变圆角底 + 三原子分子（白/琥珀色 3D 球体质感、白色化学键）
+ 环绕轨道弧线（体现"动力学"），超采样渲染后缩放到多尺寸。

输出:
    app.ico          多尺寸图标（256/64/48/32/24/16），供 exe 与窗口使用
    md_icon_64.ppm   64px PPM，供 Tk 窗口 iconphoto 使用

用法: python make_icon.py
"""
import math
import struct
import zlib

S = 512          # 渲染主分辨率（超采样）
RADIUS = 96      # 圆角半径

# 渐变与配色
GRAD_TOP = (59, 130, 246)      # #3b82f6
GRAD_BOTTOM = (29, 78, 216)    # #1d4ed8
WHITE = (255, 255, 255)
AMBER = (251, 191, 36)         # #fbbf24

# 分子布局（512 坐标系）
ATOMS = [                       # (cx, cy, r, base_color)
    (170, 200, 64, WHITE),
    (352, 159, 49, AMBER),
    (278, 346, 58, WHITE),
]
BONDS = [(0, 1), (1, 2), (0, 2)]
BOND_W = 17.0
# 轨道弧线（围绕琥珀原子，体现运动）: (cx, cy, r1, r2, a1, a2, alpha)
ARCS = [
    (352, 159, 74, 86, -2.9, -1.3, 0.55),
    (352, 159, 96, 104, 0.5, 1.7, 0.40),
]
DOTS = [(430, 380, 10, 0.45), (88, 92, 7, 0.35)]


def _seg_dist2(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return (px - x1) ** 2 + (py - y1) ** 2
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    ex, ey = x1 + t * dx, y1 + t * dy
    return (px - ex) ** 2 + (py - ey) ** 2


def render_master():
    """渲染 512x512 RGBA 主图（圆角、渐变、分子、轨道）。"""
    rows = []
    hyp = math.hypot
    for y in range(S):
        row = []
        for x in range(S):
            t = (x / S + y / S) / 2.0
            r = GRAD_TOP[0] * (1 - t) + GRAD_BOTTOM[0] * t
            g = GRAD_TOP[1] * (1 - t) + GRAD_BOTTOM[1] * t
            b = GRAD_TOP[2] * (1 - t) + GRAD_BOTTOM[2] * t
            col = [r, g, b]
            a = 1.0

            # 圆角 alpha
            nx = min(max(x, RADIUS), S - RADIUS)
            ny = min(max(y, RADIUS), S - RADIUS)
            d = hyp(x - nx, y - ny)
            if d > RADIUS:
                a = max(0.0, min(1.0, (RADIUS - d) / 1.5))

            # 化学键（胶囊）
            for i1, i2 in BONDS:
                x1, y1 = ATOMS[i1][0], ATOMS[i1][1]
                x2, y2 = ATOMS[i2][0], ATOMS[i2][1]
                if _seg_dist2(x, y, x1, y1, x2, y2) <= (BOND_W / 2) ** 2:
                    col = [col[i] * 0.08 + WHITE[i] * 0.92 for i in range(3)]
                    break

            # 轨道弧线
            for cx, cy, r1, r2, a1, a2, alpha in ARCS:
                dx, dy = x - cx, y - cy
                dd = dx * dx + dy * dy
                if r1 * r1 <= dd <= r2 * r2:
                    ang = math.atan2(dy, dx)
                    if a1 <= ang <= a2:
                        col = [col[i] * (1 - alpha) + WHITE[i] * alpha for i in range(3)]
                        break

            # 装饰点
            for cx, cy, cr, alpha in DOTS:
                if (x - cx) ** 2 + (y - cy) ** 2 <= cr * cr:
                    col = [col[i] * (1 - alpha) + WHITE[i] * alpha for i in range(3)]
                    break

            # 原子球（带高光与边缘暗化，覆盖在最上层）
            for cx, cy, cr, base in ATOMS:
                dx, dy = x - cx, y - cy
                dd = dx * dx + dy * dy
                if dd <= cr * cr:
                    d = math.sqrt(dd)
                    tt = d / cr
                    shade = 1.0 - 0.32 * tt * tt
                    cc = [c * shade for c in base]
                    # 左上高光
                    hd = hyp(x - (cx - cr * 0.33), y - (cy - cr * 0.33))
                    hl = max(0.0, 1.0 - hd / (cr * 0.85))
                    cc = [cc[i] * (1 - 0.6 * hl) + 255 * (0.6 * hl) for i in range(3)]
                    # 边缘抗锯齿
                    edge = cr - d
                    aa = 1.0 if edge >= 1.0 else max(0.0, edge)
                    col = [col[i] * (1 - aa) + cc[i] * aa for i in range(3)]
                    break

            row.append((col[0], col[1], col[2], a * 255.0))
        rows.append(row)
    return rows


def resize_box(rows, sw, sh, tw, th):
    """盒式平均缩放（带 alpha）。"""
    out = []
    for ty in range(th):
        y0, y1 = ty * sh / th, (ty + 1) * sh / th
        iy0, iy1 = int(y0), max(int(y0) + 1, int(math.ceil(y1)))
        row = []
        for tx in range(tw):
            x0, x1 = tx * sw / tw, (tx + 1) * sw / tw
            ix0, ix1 = int(x0), max(int(x0) + 1, int(math.ceil(x1)))
            rs = gs = bs = as_ = 0.0
            n = 0
            for yy in range(iy0, min(iy1, sh)):
                srow = rows[yy]
                for xx in range(ix0, min(ix1, sw)):
                    c = srow[xx]
                    rs += c[0]; gs += c[1]; bs += c[2]; as_ += c[3]
                    n += 1
            if n == 0:
                row.append((0.0, 0.0, 0.0, 0.0))
            else:
                row.append((rs / n, gs / n, bs / n, as_ / n))
        out.append(row)
    return out


def encode_png(rows, w, h):
    """RGBA PNG 编码（color type 6）。"""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for c in row:
            raw += bytes((int(c[0]) & 0xFF, int(c[1]) & 0xFF, int(c[2]) & 0xFF,
                          int(c[3]) & 0xFF))
    idat = zlib.compress(bytes(raw), 9)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def bmp_entry(rows, w, h):
    """ICO 32-bit BMP 条目（自下而上 + 空 AND 掩码，透明由 alpha 承担）。"""
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                         w * h * 4, 0, 0, 0, 0)
    pixel = bytearray()
    for y in range(h - 1, -1, -1):
        for c in rows[y]:
            pixel += bytes((int(c[2]) & 0xFF, int(c[1]) & 0xFF,
                            int(c[0]) & 0xFF, int(c[3]) & 0xFF))
    stride = ((w + 31) // 32) * 4
    mask = bytes(stride * h)
    return header + bytes(pixel) + mask


def encode_ico(images):
    """images: [(w, h, blob_bytes)]，>=256 用 PNG，其余用 BMP。"""
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    data = b""
    offset = 6 + 16 * len(images)
    for w, h, blob in images:
        wb = 0 if w >= 256 else w
        hb = 0 if h >= 256 else h
        entries += struct.pack("<BBBBHHII", wb, hb, 0, 0, 1, 32, len(blob), offset)
        data += blob
        offset += len(blob)
    return header + entries + data


def main():
    print(f"渲染 {S}x{S} 主图（纯 Python，约需几秒）...")
    master = render_master()
    print("缩放多尺寸...")
    images = []
    # 256 用 PNG（保留圆角透明）
    img256 = resize_box(master, S, S, 256, 256)
    images.append((256, 256, encode_png(img256, 256, 256)))
    # 小尺寸用 BMP
    for size in (64, 48, 32, 24, 16):
        small = resize_box(master, S, S, size, size)
        images.append((size, size, bmp_entry(small, size, size)))
    ico = encode_ico(images)
    with open("app.ico", "wb") as f:
        f.write(ico)
    print(f"app.ico 已生成（{len(ico)} 字节，{len(images)} 个尺寸: "
          f"256/64/48/32/24/16）")
    # Tk 窗口图标（64px PPM，无 alpha，合成到白底）
    small64 = resize_box(master, S, S, 64, 64)
    ppm = b"P6\n64 64\n255\n"
    for row in small64:
        for c in row:
            a = c[3] / 255.0
            ppm += bytes((int(c[0] * a + 255 * (1 - a)),
                          int(c[1] * a + 255 * (1 - a)),
                          int(c[2] * a + 255 * (1 - a))))
    with open("md_icon_64.ppm", "wb") as f:
        f.write(ppm)
    print("md_icon_64.ppm 已生成（窗口图标）")


if __name__ == "__main__":
    main()
