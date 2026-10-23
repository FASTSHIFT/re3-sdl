#!/usr/bin/env python3
"""img_dup_check.py - regression checker for same-name entries in GTA .img/.dir.

Read-only. Python 3 standard library only.

Usage:
  img_dup_check.py --src-dir S.dir --src-img S.img --out-dir O.dir --out-img O.img
                   [--full] [--refs --ide FILE [--ide FILE ...]]
                   [--preserve B.dir B.img] [--out-prefix-skip N]

Checks (one PASS/FAIL line each):
  structure   entry count / name order equal; out offsets contiguous
              (off[0]=0, off[i+1]=off[i]+sz[i]); len(out.img)=sum(sz)*2048;
              all slots inside their .img files.
  dup groups  report every same-name group: idx / name / src+out sectors /
              md5[:8] of the slot / texture names (TXD).
  identity    for every pair in a dup group: (src slots equal) == (out slots
              equal). Detects entries merged by a name-keyed rebuild.
  textures    textureNames(out[idx]) == textureNames(src[idx]) for dup-group
              TXDs (fast mode: seek-reads only those slots); --full does it for
              ALL TXD entries.
  --refs      for models of the given IDE files: every texture referenced by
              the model's dff that exists in the SOURCE txd must exist in the
              OUTPUT txd (dff = first entry named <model>.dff, txd = first
              entry named <txd>.txd, case-insensitive, like LoadCdDirectory).
              References already missing in the source are info only.
  --preserve  out vs base archive: same count / names; every entry that is not
              a dup-group TXD has equal size and equal bytes; dup-group TXDs
              that differ from base are only counted and listed.

--out-prefix-skip N: ignore the first N bytes of every OUT txd when parsing
texture names (only for test fakes that prepend a marker to converted data;
real converted TXDs start directly with the 0x16 chunk).

Exit codes: 0 all checks pass, 1 a check failed, 2 usage / structure error
(missing files, count/name mismatch, non-contiguous offsets, wrong .img length).
"""
import argparse
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import img_common  # noqa: E402

SECTOR = img_common.SECTOR
CHUNK = 1 << 20
MAX_DEPTH = 16
MAX_LIST = 20  # max failing items printed per check

# IDE sections that use the `id, model, txd, ...` line layout.
IDE_SECTIONS = {"objs", "tobj", "hier", "cars", "ped", "peds", "weap"}


class Report:
    def __init__(self):
        self.failed = False

    def ok(self, msg):
        print("PASS  %s" % msg)

    def fail(self, msg):
        self.failed = True
        print("FAIL  %s" % msg)

    def info(self, msg):
        print("      %s" % msg)


# --------------------------------------------------------------------------
# archive access (seek/read only, never the whole file)
# --------------------------------------------------------------------------

class Archive:
    def __init__(self, label, dir_path, img_path):
        self.label = label
        self.dir_path = dir_path
        self.img_path = img_path
        self.entries = img_common.read_dir(dir_path)
        self.img_size = os.path.getsize(img_path)
        self.f = open(img_path, "rb")
        self._first = None

    def close(self):
        self.f.close()

    def read_slot(self, idx):
        off, sz, _ = self.entries[idx]
        self.f.seek(off * SECTOR)
        return self.f.read(sz * SECTOR)

    def slot_md5(self, idx):
        off, sz, _ = self.entries[idx]
        h = hashlib.md5()
        self.f.seek(off * SECTOR)
        left = sz * SECTOR
        while left > 0:
            b = self.f.read(min(left, CHUNK))
            if not b:
                break
            h.update(b)
            left -= len(b)
        return h.hexdigest()

    def first_index(self):
        """lower name -> first idx (game's first-occurrence rule)."""
        if self._first is None:
            self._first = {}
            for idx, (_o, _s, name) in enumerate(self.entries):
                self._first.setdefault(name.lower(), idx)
        return self._first


