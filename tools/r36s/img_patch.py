#!/usr/bin/env python3
"""img_patch.py - replace entries inside a GTA .img archive (V1 format).

Reads gta3.dir (32-byte entries: u32 sector offset, u32 sector size,
24-byte name), swaps in replacement files (must be <= original sector
count), rewrites a new .img + .dir pair.

Usage:
  img_patch.py gta3.dir gta3.img out.dir out.img SPEC1 [SPEC2 ...]

  SPEC is either
    name=file     replace the entry called `name` (case-insensitive); the name
                  must match exactly one entry, otherwise the tool exits with
                  code 2 and lists the candidate idx values
    idx:N=file    replace directory entry number N (0-based); use this for
                  names that appear more than once in gta3.dir
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import img_common  # noqa: E402

SECTOR = 2048


def main():
    if len(sys.argv) < 6:
        sys.exit(__doc__)
    indir, inimg, outdir, outimg = sys.argv[1:5]
    entries = img_common.read_dir(indir)
    # validates everything (ambiguity, range, duplicates); exits 2 before any
    # output file is touched
    repl = img_common.resolve_replacements(entries, sys.argv[5:])

    img = open(inimg, "rb").read()
    imgbuf = bytearray(img)
    ndir = bytearray()
    nrepl = 0
    for idx, (off, sz, name) in enumerate(entries):
        if idx in repl:
            new = open(repl[idx], "rb").read()
            cap = sz * SECTOR
            if len(new) > cap:
                sys.exit(f"[{idx}] {name}: replacement {len(new)}B > slot {cap}B")
            imgbuf[off * SECTOR:off * SECTOR + len(new)] = new
            # pad rest with zeros
            imgbuf[off * SECTOR + len(new):off * SECTOR + cap] = b"\0" * (cap - len(new))
            nrepl += 1
            print(f"patched [{idx}] {name}: slot {cap}B <- {len(new)}B ({repl[idx]})")
        ndir += struct.pack("<II", off, sz) + name.encode("ascii").ljust(24, b"\0")
    open(outimg, "wb").write(bytes(imgbuf))
    open(outdir, "wb").write(bytes(ndir))
    print(f"== {nrepl}/{len(entries)} entries patched -> {outdir}, {outimg}")


if __name__ == "__main__":
    main()
