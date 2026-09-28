#!/usr/bin/env python3
"""txd_compress.py - repack RenderWare TXD archives with DXT-compressed
textures for the GL3 (librw) platform. See docs/07 for the full plan.

Pipeline per texture:
  parse native raster (D3D8 PC format: C8888 or PAL8)
    -> PIL Image
    -> ImageMagick `convert` to DXT1 (opaque) / DXT5 (alpha) DDS with mipmaps
    -> parse DDS back, extract per-mip compressed blocks
    -> emit GL3-native TEXTURENATIVE (flags|=2, compression=1|5)

The TXD chunk layout is written to match librw's readNativeTexture
(vendor/librw/src/gl/gl3raster.cpp): platform=8 (D3D8 container is kept,
the GL3 branch keys on `subplatform == gl3Caps.gles`), so pass --gles to
match the TARGET device (1 = R36S GLES, 0 = desktop GL debugging).

Usage:
  txd_compress.py in.txd out.txd [--gles 0|1] [--top N] [--dry]
      [--whitelist name1,name2] [--min-size 4]
"""
import argparse
import io
import os
import struct
import subprocess
import sys
import tempfile

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow required: pip install pillow")

ID_STRUCT = 0x0001
ID_EXTENSION = 0x0003
ID_TEXTURENATIVE = 0x0015
ID_TEXDICTIONARY = 0x0016

PLATFORM_D3D8 = 8
PLATFORM_GL3 = 12  # rwbase.h; Texture::streamReadNative dispatches on this
FMT_C8888 = 0x0500
FMT_PAL8 = 0x2500  # 8-bit palette, C8888 entries

RW_VERSION = 0x0C02FFFF

DXT_BPP = {1: 4, 3: 8, 5: 8}  # bits per pixel


def read_chunk(d, off):
    """RW chunk header: type, size, version. Returns (type, size, data_off, next_off)."""
    t, size, version = struct.unpack_from("<III", d, off)
    return t, size, off + 12, off + 12 + size


def u32(d, off):
    return struct.unpack_from("<I", d, off)[0]


def i32(d, off):
    return struct.unpack_from("<i", d, off)[0]


class Texture:
    """One parsed native texture (D3D8 PC layout)."""
    def __init__(self):
        self.name = ""
        self.mask = ""
        self.filter_addressing = 0
        self.format = 0
        self.has_alpha = 0
        self.width = 0
        self.height = 0
        self.depth = 0
        self.num_levels = 0
        self.type = 0
        self.compression = 0
        self.pixels = b""        # top-level pixels as stored
        self.palette = b""       # palette (4*entries) or empty
        self.level_sizes = []    # per-mip stored sizes
        self.level_data = []     # per-mip stored bytes
        self.pal4 = False
        self.original_struct = b""


def parse_d3d8_native(d, off, size):
    """Parse one TEXTURENATIVE STRUCT body (D3D8 PC), matching
    vendor/librw/src/d3d/d3d8.cpp readNativeTexture:
      u32 platform(8), u32 filterAddressing, name[32], mask[32],
      u32 format, i32 hasAlpha, u16 width, u16 height,
      u8 depth, u8 numLevels, u8 type, u8 compression,
      [palette 4*palentries if PAL4/PAL8],
      per level: u32 size + data."""
    tex = Texture()
    tex.filter_addressing = u32(d, off + 4)
    tex.name = d[off + 8:off + 40].split(b"\0")[0].decode("ascii", "replace")
    tex.mask = d[off + 40:off + 72].split(b"\0")[0].decode("ascii", "replace")
    p = off + 72
    tex.format = u32(d, p); p += 4
    tex.has_alpha = i32(d, p); p += 4
    tex.width = struct.unpack_from("<H", d, p)[0]; p += 2
    tex.height = struct.unpack_from("<H", d, p)[0]; p += 2
    tex.depth = d[p]; p += 1
    tex.num_levels = d[p]; p += 1
    tex.type = d[p]; p += 1
    tex.compression = d[p]; p += 1
    # already-compressed textures are passed through verbatim by main();
    # parse their level layout the same way (u32 size + data per level).
    pallength = 0
    if (tex.format & 0x2000) or (tex.format & 0x1000):  # PAL8 / PAL4
        pallength = 32 if (tex.format & 0x1000) else 256
        tex.palette = d[p:p + 4 * pallength]
        p += 4 * pallength
        tex.pal4 = pallength == 32
    tex.level_sizes = []
    tex.level_data = []
    for i in range(tex.num_levels):
        lsz = u32(d, p); p += 4
        tex.level_sizes.append(lsz)
        tex.level_data.append(d[p:p + lsz])
        p += lsz
    tex.pixels = tex.level_data[0] if tex.level_data else b""
    return tex