def slots_equal(a, ia, b, ib):
    """Compare slot ia of archive a with slot ib of archive b, chunked."""
    oa, sa, _ = a.entries[ia]
    ob, sb, _ = b.entries[ib]
    if sa != sb:
        return False
    a.f.seek(oa * SECTOR)
    b.f.seek(ob * SECTOR)
    left = sa * SECTOR
    while left > 0:
        n = min(left, CHUNK)
        x = a.f.read(n)
        y = b.f.read(n)
        if x != y or len(x) != n:
            return False
        left -= n
    return True


def is_txd(name):
    return name.lower().endswith(".txd")


def short(h):
    return h[:8]


# --------------------------------------------------------------------------
# dff texture reference parsing
# --------------------------------------------------------------------------

# parent chunk type -> the only child type we descend into
_DESCEND = {0x10: 0x1A, 0x1A: 0x0F, 0x0F: 0x08, 0x08: 0x07, 0x07: 0x06}
_CLUMP, _TEXTURE, _STRING = 0x10, 0x06, 0x02


def dff_texture_names(data):
    """Lower-cased texture names referenced by a DFF ([] for garbage).

    Clump(0x10) -> GeometryList(0x1A) -> Geometry(0x0F) -> MaterialList(0x08)
    -> Material(0x07) -> Texture(0x06) -> first String(0x02). Other chunks
    (Struct, Extension, FrameList, Atomic ...) are skipped by length; Struct is
    never entered. Sizes are clamped to the data, depth is capped.
    """
    names = []
    n = len(data)
    if n < 12 or struct.unpack_from("<I", data, 0)[0] != _CLUMP:
        return names

    def walk(ctype, start, end, depth):
        # [start, end) is the payload of a chunk of type ctype
        if depth > MAX_DEPTH:
            return
        if ctype == _TEXTURE:
            p = start
            while p + 12 <= end:
                ct, cs, _v = struct.unpack_from("<III", data, p)
                pe = min(p + 12 + cs, end)
                if ct == _STRING:
                    raw = data[p + 12:pe]
                    nm = raw.split(b"\0")[0].decode("ascii", "replace").strip().lower()
                    if nm:
                        names.append(nm)
                    return  # only the first String (the second is the mask)
                p = p + 12 + cs
            return
        want = _DESCEND.get(ctype)
        if want is None:
            return
        p = start
        while p + 12 <= end:
            ct, cs, _v = struct.unpack_from("<III", data, p)
            pe = min(p + 12 + cs, end)
            if ct == want:
                walk(ct, p + 12, pe, depth + 1)
            p = p + 12 + cs  # always advances by >= 12: no infinite loop

    _ct, cs, _v = struct.unpack_from("<III", data, 0)
    walk(_CLUMP, 12, min(12 + cs, n), 0)
    return names


# --------------------------------------------------------------------------
# IDE parsing
# --------------------------------------------------------------------------

