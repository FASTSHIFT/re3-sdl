#!/usr/bin/env python3
"""img_patch.py - replace entries inside a GTA .img archive (V1 format).

Reads gta3.dir (32-byte entries: u32 sector offset, u32 sector size,
24-byte name), swaps in replacement files (must be <= original sector
count), rewrites a new .img + .dir pair.

Usage:
  img_patch.py gta3.dir gta3.img out.dir out.img name1=newfile1 [name2=...]
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

    img = open(inimg, "rb").read()
    imgbuf = bytearray(img)
    ndir = bytearray()
    nrepl = 0
    for off, sz, name in entries:
        nl = name.lower()
        if nl in repl:
            new = open(repl[nl], "rb").read()
            cap = sz * SECTOR
            if len(new) > cap:
                sys.exit(f"{name}: replacement {len(new)}B > slot {cap}B")
            imgbuf[off * SECTOR:off * SECTOR + len(new)] = new
            # pad rest with zeros
            imgbuf[off * SECTOR + len(new):off * SECTOR + cap] = b"\0" * (cap - len(new))
            nrepl += 1
            print(f"patched {name}: slot {cap}B <- {len(new)}B ({repl[nl]})")
        ndir += struct.pack("<II", off, sz) + name.encode("ascii").ljust(24, b"\0")
    open(outimg, "wb").write(bytes(imgbuf))
    open(outdir, "wb").write(bytes(ndir))
    print(f"== {nrepl}/{len(entries)} entries patched -> {outdir}, {outimg}")


if __name__ == "__main__":
    main()
