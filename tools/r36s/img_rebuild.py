#!/usr/bin/env python3
"""img_rebuild.py - rebuild a GTA .img archive (V1 format) with replacement
entries, allowing entries to GROW (unlike img_patch.py's in-place slots).

Reads gta3.dir/gta3.img, swaps in replacement files for matching entry names,
rewrites the archive sequentially with fresh sector offsets, and pads every
entry to a 2048B sector boundary.

Usage:
  img_rebuild.py gta3.dir gta3.img out.dir out.img name1=newfile1 [name2=...]
"""
import struct
import sys

SECTOR = 2048


def main():
    if len(sys.argv) < 6:
        sys.exit(__doc__)
    indir, inimg, outdir, outimg = sys.argv[1:5]
    repl = {}
    for a in sys.argv[5:]:
        k, v = a.split("=", 1)
        repl[k.lower()] = v

    d = open(indir, "rb").read()
    entries = []
    for i in range(len(d) // 32):
        off, sz = struct.unpack_from("<II", d, i * 32)
        name = d[i * 32 + 8:i * 32 + 32].split(b"\0")[0].decode("ascii", "replace")
        entries.append([off, sz, name])

    img = open(inimg, "rb")
    ndir = bytearray()
    outbuf = bytearray()
    next_sector = 0
    nrepl = 0
    for off, sz, name in entries:
        nl = name.lower()
        if nl in repl:
            data = open(repl[nl], "rb").read()
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