def parse_txd(path):
    """Return (numTex, [Texture], [orig_struct_payload]) from a D3D8-platform
    TXD. Traversal is bounded by the TEXDICTIONARY chunk size - img entries
    are sector-padded and trailing bytes are NOT chunks."""
    d = open(path, "rb").read()
    t, dsize, doff, dend = read_chunk(d, 0)
    if t != ID_TEXDICTIONARY:
        sys.exit(f"{path}: not a TEXDICTIONARY (0x{t:04x})")
    t, size, doff, send = read_chunk(d, doff)
    if t != ID_STRUCT:
        sys.exit(f"{path}: expected STRUCT")
    num_tex = struct.unpack_from("<h", d, doff)[0]
    textures = []
    orig_payloads = []
    off = send
    while off + 12 <= dend:
        t, size, doff2, noff = read_chunk(d, off)
        if t == ID_TEXTURENATIVE:
            st, ssize, sdoff, _ = read_chunk(d, doff2)
            if st != ID_STRUCT:
                sys.exit(f"{path}: TEXTURENATIVE without STRUCT")
            platform = u32(d, sdoff)
            if platform != PLATFORM_D3D8:
                sys.exit(f"{path}: platform {platform} != D3D8; refusing")
            textures.append(parse_d3d8_native(d, sdoff, ssize))
            orig_payloads.append(d[sdoff:sdoff + ssize])
        off = noff
    if len(textures) != num_tex:
        print(f"warn: {path}: header says {num_tex} textures, parsed {len(textures)}")
    return num_tex, textures, orig_payloads


def tex_to_pil(tex):
    """Native pixels -> RGBA PIL Image (top level only)."""
    if tex.format == FMT_C8888:
        img = Image.frombytes("RGBA", (tex.width, tex.height), tex.pixels, "raw", "BGRA")
        return img
    if (tex.format & 0x2000) or (tex.format & 0x1000):  # PAL8 / PAL4
        ncol = 32 if (tex.format & 0x1000) else 256
        pal = tex.palette
        palrgb = []
        for i in range(ncol):
            b, g, r, a = pal[i * 4:i * 4 + 4]
            palrgb.append((r, g, b, a))
        rgba = bytearray(tex.width * tex.height * 4)
        for pi, ix in enumerate(tex.pixels):
            if (tex.format & 0x1000) and (pi & 1):  # PAL4: two indices per byte
                continue
            r, g, b, a = palrgb[ix]
            rgba[pi * 4:pi * 4 + 4] = bytes((r, g, b, a))
        # PAL4 second pass: odd pixels
        if tex.format & 0x1000:
            for pi in range(tex.width * tex.height):
                if pi & 1 == 0:
                    continue
                byte = tex.pixels[pi >> 1]
                ix = byte & 0x0F
                r, g, b, a = palrgb[ix]
                rgba[pi * 4:pi * 4 + 4] = bytes((r, g, b, a))
        return Image.frombytes("RGBA", (tex.width, tex.height), bytes(rgba))
    raise ValueError(f"{tex.name}: format 0x{tex.format:04x}")


def has_alpha(img):
    a = img.getchannel("A")
    mn, mx = a.getextrema()
    return mn < 255