def parse_ide(path):
    """-> [(model, txd)] from `id, model, txd, ...` lines (comments, non
    numeric first field, `end` and unrelated sections are skipped)."""
    out = []
    section = None
    with open(path, "r", errors="replace") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            low = line.lower()
            if low == "end":
                section = None
                continue
            parts = [p.strip() for p in line.replace("\t", " ").split(",")]
            if len(parts) == 1:  # section header such as `peds`
                section = low.split()[0]
                continue
            if section is not None and section not in IDE_SECTIONS:
                continue
            try:
                int(parts[0])
            except ValueError:
                continue
            if len(parts) < 3 or not parts[1] or not parts[2]:
                continue
            out.append((parts[1], parts[2]))
    return out


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_structure(rep, src, out):
    """Return True if checks can continue (structure sane)."""
    ok = True
    # source bounds
    bad = [i for i, (o, s, _n) in enumerate(src.entries) if (o + s) * SECTOR > src.img_size]
    if bad:
        rep.fail("structure: %d src slots exceed src.img (first idx %d)" % (len(bad), bad[0]))
        ok = False
    if len(src.entries) != len(out.entries):
        rep.fail("structure: entry count differs (src %d, out %d)"
                 % (len(src.entries), len(out.entries)))
        return False
    rep.ok("structure: entry count equal (%d)" % len(src.entries))

    diff = [i for i in range(len(src.entries))
            if src.entries[i][2].lower() != out.entries[i][2].lower()
            or src.entries[i][2] != out.entries[i][2]]
    if diff:
        i = diff[0]
        rep.fail("structure: %d names differ in order (first idx %d: src '%s' vs out '%s')"
                 % (len(diff), i, src.entries[i][2], out.entries[i][2]))
        ok = False
    else:
        rep.ok("structure: name order equal")

    expect = 0
    bad_off = None
    for i, (o, s, _n) in enumerate(out.entries):
        if o != expect:
            bad_off = (i, o, expect)
            break
        expect = o + s
    if bad_off:
        rep.fail("structure: out offsets not contiguous at idx %d (off %d, expected %d)" % bad_off)
        ok = False
    else:
        rep.ok("structure: out offsets contiguous")

    total = sum(s for (_o, s, _n) in out.entries) * SECTOR
    if out.img_size != total:
        rep.fail("structure: len(out.img)=%d but sum(sz)*2048=%d" % (out.img_size, total))
        ok = False
    else:
        rep.ok("structure: len(out.img) == sum(sz)*2048 (%d bytes)" % total)
    return ok


def txd_names_of(arch, idx, prefix_skip=0):
    data = arch.read_slot(idx)
    if prefix_skip:
        data = data[prefix_skip:]
    return img_common.txd_texture_names(data)


def check_dup_groups(rep, src, out, groups, skip):
    """Report groups, run identity guard + texture preservation for dup TXDs.
    Returns the set of dup-group TXD idx."""
    dup_txd = set()
    if not groups:
        rep.info("no duplicate-name groups in the source archive")
    src_md5, out_md5 = {}, {}
    print("\n== duplicate-name groups: %d groups, %d entries =="
          % (len(groups), sum(len(v) for v in groups.values())))
    names_src, names_out = {}, {}
    for lname, idxs in sorted(groups.items(), key=lambda kv: kv[1][0]):
        print("group '%s' (%d entries)" % (lname, len(idxs)))
        for i in idxs:
            src_md5[i] = src.slot_md5(i)
            out_md5[i] = out.slot_md5(i)
            line = "  idx %-5d %-22s src %4d sec  out %4d sec  src md5 %s  out md5 %s" % (
                i, src.entries[i][2], src.entries[i][1], out.entries[i][1],
                short(src_md5[i]), short(out_md5[i]))
            if is_txd(src.entries[i][2]):
                dup_txd.add(i)
                names_src[i] = txd_names_of(src, i)
                names_out[i] = txd_names_of(out, i, skip)
                line += "  tex %s" % names_src[i]
                if names_out[i] != names_src[i]:
                    line += " -> out %s" % names_out[i]
            print(line)

    print("\n== checks ==")
    # identity guard
    viol = img_common.assert_dup_identity(src.entries, src_md5, out_md5) if groups else []
    if viol:
        for v in viol[:MAX_LIST]:
            rep.fail("identity: " + v)
        if len(viol) > MAX_LIST:
            rep.info("... %d more identity violations" % (len(viol) - MAX_LIST))
    else:
        rep.ok("identity: all dup-group pairs keep (src equal) == (out equal)")

    # texture names preserved (dup TXDs)
    _report_names(rep, src, names_src, names_out, "textures(dup groups)")
    return dup_txd


def _report_names(rep, src, names_src, names_out, label):
    bad = []
    notes = 0
    for i in sorted(names_src):
        a, b = names_src[i], names_out[i]
        if a == b:
            continue
        if sorted(a) == sorted(b):
            notes += 1
            continue
        bad.append(i)
    if bad:
        for i in bad[:MAX_LIST]:
            rep.fail("%s: idx %d (%s): src %s != out %s"
                     % (label, i, src.entries[i][2], names_src[i], names_out[i]))
        if len(bad) > MAX_LIST:
            rep.info("... %d more texture-name mismatches" % (len(bad) - MAX_LIST))
    else:
        extra = " (%d with order differences only)" % notes if notes else ""
        rep.ok("%s: texture names preserved for %d TXD entries%s"
               % (label, len(names_src), extra))


