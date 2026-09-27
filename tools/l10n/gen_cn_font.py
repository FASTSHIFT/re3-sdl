#!/usr/bin/env python3
"""gen_cn_font.py - build Chinese font assets for reVC's (widened) CJK pipeline.

VC port of the re3 tool. reVC reuses the game's Japanese font path for CJK: a
big glyph atlas indexed by codepoint, full-width layout. This tool produces
assets that drop straight into the Japanese slots (JAPANESE.GXT + a FONTJAP
atlas), with the REVC_CHINESE build widening the atlas geometry (see docs/01
§6 and Font.cpp).

VC GXT difference vs re3/GTA3 (the reason this tool exists):
  * GTA3: one flat TKEY+TDAT table.
  * VC:  TABL directory + a MAIN table + 78 mission tables. Each mission
    table's data blob starts with the 8-byte table name (no chunk header),
    followed by its own TKEY+TDAT. Keys carry the table suffix in the source
    txt ("KEY:TABLE"); in the GXT the stored key is just the KEY part and the
    key belongs to that table's TKEY. MAIN keys have no suffix.

Codepoint / atlas contract (MUST match Font.cpp when REVC_CHINESE):
  * The print loop subtracts 0x20 from every wchar before indexing, and the
    CJK draw uses cell = (wchar-0x20), col = cell % COLS, row = cell // COLS.
  * So a glyph placed in atlas cell i is stored in the GXT as wchar (i + 0x20).
  * ASCII is placed in cells 0..94 so that wchar == the ASCII code itself
    (' ' -> cell 0 -> 0x20, 'A' -> cell 33 -> 0x41): ASCII text needs no
    remapping and renders from the same atlas.
  * Chinese glyphs occupy cells 95.. (wchar 0x7F..).
  * Control tokens (~g~ etc.): every char of the token carries the 0x8000 bit
    (JAP_TERMINATION scheme), exactly like the stock JAPANESE.GXT.

Outputs:
  * out-gxt  : JAPANESE.GXT (TABL + MAIN + 78 mission tables)
  * out-png  : FONTJAP atlas (COLS x rows of CELL px, white glyphs, alpha=shape)
  * out-map  : char <-> cell/codepoint map (debug)

Then pack the PNG with tools/png2txd.py as texture 'FONTJAP' into FONTS_J.TXD.

This is an offline tool; not built by CMake. Requires Pillow.
"""
import argparse
import json
import re
import struct
import sys

try:
    from PIL import Image, ImageFont, ImageDraw
except ImportError:
    sys.exit("Pillow required: pip install pillow")

JAP_TERM = 0x8000 | ord('~')  # 0x807E, how the JP GXT stores '~'
CTRL_RE = re.compile(r"~[A-Za-z0-9_]*~")
ASCII_CELLS = 95  # cells 0..94 hold ASCII 0x20..0x7E (' '..'~')
TABLE_RE = re.compile(r"^\{[=\s]*MISSION TABLE ([^}]+?)\s*[=]*\}$")
TAG_RE = re.compile(r"\{\s*[^}]*\}\s*$")


def load_translation(path):
    """Load the translation txt into {full_key: value}, where full_key keeps
    the 'KEY:TABLE' suffix for mission-table entries. Also returns the ordered
    list of table names as they appear (MAIN is the implicit first table)."""
    entries = {}
    tables = []  # mission table names in file order
    cur, buf = None, None
    for line in open(path, encoding="utf-8-sig", errors="replace"):
        line = line.rstrip("\n")
        m = re.match(r"^\[([^\]]+)\]\s*$", line)
        if m:
            if cur is not None:
                entries[cur] = strip_tag("\n".join(buf).strip())
            cur, buf = m.group(1), []
        elif TABLE_RE.match(line):
            if cur is not None:
                entries[cur] = strip_tag("\n".join(buf).strip())
                cur = None
            tables.append(TABLE_RE.match(line).group(1))
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        entries[cur] = strip_tag("\n".join(buf).strip())
    return entries, tables


def strip_tag(value):
    lines = [l for l in value.splitlines() if not TAG_RE.match(l)]
    return TAG_RE.sub("", "\n".join(lines)).rstrip()