def compress_to_dxt(img, mode="dxt"):
    """RGBA Image -> (compression_code, [mip_compressed_bytes...]).
    mode: 'dxt' (desktop S3TC via ImageMagick), 'astc' (Mali R36S via astcenc).
    ASTC code = block size (6); DXT code = 1|5.
    Returns (0, []) if the texture is too small."""
    if img.width < 4 or img.height < 4:
        return 0, []
    with tempfile.TemporaryDirectory() as td:
        if mode == "astc":
            # astcenc does not chain mips - encode each level separately.
            data = []
            cur = img
            while True:
                src = os.path.join(td, f"m{len(data)}.png")
                cur.save(src)
                out = os.path.join(td, f"m{len(data)}.astc")
                r = subprocess.run(
                    ["astcenc", "-cl", src, out, "4x4", "-medium", "-silent"],
                    capture_output=True)
                if r.returncode != 0:
                    raise RuntimeError(f"astcenc failed: {r.stderr.decode()[:200]}")
                d = open(out, "rb").read()
                data.append(d[16:])  # strip 16B ASTC magic+header
                if cur.width <= 4 or cur.height <= 4:
                    break
                cur = cur.resize((max(1, cur.width // 2), max(1, cur.height // 2)), Image.LANCZOS)
            return 4, data
        else:
            dxt = 5 if has_alpha(img) else 1
            src = os.path.join(td, "in.png")
            dds = os.path.join(td, "out.dds")
            img.save(src)
            r = subprocess.run(
                ["convert", src, "-define", f"dds:compression=dxt{dxt}", "-define", "dds:mipmaps=16", dds],
                capture_output=True)
            if r.returncode != 0:
                raise RuntimeError(f"convert failed: {r.stderr.decode()}")
            return dxt, parse_dds_mips(open(dds, "rb").read())


def parse_dds_mips(d):
    """DDS -> list of per-mip compressed block bytes (DXT1/3/5)."""
    if d[:4] != b"DDS ":
        raise RuntimeError("not a DDS")
    hdr_size = u32(d, 4)          # usually 124
    h = u32(d, 12)
    w = u32(d, 16)
    mipcount = u32(d, 28) or 1
    fourcc = d[84:88]             # pixel format starts at 76: size(4) flags(4) fourcc(4)
    if fourcc == b"DXT1":
        blk = 8
    elif fourcc in (b"DXT3", b"DXT5"):
        blk = 16
    else:
        raise RuntimeError(f"unexpected fourcc {fourcc}")
    p = 4 + hdr_size
    mips = []
    mw, mh = w, h
    for i in range(mipcount):
        bw = max(1, (mw + 3) // 4)
        bh = max(1, (mh + 3) // 4)
        sz = bw * bh * blk
        mips.append(d[p:p + sz])
        p += sz
        mw = max(1, mw // 2)
        mh = max(1, mh // 2)
        if len(mips) >= mipcount:
            break
    return mips


def chunk(t, payload):
    return struct.pack("<III", t, len(payload), RW_VERSION) + payload


def build_glnative(tex, compression, mips):
    """One GL3-native TEXTURENATIVE payload (STRUCT body)."""
    body = bytearray()
    body += struct.pack("<I", PLATFORM_GL3)       # platform: streamReadNative dispatch key
    body += struct.pack("<I", tex.filter_addressing)
    body += tex.name.encode("ascii").ljust(32, b"\0")
    body += tex.mask.encode("ascii").ljust(32, b"\0")
    body += struct.pack("<I", tex.format)
    body += struct.pack("<iiii", tex.width, tex.height, tex.depth, len(mips))
    body += struct.pack("<i", ARGS.gles)          # subplatform = gl3Caps.gles
    # flags: bit0=hasAlpha, bit1=isCompressed. ASTC blocks always carry
    # alpha (GL_COMPRESSED_RGBA_ASTC_*), so keep bit0 for both encodings.
    flags = 2 | (1 if (compression == 5 or compression >= 4) else 0)
    body += struct.pack("<i", flags)
    body += struct.pack("<i", compression)
    for m in mips:
        body += struct.pack("<I", len(m)) + m
    return bytes(body)


def write_txd(textures_out, path):
    out = bytearray()
    # deviceId MUST be 0: RwTexDictionaryGtaStreamRead (small-file path)
    # reads the whole 4-byte STRUCT as one i32 loop count (numTex |
    # deviceId<<16); deviceId=1 made it try 65537 textures -> "Failed to
    # load TXD". The big-file path (GtaStreamRead1) divides by 2 and
    # survives either way.
    body = struct.pack("<hh", len(textures_out), 0)
    out += chunk(ID_TEXDICTIONARY, chunk(ID_STRUCT, body))
    for (tex, compression, mips) in textures_out:
        if compression:
            payload = build_glnative(tex, compression, mips)
        else:
            # too small to compress: keep original bytes verbatim
            payload = tex.original_struct
        out += chunk(ID_TEXTURENATIVE, chunk(ID_STRUCT, payload))
    out += chunk(ID_EXTENSION, b"")
    open(path, "wb").write(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input")
    ap.add_argument("output")
    ap.add_argument("--gles", type=int, default=1, choices=[0, 1],
                    help="target subplatform (1=R36S GLES, 0=desktop GL)")
    ap.add_argument("--astc", action="store_true",
                    help="encode ASTC 6x6 (Mali/Bifrost) instead of DXT (desktop S3TC)")
    ap.add_argument("--min-size", type=int, default=4)
    ap.add_argument("--whitelist", default="", help="comma-separated texture names to skip")
    ap.add_argument("--dry", action="store_true")
    global ARGS
    ARGS = ap.parse_args()
    skip = set(x.strip().lower() for x in ARGS.whitelist.split(",") if x.strip())

    num, textures, orig_payloads = parse_txd(ARGS.input)
    for tex, orig in zip(textures, orig_payloads):
        tex.original_struct = orig

    out_list = []
    saved = kept = skipped = 0
    for tex in textures:
        if tex.name.lower() in skip:
            skipped += 1
            out_list.append((tex, 0, []))
            continue
        try:
            if tex.compression:
                # already DXT in the source - keep verbatim
                print(f"  keep {tex.name}: already DXT{tex.compression}")
                out_list.append((tex, 0, []))
                kept += 1
                continue
            img = tex_to_pil(tex)
        except Exception as e:
            print(f"  keep {tex.name}: {e}")
            out_list.append((tex, 0, []))
            kept += 1
            continue
        compression, mips = compress_to_dxt(img, "astc" if ARGS.astc else "dxt")
        if compression == 0:
            out_list.append((tex, 0, []))
            kept += 1
            continue
        old = len(tex.pixels) + (1024 if tex.palette else 0)
        new = sum(len(m) for m in mips)
        saved += max(0, old - new)
        out_list.append((tex, compression, mips))
        print(f"  {tex.name:24s} {tex.width}x{tex.height} fmt=0x{tex.format:04x} "
              f"{'ASTC' if ARGS.astc else 'DXT'}{compression} mips={len(mips)} {old}->{new}B")
    print(f"== {ARGS.input}: {len(textures)} textures, kept={kept} skipped={skipped}, "
          f"saved≈{saved/1024:.0f}KB (file payload)")
    if not ARGS.dry:
        write_txd(out_list, ARGS.output)


if __name__ == "__main__":
    main()
