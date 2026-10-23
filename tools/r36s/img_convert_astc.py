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
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # img_common lives next to this script
import img_common  # noqa: E402

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
    """Extract one TXD entry to a temp file, convert.

    job = (idx, name, img_path, off, nbytes, gles, tools_dir); name is only
    used for logging. Returns (idx, outpath or None, status).
    """
    idx, name, img_path, off, nbytes, gles, tools_dir = job
    with open(img_path, "rb") as f:
        f.seek(off * SECTOR)
        data = f.read(nbytes)
    if len(data) < 64 or data[:4] != b"\x16\x00\x00\x00":
        # not a TEXDICTIONARY - pass through
        return idx, None, "passthrough-nonchunk"
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
    os.unlink(tdin.name)  # extracted input is no longer needed
    if r.returncode != 0 or not os.path.exists(tdx_out):
        return idx, None, f"convert-failed: {r.stdout.strip()[-120:]} {r.stderr.strip()[-120:]}"
    return idx, tdx_out, "ok"


def _md5(b):
    return hashlib.md5(b).hexdigest()


def convert_archive(indir, inimg, outdir, outimg, gles=1, jobs=None, tools_dir=HERE):
    """Convert a whole archive. Returns 0 on success, 1 if the same-name
    identity guard fails (nothing is written in that case)."""
    entries = read_dir(indir)
    txds = [(idx, name, off, sz * SECTOR)
            for idx, (off, sz, name) in enumerate(entries)
            if name.lower().endswith(".txd") and sz]
    print(f"{len(entries)} entries, {len(txds)} TXDs, converting with -j{jobs} ...")

    groups = img_common.dup_groups(entries)
    txd_groups = {k: v for k, v in groups.items() if k.endswith(".txd")}
    n_txd_entries = sum(len(v) for v in txd_groups.values())
    n_dff_groups = sum(1 for k in groups if k.endswith(".dff"))
    print(f"{len(txd_groups)} duplicate TXD groups ({n_txd_entries} entries), "
          f"keyed by directory index")
    if n_dff_groups:
        print(f"  ({n_dff_groups} duplicate DFF groups are copied verbatim)")

    # Entry names are NOT unique (7 TXD names appear twice in gta3.dir), so
    # the replacement table is keyed by the directory index, never by name.
    repl = {}
    stats = {"ok": 0, "passthrough-nonchunk": 0, "convert-failed": 0}
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        joblist = [(idx, name, inimg, off, nb, gles, tools_dir) for idx, name, off, nb in txds]
        for i, res in enumerate(ex.map(convert_one, joblist)):
            idx, outpath, status = res
            stats[status] = stats.get(status, 0) + 1
            if outpath:
                repl[idx] = outpath
            if (i + 1) % 100 == 0:
                print(f"  {i+1}/{len(joblist)} done ({stats.get('convert-failed',0)} failed)")

    def cleanup():
        for p in repl.values():
            if os.path.exists(p):
                os.unlink(p)

    # identity guard: same-name entries must stay identical/different exactly
    # as they were in the source (only group members are hashed)
    members = sorted({i for v in groups.values() for i in v})
    before, after = {}, {}
    with open(inimg, "rb") as f:
        for idx in members:
            off, sz, _name = entries[idx]
            f.seek(off * SECTOR)
            slot = f.read(sz * SECTOR)
            before[idx] = _md5(slot)
            if idx in repl:
                with open(repl[idx], "rb") as g:
                    d = g.read()
                d += b"\0" * (-len(d) % SECTOR)
                after[idx] = _md5(d)
            else:
                after[idx] = before[idx]
    violations = img_common.assert_dup_identity(entries, before, after)
    if violations:
        for v in violations:
            print("ERROR: " + v, file=sys.stderr)
        print("ERROR: same-name identity check failed, archive NOT written", file=sys.stderr)
        cleanup()
        return 1

    # rebuild archive
    img = open(inimg, "rb")
    ndir = bytearray()
    outbuf = bytearray()
    next_sector = 0
    for idx, (off, sz, name) in enumerate(entries):
        if idx in repl:
            data = open(repl[idx], "rb").read()
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
    print(f"== stats: {stats}")
    print(f"== {len(outbuf)/1048576:.0f}MB written -> {outdir}, {outimg}")

    cleanup()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("indir")
    ap.add_argument("inimg")
    ap.add_argument("outdir")
    ap.add_argument("outimg")
    ap.add_argument("--gles", type=int, default=1, choices=[0, 1])
    ap.add_argument("-j", type=int, default=os.cpu_count())
    args = ap.parse_args()
    sys.exit(convert_archive(args.indir, args.inimg, args.outdir, args.outimg,
                             args.gles, args.j, HERE))


if __name__ == "__main__":
    main()