def collect_glyphs(entries):
    glyphs = set()
    for v in entries.values():
        for ch in v:
            if ord(ch) >= 0x80:
                glyphs.add(ch)
    return sorted(glyphs)


def build_charmap(glyphs):
    m = {}
    for i, ch in enumerate(glyphs):
        cell = ASCII_CELLS + i
        m[ch] = (cell, cell + 0x20)
    return m


def encode_value(v, charmap):
    """Encode one string to GXT wchars following the JP scheme: control tokens
    carry the 0x8000 flag on every char (engine token parsers match on it).

    Newlines are stripped first: the Sergeanur txt format ends every value
    with a blank line, so parsed values carry a trailing newline that is
    format noise (the stock GXT contains no 0x000A codepoints at all - the
    engine wraps text via its message system, never via newline codes)."""
    v = v.replace("\n", "")
    out = []
    i = 0
    while i < len(v):
        ch = v[i]
        if ch == '~':
            out.append(JAP_TERM)
            i += 1
            while i < len(v) and v[i] != '~':
                out.append(0x8000 | (ord(v[i]) & 0xFF))
                i += 1
            if i < len(v):
                out.append(JAP_TERM)
                i += 1
            continue
        o = ord(ch)
        if o < 0x80:
            out.append(o)
        else:
            out.append(charmap[ch][1])
        i += 1
    return out


def key8(k):
    return k.upper().encode("ascii")[:8]


def build_tkey_tdat(tbl_entries):
    """One table's TKEY+TDAT chunks. tbl_entries: {KEY(without suffix): value}
    in any order; TKEY is sorted (BinarySearch), TDAT follows source order."""
    all_values = b""
    offsets = {}
    for key in tbl_entries:  # source order for TDAT
        offsets[key] = len(all_values)
        for code in encode_value(tbl_entries[key], charmap_ref[0]):
            all_values += struct.pack("<H", code)
        all_values += b"\x00\x00"
    tdat = b"TDAT" + struct.pack("<I", len(all_values)) + all_values
    all_keys = b""
    for key in sorted(tbl_entries, key=key8):
        all_keys += struct.pack("<I8s", offsets[key], key8(key))
    tkey = b"TKEY" + struct.pack("<I", len(tbl_entries) * 12) + all_keys
    return tkey + tdat


# charmap threading for build_tkey_tdat (set in main before use)
charmap_ref = [None]


def to_vc_gxt(entries, tables):
    """Build the full VC GXT: TABL + MAIN + mission tables.

    entries: {full_key: value} with 'KEY:TABLE' mission suffixes.
    tables : mission table names in source order.
    Layout (verified against the stock gamefiles GXT):
      TABL chunk: (8s name + u32 abs offset) * num_tables, MAIN first
      each table: TKEY + TDAT chunks; mission tables are prefixed with the
      8-byte table name INSIDE their blob (no chunk header around it).
    """
    # Split entries: MAIN (no ':') vs per-table ('KEY:TABLE').
    main = {}
    per_table = {t: {} for t in tables}
    for full, v in entries.items():
        if ":" in full:
            key, table = full.rsplit(":", 1)
            if table in per_table:
                per_table[table][key] = v
            else:  # unknown table: keep visible instead of dropping
                sys.stderr.write("WARN: key %s references unknown table %s\n"
                                 % (full, table))
                main[full] = v
        else:
            main[full] = v

    # Build all table blobs; record lengths to lay out the TABL offsets.
    blobs = [("MAIN", build_tkey_tdat(main))]
    for t in tables:
        blob = build_tkey_tdat(per_table[t])
        # Mission table blob = 8s name + TKEY/TDAT, no chunk header.
        blobs.append((t, t.encode("ascii").ljust(8, b"\x00")[:8] + blob))

    tabl_payload = b""
    out = bytearray()
    num = len(blobs)
    out += b"TABL" + struct.pack("<I", num * 12)
    cur = 8 + num * 12
    dir_entries = b""
    for name, blob in blobs:
        dir_entries += name.encode("ascii").ljust(8, b"\x00")[:8] \
                       + struct.pack("<I", cur)
        cur += len(blob)
    out += dir_entries
    for _, blob in blobs:
        out += blob
    return bytes(out)