def check_full(rep, src, out, skip):
    ns, no = {}, {}
    for i, (_o, s, name) in enumerate(src.entries):
        if is_txd(name) and s > 0:
            ns[i] = txd_names_of(src, i)
            no[i] = txd_names_of(out, i, skip)
    _report_names(rep, src, ns, no, "textures(full)")


def check_refs(rep, src, out, ide_files, skip):
    pairs, seen = [], set()
    for path in ide_files:
        for model, txd in parse_ide(path):
            key = (model.lower(), txd.lower())
            if key not in seen:
                seen.add(key)
                pairs.append((model, txd))
    print("\n== refs: %d (model, txd) pairs from %d ide file(s) ==" % (len(pairs), len(ide_files)))
    fs, fo = src.first_index(), out.first_index()
    cache = {}

    def get(kind, arch, idx):
        key = (kind, arch.label, idx)
        if key not in cache:
            if kind == "dff":
                cache[key] = dff_texture_names(arch.read_slot(idx))
            else:
                cache[key] = set(txd_names_of(arch, idx, skip if arch is out else 0))
        return cache[key]

    checked = failures = info_missing = skipped = 0
    for model, txd in pairs:
        dff_name = model.lower() + ".dff"
        txd_name = txd.lower() + ".txd"
        if dff_name not in fs or dff_name not in fo:
            skipped += 1
            continue
        if txd_name not in fs or txd_name not in fo:
            skipped += 1
            rep.info("model %s: txd '%s' not in archive (skipped)" % (model, txd))
            continue
        di_s, di_o = fs[dff_name], fo[dff_name]
        ti_s, ti_o = fs[txd_name], fo[txd_name]
        refs_s = get("dff", src, di_s)
        refs_o = get("dff", out, di_o)
        tx_s = get("txd", src, ti_s)
        tx_o = get("txd", out, ti_o)
        checked += 1
        if refs_s != refs_o:
            rep.info("model %s: dff references differ between src %s and out %s"
                     % (model, refs_s, refs_o))
        for t in sorted(set(refs_s) | set(refs_o)):
            if t not in tx_s:
                info_missing += 1
                rep.info("model %s: texture '%s' already missing in src txd '%s' (info)"
                         % (model, t, txd))
            elif t not in tx_o:
                failures += 1
                if failures <= MAX_LIST:
                    rep.fail("refs: model %s (dff idx %d) texture '%s' exists in src txd '%s' "
                             "(idx %d) but is missing in out txd (idx %d, has %s)"
                             % (model, di_o, t, txd, ti_s, ti_o, sorted(tx_o)))
    if failures > MAX_LIST:
        rep.info("... %d more refs failures" % (failures - MAX_LIST))
    if failures == 0:
        rep.ok("refs: %d models checked, %d skipped (dff/txd not found), "
               "%d refs already missing in src (info)" % (checked, skipped, info_missing))
    else:
        rep.fail("refs: %d missing textures over %d models checked" % (failures, checked))


