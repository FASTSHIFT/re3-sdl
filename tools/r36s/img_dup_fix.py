#!/usr/bin/env python3
"""img_dup_fix.py - incremental repair of an already converted gta3.img/.dir.

gta3.dir contains TXD names that appear twice with different content. An
older, name-keyed converter wrote one shared payload into both slots. This
tool takes the converted archive as BASE, re-converts only the same-name TXD
entries (target set T) from the original SOURCE archive, copies every other
entry from BASE verbatim and rebuilds offsets sequentially. No full
re-conversion is needed. Python 3 standard library only.

Usage:
  img_dup_fix.py --base-dir B.dir --base-img B.img
                 --src-dir  S.dir --src-img  S.img
                 --out-dir  O.dir --out-img  O.img
                 [--gles 1] [-j N] [--tools-dir DIR] [--spot-check N]

Steps:
  1. base and src directories must have the same entry count and identical
     names per index (else exit 2).
  2. T = every idx of a same-name group whose name ends with .txd and sz > 0.
  3. (--spot-check N, before anything is written) N unique-name TXDs of the
     SOURCE (IGmike.txd / LODhaiti.txd always included when present) are
     converted again and must equal the BASE slot of the same idx. A mismatch
     means BASE was not produced by the same pipeline (exit 1).
  4. T is converted from SOURCE data with img_convert_astc.convert_one (key =
     directory index; tools dir must contain txd_compress.py). convert-failed
     -> exit 1; passthrough-nonchunk -> source bytes are used, with a warning.
  5. Output is streamed to out.dir/out.img + '.partial'; BASE slots are copied
     in chunks. After self-checks pass the .partial files are renamed to the
     final names; on any failure no final output exists:
       (a) non-T slots are byte-identical to BASE
       (b) per T entry: old sectors (base) -> new sectors
       (c) img_common.assert_dup_identity over ALL dup groups (before = md5 of
           the SOURCE slot, after = md5 of the OUT slot)
       (d) offsets contiguous, len(img) == sum(sz) * 2048

Exit codes: 0 ok, 1 check/conversion failure, 2 usage / input mismatch.
"""
import argparse
import hashlib
import os
import random
import struct
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import img_common  # noqa: E402
import img_convert_astc  # noqa: E402

SECTOR = img_common.SECTOR
CHUNK = 1 << 20
MUST_SPOT = ("igmike.txd", "lodhaiti.txd")


