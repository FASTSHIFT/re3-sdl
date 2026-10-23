#!/usr/bin/env python3
"""img_rebuild.py - rebuild a GTA .img archive (V1 format) with replacement
entries, allowing entries to GROW (unlike img_patch.py's in-place slots).

Reads gta3.dir/gta3.img, swaps in replacement files for the selected entries,
rewrites the archive sequentially with fresh sector offsets, and pads every
entry to a 2048B sector boundary.

Usage:
  img_rebuild.py gta3.dir gta3.img out.dir out.img SPEC1 [SPEC2 ...]

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

    img = open(inimg, "rb")
    ndir = bytearray()
    outbuf = bytearray()
    next_sector = 0
    nrepl = 0
    for idx, (off, sz, name) in enumerate(entries):
        if idx in repl:
            data = open(repl[idx], "rb").read()
            nrepl += 1
        else:
            img.seek(off * SECTOR)
            data = img.read(sz * SECTOR)
        nsec = (len(data) + SECTOR - 1) // SECTOR
        ndir += struct.pack("<II", next_sector, nsec) + name.encode("ascii").ljust(24, b"\0")
        outbuf += data
        pad = nsec * SECTOR - len(data)
        if pad:
            outbuf += b"\0" * pad
        next_sector += nsec
    img.close()

    open(outimg, "wb").write(bytes(outbuf))
    open(outdir, "wb").write(bytes(ndir))
    print(f"== rebuilt {nrepl}/{len(entries)} entries replaced, "
          f"{len(outbuf)/1048576:.0f}MB -> {outdir}, {outimg}")


if __name__ == "__main__":
    main()
