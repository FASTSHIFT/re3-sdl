#!/usr/bin/env python3
"""gxt_add_keys.py - add/override keys in the MAIN table of VC GXT files.

The reVC R36S port adds custom frontend menu entries (e.g. FED_PRF for the
perf HUD toggle). The stock game GXTs don't have those keys, so the menu
would show an empty string. This tool rewrites the MAIN table in place,
appending or overriding the given keys and re-sorting TKEY (reVC's
BinarySearch requires sorted keys). Other tables are copied unchanged.

Format (verified against gamefiles/TEXT/american.gxt):
  chunk   := magic(4s) + size(u32) + payload
  TABL    := (name(8s) + offset(u32)) * 79   # offset points at the table's
                                             # TKEY chunk (absolute)
  table   := TKEY(size = 12*n) + (offset(u32)+key(8s))*n  # sorted by key
          + TDAT + values (UTF-16LE, NUL-terminated)

Usage:
  python3 gxt_add_keys.py --gxt FILE --keys FILE.txt [--text-dir DIR]
Keys file format: [KEY]\nVALUE\n\n (Sergeanur txt style, ASCII values).

With --text-dir, processes every <lang>.gxt there against <lang>.txt
(same base name) — e.g. the utils/gxt/*.txt sources.
"""
import argparse
import struct
import sys
from pathlib import Path


def load_txt_keys(path):
    entries = {}
    cur, buf = None, []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.rstrip("\r")
        if line.startswith("[") and line.endswith("]"):
            if cur is not None:
                entries[cur] = "\n".join(buf).strip()
            cur, buf = line[1:-1], []
        else:
            buf.append(line)
    if cur is not None:
        entries[cur] = "\n".join(buf).strip()
    return entries


def key8(k):
    return k.upper().encode("ascii", errors="replace")[:8]


def parse_gxt(data):
    """Return (tables[(name, offset)], raw_chunks). tables in file order."""
    if data[:4] != b"TABL":
        sys.exit("not a VC GXT (missing TABL)")
    tabl_size = struct.unpack_from("<I", data, 4)[0]
    n = tabl_size // 12
    tables = []
    for i in range(n):
        name, off = struct.unpack_from("<8sI", data, 8 + i * 12)
        tables.append((name.rstrip(b"\x00").decode("latin1"), off))
    return tables


def parse_table(data, off):
    """Parse one table (TKEY+TDAT) -> dict key->value(str), plus TDAT start."""
    if data[off:off + 4] != b"TKEY":
        sys.exit("table at %d: expected TKEY" % off)
    ksize = struct.unpack_from("<I", data, off + 4)[0]
    nkeys = ksize // 12
    keys = []
    for i in range(nkeys):
        koff, kname = struct.unpack_from("<I8s", data, off + 8 + i * 12)
        keys.append((kname.rstrip(b"\x00").decode("latin1"), koff))
    tdat_off = off + 8 + ksize
    if data[tdat_off:tdat_off + 4] != b"TDAT":
        sys.exit("table at %d: expected TDAT at %d" % (off, tdat_off))
    tdat_size = struct.unpack_from("<I", data, tdat_off + 4)[0]
    tdat = data[tdat_off + 8: tdat_off + 8 + tdat_size]

    entries = {}
    for kname, koff in keys:
        # value is NUL-terminated UTF-16LE; tolerate odd trailing bytes
        end = tdat.index(b"\x00\x00", koff)
        raw = tdat[koff:end]
        if len(raw) % 2:
            raw += b"\x00"
        entries[kname] = raw.decode("utf-16-le", errors="replace")
    return entries


def build_table(entries):
    """Build TKEY+TDAT chunks from {key: str}. Keys sorted (upper, 8ch)."""
    values = b""
    offsets = {}
    for key in entries:  # TDAT order irrelevant, offsets explicit
        offsets[key] = len(values)
        values += entries[key].encode("utf-16-le") + b"\x00\x00"
    tdat = b"TDAT" + struct.pack("<I", len(values)) + values

    all_keys = b""
    for key in sorted(entries, key=key8):
        all_keys += struct.pack("<I8s", offsets[key], key8(key))
    tkey = b"TKEY" + struct.pack("<I", len(all_keys)) + all_keys
    return tkey + tdat


def rebuild_gxt(data, main_entries):
    """Rebuild the GXT with a replaced MAIN table; others byte-identical."""
    tables = parse_gxt(data)
    # End of the TABL chunk = start of first table
    first_off = min(off for _, off in tables)
    tabl_payload = b""
    new_tables = []
    out_off = None  # filled after we know table sizes
    # Build all table blobs first (MAIN replaced, others copied raw).
    blobs = []
    for name, off in tables:
        if name == "MAIN":
            blob = build_table(main_entries)
        else:
            # copy raw through to the next table boundary (or EOF)
            ends = [o for _, o in tables if o > off]
            end = min(ends) if ends else len(data)
            blob = data[off:end]
        blobs.append((name, blob))
    # Layout: TABL header + table blobs sequentially.
    tabl_n = len(blobs)
    tabl_size = tabl_n * 12
    out = bytearray()
    out += b"TABL" + struct.pack("<I", tabl_size)
    cur = 8 + tabl_size
    dir_entries = b""
    for name, blob in blobs:
        dir_entries += name.encode("latin1").ljust(8, b"\x00")[:8] + struct.pack("<I", cur)
        cur += len(blob)
    out += dir_entries
    for _, blob in blobs:
        out += blob
    return bytes(out)


def process(gxt_path, add_entries, txt_src=None):
    data = Path(gxt_path).read_bytes()
    tables = parse_gxt(data)
    main_off = next(off for name, off in tables if name == "MAIN")
    entries = parse_table(data, main_off)
    n_before = len(entries)

    changed = 0
    for k, v in add_entries.items():
        if not k.isascii():
            sys.exit("key must be ASCII: %r" % k)
        if entries.get(k) != v:
            entries[k] = v
            changed += 1
    # drop any non-ASCII keys that came from the source GXT itself
    # (the BinarySearch compares raw bytes; such keys can't round-trip here)
    entries = {k: v for k, v in entries.items() if k.isascii()}
    if not changed:
        print("%s: no changes (%d keys)" % (gxt_path, n_before))
        return

    new = rebuild_gxt(data, entries)
    Path(gxt_path).write_bytes(new)
    print("%s: %d keys -> %d keys (%d changed)" % (gxt_path, n_before, len(entries), changed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gxt", help="single GXT file to modify in place")
    ap.add_argument("--keys", required=True, help="txt with keys to add/override")
    ap.add_argument("--text-dir", help="process every *.gxt here using matching *.txt as base")
    args = ap.parse_args()

    add = load_txt_keys(args.keys)
    if not add:
        sys.exit("no keys in %s" % args.keys)

    if args.text_dir:
        for gxt in sorted(Path(args.text_dir).glob("*.gxt")):
            base = gxt.with_suffix(".txt")
            if base.exists():
                merged = load_txt_keys(base)
                merged.update(add)
                # write merged back through the same path so process() sees it
                tmp_keys = gxt.with_suffix(".merged.tmp")
                with open(tmp_keys, "w", encoding="utf-8") as f:
                    for k, v in merged.items():
                        f.write("[%s]\n%s\n\n" % (k, v))
                process(gxt, merged, base)
                tmp_keys.unlink()
            else:
                process(gxt, add)
    elif args.gxt:
        process(args.gxt, add)
    else:
        sys.exit("need --gxt or --text-dir")


if __name__ == "__main__":
    main()