def check_preserve(rep, src, out, base, dup_txd):
    print("\n== preserve vs base ==")
    if len(base.entries) != len(out.entries):
        print("FAIL  preserve: entry count differs (base %d, out %d)"
              % (len(base.entries), len(out.entries)))
        return False
    names_diff = [i for i in range(len(base.entries))
                  if base.entries[i][2] != out.entries[i][2]]
    if names_diff:
        i = names_diff[0]
        print("FAIL  preserve: %d names differ (first idx %d: base '%s' vs out '%s')"
              % (len(names_diff), i, base.entries[i][2], out.entries[i][2]))
        return False
    bad = [i for i, (o, s, _n) in enumerate(base.entries) if (o + s) * SECTOR > base.img_size]
    if bad:
        print("FAIL  preserve: %d base slots exceed base.img (first idx %d)" % (len(bad), bad[0]))
        return False
    rep.ok("preserve: entry count and name order equal to base (%d)" % len(base.entries))

    bad_size, bad_bytes, differ = [], [], []
    for i in range(len(out.entries)):
        same = slots_equal(out, i, base, i)
        if i in dup_txd:
            if not same:
                differ.append(i)
            continue
        if out.entries[i][1] != base.entries[i][1]:
            bad_size.append(i)
        elif not same:
            bad_bytes.append(i)
    for label, lst in (("size differs from base", bad_size), ("bytes differ from base", bad_bytes)):
        if lst:
            for i in lst[:MAX_LIST]:
                rep.fail("preserve: non-dup idx %d (%s) %s" % (i, out.entries[i][2], label))
            if len(lst) > MAX_LIST:
                rep.info("... %d more (%s)" % (len(lst) - MAX_LIST, label))
    if not bad_size and not bad_bytes:
        rep.ok("preserve: %d non-dup-group-TXD entries identical to base"
               % (len(out.entries) - len(dup_txd)))
    rep.info("dup-group TXD entries differing from base: %d of %d" % (len(differ), len(dup_txd)))
    for i in differ:
        rep.info("  idx %-5d %-22s base %d sec -> out %d sec"
                 % (i, out.entries[i][2], base.entries[i][1], out.entries[i][1]))
    return True


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Regression checker for same-name entries in GTA .img/.dir archives")
    ap.add_argument("--src-dir", required=True)
    ap.add_argument("--src-img", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--out-img", required=True)
    ap.add_argument("--full", action="store_true",
                    help="check texture names of ALL TXD entries (reads whole archives)")
    ap.add_argument("--refs", action="store_true",
                    help="check dff texture references of the models in --ide files")
    ap.add_argument("--ide", action="append", default=[], metavar="FILE")
    ap.add_argument("--preserve", nargs=2, metavar=("B.DIR", "B.IMG"))
    ap.add_argument("--out-prefix-skip", type=int, default=0, metavar="N",
                    help="skip N leading bytes of OUT txds when parsing names (test fakes)")
    args = ap.parse_args(argv)  # usage errors -> exit 2

    if args.refs and not args.ide:
        ap.error("--refs needs at least one --ide FILE")
    if args.ide and not args.refs:
        ap.error("--ide is only used together with --refs")
    paths = [args.src_dir, args.src_img, args.out_dir, args.out_img]
    if args.preserve:
        paths += args.preserve
    paths += args.ide
    for p in paths:
        if not os.path.isfile(p):
            sys.stderr.write("error: file not found: %s\n" % p)
            return 2
    for p in (args.src_dir, args.out_dir) + (tuple(args.preserve[:1]) if args.preserve else ()):
        if os.path.getsize(p) % 32:
            sys.stderr.write("error: %s: size is not a multiple of 32 (not a V1 .dir)\n" % p)
            return 2

    rep = Report()
    src = Archive("src", args.src_dir, args.src_img)
    out = Archive("out", args.out_dir, args.out_img)
    base = Archive("base", *args.preserve) if args.preserve else None
    try:
        print("== structure ==")
        if not check_structure(rep, src, out):
            print("\nRESULT: STRUCTURE ERROR (exit 2): later checks skipped")
            return 2

        groups = img_common.dup_groups(src.entries)
        dup_txd = check_dup_groups(rep, src, out, groups, args.out_prefix_skip)
        if args.full:
            check_full(rep, src, out, args.out_prefix_skip)
        if args.refs:
            check_refs(rep, src, out, args.ide, args.out_prefix_skip)
        if base is not None:
            if not check_preserve(rep, src, out, base, dup_txd):
                print("\nRESULT: STRUCTURE ERROR (exit 2): base archive does not match")
                return 2
    finally:
        src.close()
        out.close()
        if base is not None:
            base.close()

    print()
    if rep.failed:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
