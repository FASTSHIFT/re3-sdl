#!/usr/bin/env python3
"""test_gxt.py - data-consistency tests for the VC Chinese GXT + atlas (T1-T5).

Docs: docs/04 §3.1. Pure offline asserts over the l10n outputs; no engine
involved. Exit code = number of failures.

Usage:
  python3 tools/l10n/test/test_gxt.py [--gxt l10n-out/JAPANESE.GXT]
      [--png l10n-out/FONTJAP.png] [--map l10n-out/cn_charmap.json]
      [--src utils/gxt/american.txt] [--trans translation.txt]
"""
import argparse
import json
import re
import struct
import sys

JAP_FLAG = 0x8000
CTRL_RE = re.compile(r"~[A-Za-z0-9_]*~")


def parse_gxt(path):
    """Return {table: [(key, [code, ...]), ...]} + TABL order (engine view)."""
    data = open(path, "rb").read()
    out, order = {}, []
    assert data[:4] == b"TABL", "missing TABL"
    tabl_size = struct.unpack_from("<I", data, 4)[0]
    n = tabl_size // 12
    for i in range(n):
        name, off = struct.unpack_from("<8sI", data, 8 + i * 12)
        name = name.rstrip(b"\x00").decode("latin1")
        order.append(name)
        if name == "MAIN":
            base = off
        else:
            base = off + 8  # mission tables: 8s name prefix, no chunk header
        assert data[base:base + 4] == b"TKEY", f"{name}: no TKEY @ {base}"
        ksize = struct.unpack_from("<I", data, base + 4)[0]
        tdat_off = base + 8 + ksize
        assert data[tdat_off:tdat_off + 4] == b"TDAT", f"{name}: no TDAT"
        tdat_size = struct.unpack_from("<I", data, tdat_off + 4)[0]
        tdat = data[tdat_off + 8:tdat_off + 8 + tdat_size]
        entries = []
        for i in range(ksize // 12):
            koff, kname = struct.unpack_from("<I8s", data, base + 8 + i * 12)
            # Walk wchar-aligned like the engine: a code < 0x100 has high
            # byte 0x00, so a byte-level b"\x00\x00" scan would false-
            # terminate between e.g. 0x0080 and 0x0400 (bytes: 80 00 00 04).
            codes, pos = [], koff
            while True:
                (c,) = struct.unpack_from("<H", tdat, pos)
                if c == 0:
                    break
                codes.append(c)
                pos += 2
            entries.append((kname.rstrip(b"\x00").decode("latin1"), codes))
        out[name] = entries
    return out, order


def ctrl_multiset(s):
    return sorted(CTRL_RE.findall(s))


def load_txt_tables(path):
    """Source-of-truth: {key: value} + {table: [keys...]} from american.txt."""
    entries, tables, cur, buf, cur_table = {}, {}, None, None, "MAIN"
    TAG = re.compile(r"\{\s*[^}]*\}\s*$")
    TBL = re.compile(r"^\{[=\s]*MISSION TABLE ([^}]+?)\s*[=]*\}$")
    for line in open(path, encoding="utf-8-sig", errors="replace"):
        line = line.rstrip("\n")
        m = re.match(r"^\[([^\]]+)\]\s*$", line)
        tm = TBL.match(line)
        if tm:
            if cur is not None:
                entries[cur] = "\n".join(buf).strip()
            cur, tables_ = None, tm.group(1)
            tables.setdefault(tables_, [])
            cur_table = tables_
        elif m:
            if cur is not None:
                entries[cur] = TAG.sub("", "\n".join(buf).strip()).rstrip()
            cur, buf = m.group(1), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        entries[cur] = TAG.sub("", "\n".join(buf).strip()).rstrip()
    return entries, tables


def load_translation(path):
    """translation.txt -> {full_key: zh_value} (mission keys KEY:TABLE)."""
    entries = {}
    cur, buf = None, None
    TBL = re.compile(r"^\{[=\s]*MISSION TABLE ([^}]+?)\s*[=]*\}$")  # merge marker
    for line in open(path, encoding="utf-8-sig", errors="replace"):
        line = line.rstrip("\n")
        if TBL.match(line):
            continue  # boundary re-emitted by make_translation_batches merge
        m = re.match(r"^\[([^\]]+)\]\s*$", line)
        if m:
            if cur is not None:
                entries[cur] = "\n".join(buf).strip()
            cur, buf = m.group(1), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        entries[cur] = "\n".join(buf).strip()
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gxt", default="l10n-out/JAPANESE.GXT")
    ap.add_argument("--png", default="l10n-out/FONTJAP.png")
    ap.add_argument("--map", default="l10n-out/cn_charmap.json")
    ap.add_argument("--src", default="utils/gxt/american.txt")
    ap.add_argument("--trans", default="translation.txt")
    args = ap.parse_args()

    fails = []
    def check(name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}" + (f" - {detail}" if detail and not ok else ""))
        if not ok:
            fails.append((name, detail))

    tables, order = parse_gxt(args.gxt)
    charmap = json.load(open(args.map, encoding="utf-8"))
    cols, rows = charmap["cols"], charmap["rows"]
    atlas_cells = cols * rows

    # ---- T1: structure ----
    check("T1.1 TABL order starts with MAIN", order[0] == "MAIN")
    tsorted = True
    for t, entries in tables.items():
        keys = [k.upper() for k, _ in entries]
        if keys != sorted(keys):
            tsorted = False
            check(f"T1.2 TKEY sorted in {t}", False, "out of strcmp order")
    if tsorted:
        check("T1.2 TKEY strcmp-sorted (all tables)", True)

    # ---- T5: key completeness vs source ----
    src_entries, src_tables = load_txt_tables(args.src)
    zh = load_translation(args.trans)
    gxt_keys = set()
    for t, entries in tables.items():
        for k, _ in entries:
            gxt_keys.add(f"{k}:{t}" if t != "MAIN" else k)
    # mission keys in source carry KEY:TABLE
    src_keys = set(src_entries.keys())
    missing = src_keys - gxt_keys
    check("T5.1 all source keys present in GXT", not missing,
          f"missing {len(missing)}: {sorted(missing)[:5]}")

    # ---- T3: control-code conservation (source EN vs GXT codes) ----
    # Build the expected code stream from zh values + charmap, compare.
    cmap = {ch: v["code"] for ch, v in charmap["map"].items()}
    bad_ctrl = bad_flag = 0
    for t, entries in tables.items():
        for k, codes in entries:
            full = f"{k}:{t}" if t != "MAIN" else k
            if full not in zh:
                continue
            v = zh[full].replace("\n", "")  # format noise, stripped by generator
            # expected: token chars flagged, others plain
            exp, i = [], 0
            while i < len(v):
                if v[i] == "~":
                    exp.append(JAP_FLAG | ord("~"))
                    i += 1
                    while i < len(v) and v[i] != "~":
                        exp.append(JAP_FLAG | (ord(v[i]) & 0xFF))
                        i += 1
                    if i < len(v):
                        exp.append(JAP_FLAG | ord("~"))
                        i += 1
                else:
                    o = ord(v[i])
                    exp.append(o if o < 0x80 else cmap[v[i]])
                    i += 1
            if codes != exp:
                bad_ctrl += 1
                if bad_ctrl <= 3:
                    print(f"    {full}: GXT {codes[:8]} != exp {exp[:8]}")
    check("T3 code stream matches translation+charmap", bad_ctrl == 0,
          f"{bad_ctrl} keys differ")

    # ---- T2: codepoint coverage ----
    used_cells = set()
    for t, entries in tables.items():
        for k, codes in entries:
            for c in codes:
                if c & JAP_FLAG:
                    continue
                if c == 0:
                    continue
                cell = c - 0x20
                if cell >= atlas_cells:
                    check("T2.1 codepoint in atlas range", False,
                          f"{k} (table {t}): code {hex(c)} -> cell {cell} >= {atlas_cells}")
                used_cells.add(cell)
    check("T2.1 codepoint in atlas range", True)
    mapped = set(v["cell"] for v in charmap["map"].values())
    orphan = used_cells - mapped - set(range(95))  # 0..94 = ASCII
    check("T2.2 no unmapped CJK cells referenced", not orphan,
          f"{sorted(orphan)[:5]}")

    # ---- T4: atlas completeness (PNG cells non-empty where mapped) ----
    try:
        from PIL import Image
        img = Image.open(args.png).convert("RGBA")
        cell_px = charmap["cell"]
        empty = []
        for ch, v in charmap["map"].items():
            c = v["cell"]
            cx, cy = (c % cols) * cell_px, (c // cols) * cell_px
            region = img.crop((cx, cy, cx + cell_px, cy + cell_px))
            if not region.getchannel("A").getbbox():
                empty.append((ch, c))
        check("T4.1 every mapped glyph has pixels", not empty,
              f"{len(empty)} empty: {empty[:5]}")
        # ASCII cells present too (0x20 = space is blank by design)
        missing_ascii = [code for code in range(0x21, 0x7F)
                         if not img.crop(((code-0x20) % cols * cell_px,
                                          (code-0x20) // cols * cell_px,
                                          (code-0x20) % cols * cell_px + cell_px,
                                          (code-0x20) // cols * cell_px + cell_px)
                                         ).getchannel("A").getbbox()]
        check("T4.2 ASCII cells rendered (excl. space)", not missing_ascii,
              f"missing {len(missing_ascii)}: {[chr(c) for c in missing_ascii[:8]]}")
    except ImportError:
        check("T4 atlas pixels (Pillow missing)", False, "pip install pillow")

    print()
    if fails:
        print(f"FAILED: {len(fails)}")
        for n, d in fails:
            print(f"  - {n}: {d}")
        sys.exit(len(fails))
    print("ALL PASS")


if __name__ == "__main__":
    main()
