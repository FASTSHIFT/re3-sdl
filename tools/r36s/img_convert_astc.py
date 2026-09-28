#!/usr/bin/env python3
"""img_convert_astc.py - whole-archive TXD -> GL3-native ASTC conversion.

Reads gta3.dir/gta3.img, converts every D3D8-platform TXD entry with
txd_compress.py (--astc), rebuilds the archive with img_rebuild.py semantics
(entries may grow). Non-TXD entries and unparseable TXDs pass through
verbatim. Run from the re3-sdl repo root (or pass --tools-dir).

Parallelism: one worker process per CPU, each running txd_compress.py on an
extracted entry. astcenc is single-instance-per-process here (the tool calls
it per texture), so we scale by entries.

Usage:
  img_convert_astc.py gta3.dir gta3.img out.dir out.img [--gles 1] [-j N]
"""
import argparse
import os
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR = 2048


def read_dir(path):
    d = open(path, "rb").read()
    entries = []
    for i in range(len(d) // 32):
        off, sz = struct.unpack_from("<II", d, i * 32)
        name = d[i * 32 + 8:i * 32 + 32].split(b"\0")[0].decode("ascii", "replace")
        entries.append((off, sz, name))
    return entries


def convert_one(job):
    """Extract one TXD entry to a temp file, convert, return (name, outpath or None)."""
    name, img_path, off, nbytes, gles, tools_dir = job
    with open(img_path, "rb") as f:
        f.seek(off * SECTOR)
        data = f.read(nbytes)
    if len(data) < 64 or data[:4] != b"\x16\x00\x00\x00":
        # not a TEXDICTIONARY - pass through
        return name, None, "passthrough-nonchunk"
    # GL3-native or empty TXD? detect platform field of first texture if any
    # (cheap sniff: TEXDICT STRUCT then TEXTURENATIVE STRUCT platform u32)
    # Just let txd_compress decide; it refuses non-D3D8 via SystemExit.
    tdin = tempfile.NamedTemporaryFile(suffix=".txd", delete=False)
    tdin.write(data)
    tdin.close()
    tdx_out = tdin.name + ".astc.txd"
    r = subprocess.run(
        [sys.executable, os.path.join(tools_dir, "txd_compress.py"),
         tdin.name, tdx_out, "--gles", str(gles), "--astc"],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(tdx_out):
        os.unlink(tdin.name)
        return name, None, f"convert-failed: {r.stdout.strip()[-120:]} {r.stderr.strip()[-120:]}"
    return name, tdx_out, "ok"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("indir")
    ap.add_argument("inimg")
    ap.add_argument("outdir")
    ap.add_argument("outimg")
    ap.add_argument("--gles", type=int, default=1, choices=[0, 1])
    ap.add_argument("-j", type=int, default=os.cpu_count())
    args = ap.parse_args()

    entries = read_dir(args.indir)
    txds = [(name, args.inimg, off, sz * SECTOR)
            for off, sz, name in entries if name.lower().endswith(".txd") and sz]
    print(f"{len(entries)} entries, {len(txds)} TXDs, converting with -j{args.j} ...")

    repl = {}
    stats = {"ok": 0, "passthrough-nonchunk": 0, "convert-failed": 0}
    with ProcessPoolExecutor(max_workers=args.j) as ex:
        jobs = [(name, img, off, nb, args.gles, HERE) for name, img, off, nb in txds]
        for i, res in enumerate(ex.map(convert_one, jobs)):
            name, outpath, status = res
            stats[status] = stats.get(status, 0) + 1
            if outpath:
                repl[name] = outpath
            if (i + 1) % 100 == 0:
                print(f"  {i+1}/{len(jobs)} done ({stats.get('convert-failed',0)} failed)")

    # rebuild archive
    img = open(args.inimg, "rb")
    ndir = bytearray()
    outbuf = bytearray()
    next_sector = 0
    for off, sz, name in entries:
        nl = name.lower()
        if nl in repl:
            data = open(repl[nl], "rb").read()
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
    open(args.outimg, "wb").write(bytes(outbuf))
    open(args.outdir, "wb").write(bytes(ndir))
    print(f"== stats: {stats}")
    print(f"== {len(outbuf)/1048576:.0f}MB written -> {args.outdir}, {args.outimg}")

    # cleanup temp files
    for p in repl.values():
        os.unlink(p)


if __name__ == "__main__":
    main()