def die(code, msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(code)


def pad_sector(data):
    return data + b"\0" * (-len(data) % SECTOR)


def read_slot(f, off, sz):
    f.seek(off * SECTOR)
    return f.read(sz * SECTOR)


def slot_md5(f, off, sz):
    """md5 of a slot, chunked."""
    h = hashlib.md5()
    f.seek(off * SECTOR)
    left = sz * SECTOR
    while left > 0:
        b = f.read(min(CHUNK, left))
        if not b:
            break
        h.update(b)
        left -= len(b)
    return h.hexdigest()


def copy_slot(src, dst, off, sz):
    """Copy a slot src->dst in chunks; returns bytes copied."""
    src.seek(off * SECTOR)
    left = sz * SECTOR
    total = 0
    while left > 0:
        b = src.read(min(CHUNK, left))
        if not b:
            raise IOError("short read copying slot at sector %d" % off)
        dst.write(b)
        left -= len(b)
        total += len(b)
    return total


def slots_equal(fa, offa, fb, offb, nbytes):
    fa.seek(offa * SECTOR)
    fb.seek(offb * SECTOR)
    left = nbytes
    while left > 0:
        n = min(CHUNK, left)
        a, b = fa.read(n), fb.read(n)
        if a != b or len(a) != n:
            return False
        left -= n
    return True


def check_archive_bounds(label, entries, img_path):
    size = os.path.getsize(img_path)
    for idx, (off, sz, name) in enumerate(entries):
        if (off + sz) * SECTOR > size:
            die(2, "error: %s entry idx %d (%s) lies beyond the end of %s (%d bytes)"
                % (label, idx, name, img_path, size))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for k in ("base-dir", "base-img", "src-dir", "src-img", "out-dir", "out-img"):
        ap.add_argument("--" + k, required=True)
    ap.add_argument("--gles", type=int, default=1, choices=[0, 1])
    ap.add_argument("-j", type=int, default=os.cpu_count())
    ap.add_argument("--tools-dir", default=None)
    ap.add_argument("--spot-check", type=int, default=0, metavar="N")
    args = ap.parse_args()

    tools_dir = os.path.abspath(args.tools_dir) if args.tools_dir else HERE
    if not os.path.isfile(os.path.join(tools_dir, "txd_compress.py")):
        die(2, "error: txd_compress.py not found in tools dir %s" % tools_dir)
    for p in (args.base_dir, args.base_img, args.src_dir, args.src_img):
        if not os.path.isfile(p):
            die(2, "error: input file not found: %s" % p)
    ins = {os.path.realpath(p) for p in
           (args.base_dir, args.base_img, args.src_dir, args.src_img)}
    for p in (args.out_dir, args.out_img):
        if os.path.realpath(p) in ins:
            die(2, "error: output path %s equals an input file" % p)

    # ---- 1. directories ---------------------------------------------------
    base = img_common.read_dir(args.base_dir)
    src = img_common.read_dir(args.src_dir)
    if len(base) != len(src):
        die(2, "error: entry count differs: base %d vs src %d" % (len(base), len(src)))
    bad = [i for i in range(len(src)) if base[i][2] != src[i][2]]
    if bad:
        msg = ["error: %d entries have different names in base and src:" % len(bad)]
        for i in bad[:10]:
            msg.append("    idx %d: base '%s' vs src '%s'" % (i, base[i][2], src[i][2]))
        die(2, "\n".join(msg))
    check_archive_bounds("base", base, args.base_img)
    check_archive_bounds("src", src, args.src_img)

    # ---- 2. target set ----------------------------------------------------
    groups = img_common.dup_groups(src)
    members = sorted({i for v in groups.values() for i in v})
    T = [i for i in members if src[i][2].lower().endswith(".txd") and src[i][1] > 0]
    Tset = set(T)
    print("%d entries, %d dup groups (%d members), %d target TXD entries"
          % (len(src), len(groups), len(members), len(T)))

    # stale partials from an earlier run
    part_img, part_dir = args.out_img + ".partial", args.out_dir + ".partial"
    for p in (part_img, part_dir):
        if os.path.exists(p):
            os.unlink(p)
            print("removed stale %s" % p)

    tmp_files = []

    def cleanup_tmp():
        for p in tmp_files:
            if os.path.exists(p):
                os.unlink(p)

    def fail(msg):
        sys.stderr.write("ERROR: " + msg.rstrip("\n") + "\n")
        cleanup_tmp()
        for p in (part_img, part_dir):
            if os.path.exists(p):
                os.unlink(p)
        sys.exit(1)

    def jobs_for(idxs):
        return [(i, src[i][2], args.src_img, src[i][0], src[i][1] * SECTOR,
                 args.gles, tools_dir) for i in idxs]

    ex = ProcessPoolExecutor(max_workers=args.j)
    try:
        # ---- 3. spot check (before writing anything) ----------------------
        if args.spot_check > 0:
            lname_count = {}
            for off, sz, name in src:
                lname_count[name.lower()] = lname_count.get(name.lower(), 0) + 1
            uniq = [i for i, (off, sz, name) in enumerate(src)
                    if name.lower().endswith(".txd") and sz > 0
                    and lname_count[name.lower()] == 1]
            must = [i for i in uniq if src[i][2].lower() in MUST_SPOT]
            rest = [i for i in uniq if i not in set(must)]
            nfill = max(0, args.spot_check - len(must))
            rng = random.Random(0)
            picks = must + (rng.sample(rest, min(nfill, len(rest))) if nfill else [])
            print("spot check: re-converting %d unique-name TXDs and comparing with base ..."
                  % len(picks))
            match = mismatch = 0
            with open(args.src_img, "rb") as fs, open(args.base_img, "rb") as fb:
                for idx, outpath, status in ex.map(img_convert_astc.convert_one,
                                                   jobs_for(picks)):
                    if outpath:
                        tmp_files.append(outpath)
                    if status.startswith("convert-failed"):
                        fail("spot check: idx %d %s conversion failed: %s"
                             % (idx, src[idx][2], status))
                    if outpath:
                        with open(outpath, "rb") as g:
                            conv = pad_sector(g.read())
                    else:  # passthrough-nonchunk: source bytes
                        conv = read_slot(fs, src[idx][0], src[idx][1])
                    bslot = read_slot(fb, base[idx][0], base[idx][1])
                    if conv == bslot:
                        match += 1
                        print("  idx %d %s: match" % (idx, src[idx][2]))
                    else:
                        mismatch += 1
                        print("  idx %d %s: MISMATCH (converted %d bytes vs base slot %d bytes)"
                              % (idx, src[idx][2], len(conv), len(bslot)))
            print("spot check: %d match, %d mismatch" % (match, mismatch))
            cleanup_tmp()
            tmp_files.clear()
            if mismatch:
                fail("spot check mismatch: base was NOT produced by the same pipeline")

        # ---- 4. convert T from SOURCE data --------------------------------
        print("converting %d target entries (-j%s) ..." % (len(T), args.j))
        repl = {}  # idx -> bytes (unpadded) to place into the archive
        for idx, outpath, status in ex.map(img_convert_astc.convert_one, jobs_for(T)):
            if outpath:
                tmp_files.append(outpath)
            if status.startswith("convert-failed"):
                fail("idx %d %s: %s" % (idx, src[idx][2], status))
            if outpath:
                with open(outpath, "rb") as g:
                    repl[idx] = g.read()
            elif status == "passthrough-nonchunk":
                print("WARNING: idx %d %s is not a TEXDICTIONARY, using source bytes as-is"
                      % (idx, src[idx][2]))
                with open(args.src_img, "rb") as fs:
                    repl[idx] = read_slot(fs, src[idx][0], src[idx][1])
            else:
                fail("idx %d %s: unexpected status %r" % (idx, src[idx][2], status))
    finally:
        ex.shutdown()
    cleanup_tmp()
    tmp_files.clear()

    # ---- 5. sequential streamed write to .partial ---------------------------
    expect_md5 = {}
    ndir = bytearray()
    next_sector = 0
    try:
        with open(args.base_img, "rb") as fb, open(part_img, "wb") as fo:
            for idx, (off, sz, name) in enumerate(base):
                if idx in repl:
                    data = pad_sector(repl[idx])
                    nsec = len(data) // SECTOR
                    fo.write(data)
                    expect_md5[idx] = hashlib.md5(data).hexdigest()
                else:
                    nsec = sz
                    copy_slot(fb, fo, off, sz)
                ndir += struct.pack("<II", next_sector, nsec) + name.encode("ascii").ljust(24, b"\0")
                next_sector += nsec
        with open(part_dir, "wb") as f:
            f.write(bytes(ndir))
    except Exception as e:  # noqa: BLE001
        fail("writing output failed: %r" % (e,))
    del repl
    print("wrote %s (%d sectors, %.1f MB), %s" % (part_img, next_sector,
                                                  next_sector * SECTOR / 1048576, part_dir))

    # ---- 6. self-checks (on what is actually on disk) -----------------------
    problems = []
    out = img_common.read_dir(part_dir)

    # (d) structure
    if len(out) != len(base):
        problems.append("(d) out entry count %d != base %d" % (len(out), len(base)))
    else:
        exp_off = 0
        for idx, (off, sz, name) in enumerate(out):
            if name != base[idx][2]:
                problems.append("(d) idx %d name '%s' != '%s'" % (idx, name, base[idx][2]))
            if off != exp_off:
                problems.append("(d) idx %d offset %d, expected %d (not contiguous)"
                                % (idx, off, exp_off))
            exp_off = off + sz
        total = sum(sz for _o, sz, _n in out) * SECTOR
        actual = os.path.getsize(part_img)
        if actual != total:
            problems.append("(d) len(img)=%d != sum(sz)*2048=%d" % (actual, total))
    print("(d) structure: %s" % ("FAIL" if problems else "ok"))

    if not problems:
        # (a) non-T slots identical to base
        bad_a = []
        with open(args.base_img, "rb") as fb, open(part_img, "rb") as fo:
            for idx in range(len(base)):
                if idx in Tset:
                    continue
                if out[idx][1] != base[idx][1] or not slots_equal(
                        fb, base[idx][0], fo, out[idx][0], base[idx][1] * SECTOR):
                    bad_a.append(idx)
        for idx in bad_a[:20]:
            problems.append("(a) idx %d %s differs from base" % (idx, base[idx][2]))
        print("(a) %d non-target entries compared with base: %s"
              % (len(base) - len(Tset), "FAIL (%d differ)" % len(bad_a) if bad_a else "all identical"))

        # (b) report + verify T slot content
        print("(b) target entries (old sectors (base) -> new sectors):")
        with open(part_img, "rb") as fo:
            for idx in T:
                print("    %d %s: %d -> %d" % (idx, src[idx][2], base[idx][1], out[idx][1]))
                if slot_md5(fo, out[idx][0], out[idx][1]) != expect_md5[idx]:
                    problems.append("(b) idx %d %s: out slot differs from converted data"
                                    % (idx, src[idx][2]))

        # (c) dup identity over ALL groups
        before, after = {}, {}
        with open(args.src_img, "rb") as fs, open(part_img, "rb") as fo:
            for idx in members:
                before[idx] = slot_md5(fs, src[idx][0], src[idx][1])
                after[idx] = slot_md5(fo, out[idx][0], out[idx][1])
        viol = img_common.assert_dup_identity(src, before, after)
        for v in viol:
            problems.append("(c) " + v)
        print("(c) dup identity over %d groups: %s" % (len(groups), "FAIL" if viol else "ok"))

    if problems:
        for p in problems:
            sys.stderr.write("ERROR: %s\n" % p)
        fail("self-check failed, output NOT written")

    # ---- 7. publish ---------------------------------------------------------
    os.replace(part_img, args.out_img)
    os.replace(part_dir, args.out_dir)
    print("== done: %d target entries re-converted, %d entries copied from base"
          % (len(T), len(base) - len(T)))
    print("== %s (%d bytes), %s" % (args.out_img, os.path.getsize(args.out_img), args.out_dir))


if __name__ == "__main__":
    main()