def render_atlas(glyphs, charmap, font_path, cell, cols, rows, px,
                 ascii_font_path, ascii_px):
    """Render ASCII (cells 0..94) then Chinese (95..) into a fixed grid. White
    glyphs with coverage in alpha. Height = rows*cell (must match CJK_ROWS_UV)."""
    W, H = cols * cell, rows * cell
    cov = Image.new("L", (W, H), 0)
    draw = ImageDraw.Draw(cov)
    cn_font = ImageFont.truetype(font_path, px)
    ascii_font = ImageFont.truetype(ascii_font_path or font_path, ascii_px)

    def put(cellidx, ch, font):
        cx = (cellidx % cols) * cell
        cy = (cellidx // cols) * cell
        bbox = draw.textbbox((0, 0), ch, font=font)
        gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        ox = cx + (cell - gw) // 2 - bbox[0]
        oy = cy + (cell - gh) // 2 - bbox[1]
        draw.text((ox, oy), ch, fill=255, font=font)

    for code in range(0x20, 0x7F):
        put(code - 0x20, chr(code), ascii_font)
    for ch, (cellidx, _code) in charmap.items():
        put(cellidx, ch, cn_font)

    white = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    white.putalpha(cov)
    return white


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("translation", help="translation.txt (upstream fmt, mission keys as KEY:TABLE)")
    ap.add_argument("--font", default="/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                    help="CJK TTF/TTC")
    ap.add_argument("--ascii-font", default="", help="ASCII TTF (default: --font)")
    ap.add_argument("--out-gxt", default="JAPANESE.GXT")
    ap.add_argument("--out-png", default="FONTJAP.png")
    ap.add_argument("--out-map", default="cn_charmap.json")
    ap.add_argument("--cols", type=int, default=64, help="atlas columns (match CJK_COLS)")
    ap.add_argument("--rows", type=int, default=40, help="atlas rows (match CJK_ROWS_UV)")
    ap.add_argument("--cell", type=int, default=16, help="cell px (texW/cols)")
    ap.add_argument("--px", type=int, default=14, help="CJK glyph render px")
    ap.add_argument("--ascii-px", type=int, default=12, help="ASCII glyph render px")
    args = ap.parse_args()

    entries, tables = load_translation(args.translation)
    glyphs = collect_glyphs(entries)
    charmap = build_charmap(glyphs)
    charmap_ref[0] = charmap
    cap = args.cols * args.rows
    used = ASCII_CELLS + len(glyphs)
    print("entries: %d (MAIN %d + mission %d), tables: %d"
          % (len(entries),
             len([k for k in entries if ":" not in k]),
             len([k for k in entries if ":" in k]),
             len(tables) + 1))
    print("CJK glyphs: %d, cells used: %d / %d" % (len(glyphs), used, cap))
    if used > cap:
        sys.exit("atlas too small: need %d cells, have %d (raise --rows)"
                 % (used, cap))
    top_code = (ASCII_CELLS + len(glyphs) - 1) + 0x20 if glyphs else 0x7E
    if top_code >= 0x8000:
        sys.exit("codepoint 0x%X collides with JAP_TERMINATION range" % top_code)

    atlas = render_atlas(glyphs, charmap, args.font, args.cell, args.cols,
                         args.rows, args.px, args.ascii_font, args.ascii_px)
    atlas.save(args.out_png)
    print("atlas: %dx%d, %d cols x %d rows -> %s"
          % (atlas.width, atlas.height, args.cols, args.rows, args.out_png))

    gxt = to_vc_gxt(entries, tables)
    open(args.out_gxt, "wb").write(gxt)
    print("gxt: %d bytes -> %s" % (len(gxt), args.out_gxt))

    json.dump({"cols": args.cols, "rows": args.rows, "cell": args.cell,
               "ascii_cells": ASCII_CELLS,
               "map": {ch: {"cell": c, "code": code} for ch, (c, code) in charmap.items()}},
              open(args.out_map, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("charmap -> %s" % args.out_map)


if __name__ == "__main__":
    main()
