#!/usr/bin/env python3
"""test_img_dup.py - tests for same-name TXD entries in GTA .img archive tools.

Standard library only (unittest + random). Does not need astcenc / PIL: the
converter is driven with a fake txd_compress.py (deterministic transform that
keeps texture names), copied together with the converter script into a temp
directory so the converter's HERE points at it. The same tests therefore run
before and after the fix, without depending on convert_archive().

Usage:
  python3 tools/r36s/tests/test_img_dup.py BugConditionExploration
  python3 tools/r36s/tests/test_img_dup.py            # everything
"""
import hashlib
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTOR = 2048
FAKE_MARKER = b"FAKEASTC"  # 8 bytes, prepended by the fake txd_compress.py
WHITEMODEL = "/tmp/whitemodel"


# --------------------------------------------------------------------------
# Fixtures (reused by later tasks)
# --------------------------------------------------------------------------

def build_txd(names, fill=b"", version=0x1003FFFF):
    """Minimal TEXDICT(0x16) -> TEXTURENATIVE(0x15) stream.

    Each texture gets a STRUCT whose data is: u32 platform, u32 filter,
    char name[32] (offset 8), char mask[32], then `fill`. Starts with 0x16
    and is always >= 64 bytes, so the converter treats it as a TEXDICT.
    """
    if isinstance(names, str):
        names = [names]
    textures = b""
    for n in names:
        sdata = struct.pack("<II", 8, 0x1106)
        sdata += n.encode("ascii").ljust(32, b"\0")
        sdata += b"\0" * 32
        sdata += fill
        struct_chunk = struct.pack("<III", 0x01, len(sdata), version) + sdata
        ext = struct.pack("<III", 0x03, 0, version)
        body = struct_chunk + ext
        textures += struct.pack("<III", 0x15, len(body), version) + body
    dict_struct = struct.pack("<III", 0x01, 4, version) + struct.pack("<HH", len(names), 0)
    body = dict_struct + textures
    return struct.pack("<III", 0x16, len(body), version) + body


def txd_texture_names(data):
    """Lower-cased TEXTURENATIVE names of a TXD (name[32] at STRUCT offset 8).

    Tolerates the fake converter's 8-byte marker prefix and the bogus
    TEXDICT size field found in real converted files: walks top-level
    children up to the end of data, stops on a zero chunk.
    """
    if data.startswith(FAKE_MARKER):
        data = data[len(FAKE_MARKER):]
    out = []
    if len(data) < 12 or struct.unpack_from("<I", data, 0)[0] != 0x16:
        return out
    p = 12
    while p + 12 <= len(data):
        ct, cs, _ = struct.unpack_from("<III", data, p)
        if ct == 0:
            break
        if ct == 0x15 and p + 24 + 40 <= len(data):
            raw = data[p + 24 + 8:p + 24 + 40]
            out.append(raw.split(b"\0")[0].decode("ascii", "replace").lower())
        p += 12 + cs
    return out


def build_archive(entries):
    """entries: [(name, bytes)] -> (dir_bytes, img_bytes).

    32-byte dir entries (u32 sector offset, u32 sector count, char name[24]),
    every entry zero-padded to a 2048-byte sector.
    """
    d = bytearray()
    img = bytearray()
    sector = 0
    for name, data in entries:
        nsec = (len(data) + SECTOR - 1) // SECTOR
        d += struct.pack("<II", sector, nsec) + name.encode("ascii").ljust(24, b"\0")
        img += data + b"\0" * (nsec * SECTOR - len(data))
        sector += nsec
    return bytes(d), bytes(img)


def read_archive(dir_path, img_path):
    """-> [(name, slot_bytes)] in directory order (slot = whole sectors)."""
    with open(dir_path, "rb") as f:
        d = f.read()
    with open(img_path, "rb") as f:
        img = f.read()
    out = []
    for i in range(len(d) // 32):
        off, sz = struct.unpack_from("<II", d, i * 32)
        name = d[i * 32 + 8:i * 32 + 32].split(b"\0")[0].decode("ascii", "replace")
        out.append((name, img[off * SECTOR:(off + sz) * SECTOR]))
    return out


def write_archive(dir_path, img_path, entries):
    d, img = build_archive(entries)
    with open(dir_path, "wb") as f:
        f.write(d)
    with open(img_path, "wb") as f:
        f.write(img)


def md5(b):
    return hashlib.md5(b).hexdigest()


def random_archive(rng, n_unique=12, dup_groups=2, group_size=2, mixed_case=True,
                   ):
    """Random mixed TXD/DFF/COL archive with random same-name TXD groups.

    Returns [(name, bytes)]. Within a dup group every member gets distinct
    content and a distinct texture name. Group members' names differ only in
    case when mixed_case is set.
    """
    entries = []
    for i in range(n_unique):
        kind = rng.choice(["txd", "txd", "dff", "col"])
        base = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(3, 8)))
        if mixed_case and rng.random() < 0.5:
            base = base.capitalize()
        base = "%s%d" % (base, i)
        fill = bytes(rng.randrange(256) for _ in range(rng.randint(0, 3000)))
        if kind == "txd":
            entries.append((base + ".txd", build_txd(["tex_" + base.lower()], fill)))
        elif kind == "dff":
            entries.append((base + ".dff", b"\x10\0\0\0" + fill + b"dff"))
        else:
            entries.append((base + ".col", b"COLL" + fill))
    for g in range(dup_groups):
        base = "dup%d" % g
        pos = [rng.randint(0, len(entries)) for _ in range(group_size)]
        for m, p in enumerate(sorted(pos)):
            nm = base + ".txd"
            if mixed_case and m % 2:
                nm = base.upper() + ".txd"
            fill = bytes(rng.randrange(256) for _ in range(rng.randint(100, 3000)))
            entries.insert(p + m, (nm, build_txd(["%s_tex%d" % (base, m)], fill)))
    return entries


FAKE_TXD_COMPRESS = '''#!/usr/bin/env python3
"""Fake txd_compress.py: out = 8-byte marker + input (keeps texture names)."""
import sys
args = sys.argv[1:]
if len(args) < 2:
    sys.exit(2)
data = open(args[0], "rb").read()
open(args[1], "wb").write(b"FAKEASTC" + data)
'''


def make_tool_dir(parent=None):
    """Temp dir holding img_convert_astc.py (+ img_common.py if present) and
    the fake txd_compress.py. Returns the directory path."""
    tdir = tempfile.mkdtemp(prefix="imgdup_tools_", dir=parent)
    for f in ("img_convert_astc.py", "img_common.py"):
        src = os.path.join(TOOLS, f)
        if os.path.exists(src):
            shutil.copy(src, tdir)
    with open(os.path.join(tdir, "txd_compress.py"), "w") as f:
        f.write(FAKE_TXD_COMPRESS)
    return tdir


def run_converter(tool_dir, indir, inimg, outdir, outimg, jobs=2):
    return subprocess.run(
        [sys.executable, os.path.join(tool_dir, "img_convert_astc.py"),
         indir, inimg, outdir, outimg, "--gles", "1", "-j", str(jobs)],
        capture_output=True, text=True, timeout=120)


def run_tool(script, *args):
    """Run img_rebuild.py / img_patch.py from the repo tools directory."""
    return subprocess.run(
        [sys.executable, os.path.join(TOOLS, script)] + list(args),
        capture_output=True, text=True, timeout=120)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="imgdup_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tool_dir = make_tool_dir(self.tmp)

    def p(self, name):
        return os.path.join(self.tmp, name)

    def make_input(self, entries, stem="gta3"):
        d, i = self.p(stem + ".dir"), self.p(stem + ".img")
        write_archive(d, i, entries)
        return d, i

    def convert(self, entries):
        d, i = self.make_input(entries)
        od, oi = self.p("out.dir"), self.p("out.img")
        r = run_converter(self.tool_dir, d, i, od, oi)
        self.assertEqual(r.returncode, 0, "converter failed:\n%s\n%s" % (r.stdout, r.stderr))
        return read_archive(od, oi)


# --------------------------------------------------------------------------
# Task 1: bug condition exploration (must FAIL on unfixed tools)
# --------------------------------------------------------------------------

def _dup_entries(name_a, name_b):
    """Archive with two same-name TXDs (A != B) among unique entries."""
    a = build_txd(["pgoona"], fill=b"A" * 3000)
    b = build_txd(["Player Hood1"], fill=b"B" * 1500)
    return [
        ("ped1.txd", build_txd(["ped1tex"], fill=b"1" * 500)),
        ("pga.dff", b"\x10\0\0\0" + b"dffA" * 300),
        (name_a, a),
        ("IGmike.txd", build_txd(["mike"], fill=b"m" * 700)),
        ("pga.dff", b"\x10\0\0\0" + b"dffB" * 100),
        ("world.col", b"COLL" + b"c" * 900),
        (name_b, b),
        ("other.txd", build_txd(["other"], fill=b"o" * 100)),
    ], a, b


class BugConditionExploration(TempDirCase):
    """Property 1: same-name TXD entries must keep their own converted content."""

    def _check_collision(self, name_a, name_b):
        entries, a, b = _dup_entries(name_a, name_b)
        out = self.convert(entries)
        idx = [i for i, (n, _) in enumerate(entries) if n.lower() == "pga.txd"]
        self.assertEqual(len(idx), 2)
        s0, s1 = out[idx[0]][1], out[idx[1]][1]
        self.assertNotEqual(
            md5(s0), md5(s1),
            "idx %d and idx %d (%s / %s) have identical converted bytes md5=%s, "
            "textureNames=%r and %r" % (idx[0], idx[1], name_a, name_b, md5(s0),
                                         txd_texture_names(s0), txd_texture_names(s1)))
        self.assertEqual(txd_texture_names(s0), ["pgoona"])
        self.assertEqual(txd_texture_names(s1), ["player hood1"])
        # each slot is the conversion of its own source
        self.assertEqual(s0[:len(FAKE_MARKER) + len(a)], FAKE_MARKER + a)
        self.assertEqual(s1[:len(FAKE_MARKER) + len(b)], FAKE_MARKER + b)

    def test_1_converter_collision(self):
        self._check_collision("pga.txd", "pga.txd")

    def test_2_converter_collision_mixed_case(self):
        self._check_collision("Pga.txd", "PGA.txd")

    def _check_ambiguous(self, script):
        entries, _, _ = _dup_entries("pga.txd", "pga.txd")
        d, i = self.make_input(entries)
        x = self.p("x.txd")
        with open(x, "wb") as f:
            f.write(build_txd(["xtex"], fill=b"X" * 200))
        od, oi = self.p("o.dir"), self.p("o.img")
        r = run_tool(script, d, i, od, oi, "pga.txd=" + x)
        idx = [k for k, (n, _) in enumerate(entries) if n.lower() == "pga.txd"]
        self.assertEqual(r.returncode, 2,
                         "%s: expected exit 2 for ambiguous name, got %d (stdout=%r stderr=%r)"
                         % (script, r.returncode, r.stdout.strip()[-200:], r.stderr.strip()[-200:]))
        for k in idx:
            self.assertRegex(r.stderr, r"(?<!\d)%d(?!\d)" % k,
                             "%s: stderr does not list candidate idx %d" % (script, k))
        self.assertFalse(os.path.exists(od), "%s: output dir created" % script)
        self.assertFalse(os.path.exists(oi), "%s: output img created" % script)

    def test_3_rebuild_ambiguous_replacement(self):
        self._check_ambiguous("img_rebuild.py")

    def test_4_patch_ambiguous_replacement(self):
        self._check_ambiguous("img_patch.py")

    @unittest.skipUnless(
        all(os.path.exists(os.path.join(WHITEMODEL, f)) for f in
            ("c_321.txd", "c_785.txd", "o_321.txd", "o_785.txd")),
        "real data in /tmp/whitemodel not available")
    def test_5_real_data_evidence(self):
        def rd(n):
            with open(os.path.join(WHITEMODEL, n), "rb") as f:
                return f.read()
        c321, c785 = rd("c_321.txd"), rd("c_785.txd")
        self.assertEqual(md5(c321), md5(c785))
        self.assertEqual(txd_texture_names(c321), ["player hood1"])
        self.assertEqual(txd_texture_names(c785), ["player hood1"])
        self.assertIn("pgoona", txd_texture_names(rd("o_321.txd")))
        self.assertIn("player hood1", txd_texture_names(rd("o_785.txd")))


# --------------------------------------------------------------------------
# Task 2: preservation (observed on the UNFIXED tools; must keep passing)
# --------------------------------------------------------------------------

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
BASELINE_FIXTURE = os.path.join(FIXTURE_DIR, "baseline_unfixed.json")


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def det_bytes(tag, n):
    """Deterministic pseudo-content independent of the random module."""
    out = b""
    c = 0
    while len(out) < n:
        out += hashlib.sha256(("%s:%d" % (tag, c)).encode()).digest()
        c += 1
    return out[:n]


def fixed_archive():
    """Fixed small archive WITHOUT same-name TXDs (mixed-case names, a pair of
    same-name DFFs with different content, col/ifp, an empty entry)."""
    return [
        ("ped1.txd", build_txd(["ped1tex"], det_bytes("ped1", 500))),
        ("IGmike.txd", build_txd(["mike"], det_bytes("mike", 2500))),
        ("pga.dff", b"\x10\0\0\0" + det_bytes("dffA", 1200)),
        ("LODhaiti.txd", build_txd(["haiti"], det_bytes("haiti", 4100))),
        ("world.col", b"COLL" + det_bytes("col", 900)),
        ("pga.dff", b"\x10\0\0\0" + det_bytes("dffB", 300)),
        ("walk.ifp", b"ANP3" + det_bytes("ifp", 2100)),
        ("empty.txd", b""),
        ("other.txd", build_txd(["other", "other2"], det_bytes("other", 3000))),
    ]


def ref_convert(entries):
    """Name-keyed reference of the unfixed converter. Valid ONLY when no two
    entries share a (case-insensitive) name: TXD with sz>0 and a TEXDICT
    header (>= 64 bytes) -> FAKE marker + whole source slot; others copied."""
    out = []
    for name, data in entries:
        nsec = (len(data) + SECTOR - 1) // SECTOR
        slot = data + b"\0" * (nsec * SECTOR - len(data))
        if (name.lower().endswith(".txd") and nsec and len(slot) >= 64
                and slot[:4] == b"\x16\0\0\0"):
            out.append((name, FAKE_MARKER + slot))
        else:
            out.append((name, slot))
    return out


def ref_rebuild(entries, repl):
    """repl: {lower_name: bytes}. Sequential rebuild, entries may grow/shrink."""
    return build_archive([(n, repl.get(n.lower(), d)) for n, d in entries])


def ref_patch(entries, repl):
    """In-place patch: dir unchanged, replaced slot = new bytes + zero pad."""
    d, img = build_archive(entries)
    img = bytearray(img)
    pos = 0
    for n, data in entries:
        nsec = (len(data) + SECTOR - 1) // SECTOR
        if n.lower() in repl:
            new = repl[n.lower()]
            assert len(new) <= nsec * SECTOR
            img[pos:pos + nsec * SECTOR] = new + b"\0" * (nsec * SECTOR - len(new))
        pos += nsec * SECTOR
    return d, bytes(img)


def no_dup_archive(rng, n_unique=12):
    """Random archive with NO same-name entries, mixed case allowed; always
    contains the mixed-case TXDs IGmike.txd / LODhaiti.txd."""
    entries = random_archive(rng, n_unique=n_unique, dup_groups=0, mixed_case=True)
    for nm, tex, size in (("IGmike.txd", "mike", rng.randint(0, 2500)),
                          ("LODhaiti.txd", "haiti", rng.randint(0, 2500))):
        fill = bytes(rng.randrange(256) for _ in range(size))
        entries.insert(rng.randint(0, len(entries)), (nm, build_txd([tex], fill)))
    names = [n.lower() for n, _ in entries]
    assert len(names) == len(set(names))
    return entries


def parse_dir(d):
    out = []
    for i in range(len(d) // 32):
        off, sz = struct.unpack_from("<II", d, i * 32)
        out.append((off, sz, d[i * 32 + 8:i * 32 + 32].split(b"\0")[0].decode("ascii")))
    return out


class PreservationBaseline(TempDirCase):
    """Property 2: entries / CLI usages outside the bug condition keep their
    observed (unfixed) behaviour. Reference = name-keyed implementation, valid
    only for archives without same-name entries."""

    ROUNDS = 6

    def _tool_out(self, script, entries, repl_args, repl_files):
        d, i = self.make_input(entries)
        od, oi = self.p("o.dir"), self.p("o.img")
        for f in (od, oi):
            if os.path.exists(f):
                os.unlink(f)
        paths = []
        for k, (arg_name, data) in enumerate(zip(repl_args, repl_files)):
            fp = self.p("repl%d.bin" % k)
            with open(fp, "wb") as f:
                f.write(data)
            paths.append("%s=%s" % (arg_name, fp))
        r = run_tool(script, d, i, od, oi, *paths)
        return r, od, oi

    def _convert_raw(self, entries):
        d, i = self.make_input(entries)
        od, oi = self.p("c.dir"), self.p("c.img")
        r = run_converter(self.tool_dir, d, i, od, oi)
        self.assertEqual(r.returncode, 0, "converter failed:\n%s\n%s" % (r.stdout, r.stderr))
        with open(od, "rb") as f:
            cd = f.read()
        with open(oi, "rb") as f:
            ci = f.read()
        return cd, ci

    # ---- observations on a fixed archive (examples) ----------------------

    def test_observed_fixed_archive_converter(self):
        entries = fixed_archive()
        out = self.convert(entries)
        self.assertEqual([n for n, _ in out], [n for n, _ in entries])
        for (name, data), (_, slot) in zip(entries, out):
            nsec = (len(data) + SECTOR - 1) // SECTOR
            src_slot = data + b"\0" * (nsec * SECTOR - len(data))
            low = name.lower()
            if low.endswith(".txd") and data:
                self.assertNotEqual(slot[:len(src_slot)], src_slot, name + " left unconverted")
                self.assertEqual(txd_texture_names(slot), txd_texture_names(data), name)
            else:
                self.assertEqual(slot, src_slot, name + " must be copied byte-for-byte")
        # same-name DFFs with different content stay byte-identical to source
        dffs = [(n, s) for (n, s) in out if n == "pga.dff"]
        self.assertEqual(len(dffs), 2)
        self.assertNotEqual(dffs[0][1], dffs[1][1])

    def test_observed_fixed_archive_structure(self):
        entries = fixed_archive()
        cd, ci = self._convert_raw(entries)
        parsed = parse_dir(cd)
        self.assertEqual([n for _, _, n in parsed], [n for n, _ in entries])
        self.assertEqual(parsed[0][0], 0)
        for a, b in zip(parsed, parsed[1:]):
            self.assertEqual(b[0], a[0] + a[1])
        self.assertEqual(len(ci), sum(sz for _, sz, _ in parsed) * SECTOR)

    def test_observed_patch_rejects_oversize(self):
        entries = fixed_archive()
        d, i = self.make_input(entries)
        big = self.p("big.bin")
        with open(big, "wb") as f:
            f.write(b"x" * (SECTOR * 2 + 1))  # ped1.txd slot is 1 sector (500B+headers < 2048)
        od, oi = self.p("o.dir"), self.p("o.img")
        slot_sectors = (len(entries[0][1]) + SECTOR - 1) // SECTOR
        self.assertLess(slot_sectors, 3)
        r = run_tool("img_patch.py", d, i, od, oi, "ped1.txd=" + big)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("slot", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(oi))
        self.assertFalse(os.path.exists(od))

    def test_observed_rebuild_grow_shrink_structure(self):
        entries = fixed_archive()
        repl = {"igmike.txd": b"G" * 7000,       # grow
                "world.col": b"s" * 10}          # shrink
        r, od, oi = self._tool_out("img_rebuild.py", entries,
                                   ["IGMIKE.TXD", "world.col"],
                                   [repl["igmike.txd"], repl["world.col"]])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(od, "rb") as f:
            d = f.read()
        with open(oi, "rb") as f:
            img = f.read()
        parsed = parse_dir(d)
        self.assertEqual([n for _, _, n in parsed], [n for n, _ in entries])
        self.assertEqual(parsed[0][0], 0)
        for a, b in zip(parsed, parsed[1:]):
            self.assertEqual(b[0], a[0] + a[1])
        self.assertEqual(len(img), sum(sz for _, sz, _ in parsed) * SECTOR)
        self.assertEqual((d, img), ref_rebuild(entries, repl))

    # ---- property-style tests (fixed seeds) -------------------------------

    def test_prop_a_converter_equals_name_keyed_reference(self):
        for seed in range(100, 100 + self.ROUNDS):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                entries = no_dup_archive(rng, n_unique=rng.randint(4, 14))
                cd, ci = self._convert_raw(entries)
                rd, ri = build_archive(ref_convert(entries))
                self.assertEqual(cd, rd)
                self.assertEqual(ci, ri)

    def test_prop_b_non_txd_entries_copied_byte_for_byte(self):
        for seed in range(200, 200 + self.ROUNDS):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                entries = no_dup_archive(rng, n_unique=rng.randint(4, 14))
                # same-name DFF pair (not in the replacement table) must also survive
                entries.insert(rng.randint(0, len(entries)),
                               ("twin.dff", b"\x10\0\0\0" + b"A" * rng.randint(1, 3000)))
                entries.insert(rng.randint(0, len(entries)),
                               ("Twin.dff", b"\x10\0\0\0" + b"B" * rng.randint(1, 3000)))
                out = self.convert(entries)
                self.assertEqual(len(out), len(entries))
                checked = 0
                for (name, data), (oname, slot) in zip(entries, out):
                    self.assertEqual(name, oname)
                    if not name.lower().endswith(".txd"):
                        nsec = (len(data) + SECTOR - 1) // SECTOR
                        self.assertEqual(slot, data + b"\0" * (nsec * SECTOR - len(data)), name)
                        checked += 1
                self.assertGreaterEqual(checked, 2)

    def test_prop_c_unique_name_replacement_matches_reference(self):
        for seed in range(300, 300 + self.ROUNDS):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                entries = no_dup_archive(rng, n_unique=rng.randint(4, 14))
                picks = rng.sample(entries, k=min(3, len(entries)))
                # rebuild: arbitrary sizes, case-varied argument names
                rb_repl, args, files = {}, [], []
                for name, _ in picks:
                    data = bytes(rng.randrange(256) for _ in range(rng.randint(0, 6000)))
                    arg = name.upper() if rng.random() < 0.5 else name.lower()
                    rb_repl[name.lower()] = data
                    args.append(arg)
                    files.append(data)
                r, od, oi = self._tool_out("img_rebuild.py", entries, args, files)
                self.assertEqual(r.returncode, 0, r.stderr)
                with open(od, "rb") as f:
                    d = f.read()
                with open(oi, "rb") as f:
                    img = f.read()
                self.assertEqual((d, img), ref_rebuild(entries, rb_repl))
                # patch: replacement must fit the slot
                p_repl, args, files = {}, [], []
                for name, data in picks:
                    cap = ((len(data) + SECTOR - 1) // SECTOR) * SECTOR
                    new = bytes(rng.randrange(256) for _ in range(rng.randint(0, cap)))
                    p_repl[name.lower()] = new
                    args.append(name.upper())
                    files.append(new)
                r, od, oi = self._tool_out("img_patch.py", entries, args, files)
                self.assertEqual(r.returncode, 0, r.stderr)
                with open(od, "rb") as f:
                    d = f.read()
                with open(oi, "rb") as f:
                    img = f.read()
                self.assertEqual((d, img), ref_patch(entries, p_repl))

    def test_prop_d_mixed_case_txds_are_converted(self):
        for seed in range(400, 400 + self.ROUNDS):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                entries = no_dup_archive(rng, n_unique=rng.randint(4, 14))
                out = self.convert(entries)
                mixed = 0
                for (name, data), (_, slot) in zip(entries, out):
                    if name.lower().endswith(".txd") and data and name != name.lower():
                        mixed += 1
                        self.assertTrue(slot.startswith(FAKE_MARKER), name + " not converted")
                        self.assertNotEqual(slot[:len(data)], data, name)
                self.assertGreaterEqual(mixed, 2)  # IGmike.txd + LODhaiti.txd at least

    # ---- sha256 baseline fixture ------------------------------------------

    def _observe_fixed(self):
        """Run the three tools on the fixed archive; return a sha256 summary."""
        entries = fixed_archive()
        sd, si = build_archive(entries)

        def summarize(d, img):
            return {
                "dir_sha256": sha256(d),
                "img_sha256": sha256(img),
                "img_bytes": len(img),
                "entry_sha256": [sha256(img[o * SECTOR:(o + s) * SECTOR])
                                 for o, s, _ in parse_dir(d)],
            }

        summary = {
            "description": "sha256 of outputs of the UNFIXED img tools on a fixed "
                           "archive (fake txd_compress: FAKEASTC marker + input)",
            "names": [n for n, _ in entries],
            "source": summarize(sd, si),
        }
        cd, ci = self._convert_raw(entries)
        summary["converter"] = summarize(cd, ci)

        grow = det_bytes("repl-grow", 7000)
        shrink = det_bytes("repl-shrink", 10)
        r, od, oi = self._tool_out("img_rebuild.py", entries,
                                   ["igmike.txd", "world.col"], [grow, shrink])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(od, "rb") as f:
            d = f.read()
        with open(oi, "rb") as f:
            img = f.read()
        summary["rebuild_igmike_grow_world_shrink"] = summarize(d, img)

        r, od, oi = self._tool_out("img_patch.py", entries,
                                   ["IGmike.txd", "world.col"],
                                   [det_bytes("repl-fit", 1800), shrink])
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(od, "rb") as f:
            d = f.read()
        with open(oi, "rb") as f:
            img = f.read()
        summary["patch_igmike_fit_world_shrink"] = summarize(d, img)
        return summary

    def test_prop_e_fixed_archive_sha256_matches_baseline(self):
        import json
        observed = self._observe_fixed()
        if os.environ.get("PRESERVATION_RECORD") == "1":
            os.makedirs(FIXTURE_DIR, exist_ok=True)
            with open(BASELINE_FIXTURE, "w") as f:
                json.dump(observed, f, indent=2, sort_keys=True)
                f.write("\n")
            print("PRESERVATION_RECORD=1: wrote %s" % BASELINE_FIXTURE, file=sys.stderr)
            return
        if not os.path.exists(BASELINE_FIXTURE):
            self.skipTest("baseline fixture %s is absent; record it ONCE on the unfixed "
                          "tools with PRESERVATION_RECORD=1" % BASELINE_FIXTURE)
        with open(BASELINE_FIXTURE) as f:
            baseline = json.load(f)
        self.assertEqual(observed, baseline)


# --------------------------------------------------------------------------
# Task 3.5: full dff texture reference parsing + IDE parsing (img_dup_check)
# --------------------------------------------------------------------------

def _load_dup_check():
    """Import img_dup_check by path, independent of the current directory."""
    import importlib.util
    path = os.path.join(TOOLS, "img_dup_check.py")
    spec = importlib.util.spec_from_file_location("img_dup_check_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rw_chunk(ctype, payload=b"", size=None):
    """RenderWare chunk header (type, size, version) + payload."""
    if size is None:
        size = len(payload)
    return struct.pack("<III", ctype, size, 0x1003FFFF) + payload


def rw_string(s):
    raw = s.encode("ascii") + b"\0"
    raw += b"\0" * ((-len(raw)) % 4)  # RW pads strings to 4 bytes
    return rw_chunk(0x02, raw)


def rw_texture(name, mask=""):
    return rw_chunk(0x06, rw_chunk(0x01, b"\x06\x11\x00\x00")   # Struct (filter/addressing)
                    + rw_string(name) + rw_string(mask) + rw_chunk(0x03))


def rw_material(tex=None, mask="", ext=b""):
    body = rw_chunk(0x01, b"\0" * 28)  # Struct
    if tex is not None:
        body += rw_texture(tex, mask)
    body += rw_chunk(0x03, ext)       # Extension
    return rw_chunk(0x07, body)


def rw_geometry(materials, ext=b""):
    mlist = rw_chunk(0x08, rw_chunk(0x01, struct.pack("<I", len(materials))
                                    + b"\xff\xff\xff\xff" * len(materials))
                     + b"".join(materials))
    return rw_chunk(0x0F, rw_chunk(0x01, b"\0" * 56) + mlist + rw_chunk(0x03, ext))


def rw_dff(geometries, clump_ext=b""):
    glist = rw_chunk(0x1A, rw_chunk(0x01, struct.pack("<I", len(geometries)))
                     + b"".join(geometries))
    body = (rw_chunk(0x01, struct.pack("<I", len(geometries)))
            + rw_chunk(0x0E, rw_chunk(0x01, b"\0" * 4))      # FrameList
            + glist
            + rw_chunk(0x14, rw_chunk(0x01, b"\0" * 16))     # Atomic
            + rw_chunk(0x03, clump_ext))
    return rw_chunk(0x10, body)


class DffRefsParsing(unittest.TestCase):
    """Task 3.5: dff_texture_names() and parse_ide() of img_dup_check."""

    @classmethod
    def setUpClass(cls):
        cls.m = _load_dup_check()

    def names(self, data):
        return self.m.dff_texture_names(data)

    # ---- dff_texture_names ----

    def test_single_material(self):
        self.assertEqual(self.names(rw_dff([rw_geometry([rw_material("gang1")])])), ["gang1"])

    def test_multiple_materials_and_geometries_in_order(self):
        dff = rw_dff([
            rw_geometry([rw_material("a"), rw_material("b"), rw_material("c")]),
            rw_geometry([rw_material("d")]),
            rw_geometry([rw_material("e"), rw_material("f")]),
        ])
        self.assertEqual(self.names(dff), ["a", "b", "c", "d", "e", "f"])

    def test_material_without_texture_adds_nothing(self):
        dff = rw_dff([rw_geometry([rw_material(None), rw_material("x"), rw_material(None)])])
        self.assertEqual(self.names(dff), ["x"])
        self.assertEqual(self.names(rw_dff([rw_geometry([rw_material(None)])])), [])
        self.assertEqual(self.names(rw_dff([rw_geometry([])])), [])
        self.assertEqual(self.names(rw_dff([])), [])

    def test_mask_string_is_not_returned(self):
        dff = rw_dff([rw_geometry([rw_material("body", mask="body_mask")])])
        self.assertEqual(self.names(dff), ["body"])

    def test_empty_texture_name_skipped(self):
        dff = rw_dff([rw_geometry([rw_material(""), rw_material("real", mask="m")])])
        self.assertEqual(self.names(dff), ["real"])

    def test_case_folded_to_lower(self):
        dff = rw_dff([rw_geometry([rw_material("Player_HOOD1"), rw_material("PGoona")])])
        self.assertEqual(self.names(dff), ["player_hood1", "pgoona"])

    def test_nested_extensions_with_lookalike_payloads_are_skipped(self):
        # payloads that look exactly like Texture / Material / Geometry chunks
        decoy_tex = rw_texture("decoy_tex")
        decoy_mat = rw_material("decoy_mat")
        decoy_geo = rw_geometry([rw_material("decoy_geo")])
        nested_ext = rw_chunk(0x03, decoy_tex + decoy_mat + decoy_geo)
        dff = rw_dff(
            [rw_geometry([rw_material("real1", ext=decoy_tex + nested_ext),
                          rw_material("real2", ext=decoy_mat)],
                         ext=decoy_mat + decoy_geo + nested_ext)],
            clump_ext=rw_chunk(0x1A, decoy_geo) + nested_ext + decoy_geo)
        self.assertEqual(self.names(dff), ["real1", "real2"])

    def test_struct_payload_that_looks_like_chunks_is_not_entered(self):
        fake = rw_texture("in_struct") + rw_material("in_struct2")
        mat = rw_chunk(0x07, rw_chunk(0x01, fake) + rw_texture("outside"))
        mlist = rw_chunk(0x08, rw_chunk(0x01, fake) + mat)
        geo = rw_chunk(0x0F, rw_chunk(0x01, fake) + mlist)
        dff = rw_chunk(0x10, rw_chunk(0x01, fake)
                       + rw_chunk(0x1A, rw_chunk(0x01, fake) + geo))
        self.assertEqual(self.names(dff), ["outside"])

    def test_non_dff_and_truncated_data_return_empty_without_exception(self):
        good = rw_dff([rw_geometry([rw_material("t1"), rw_material("t2")])])
        for bad in (b"", b"\0", b"\x10\0\0\0", b"not a dff at all" * 5,
                    build_txd(["pgoona"], fill=b"x" * 100),
                    rw_chunk(0x16, rw_chunk(0x15)), rw_chunk(0x0F, b"\0" * 64)):
            self.assertEqual(self.names(bad), [], repr(bad[:16]))
        # every prefix of a valid dff parses without raising and never invents names
        # (a string cut mid-way is clamped, so it may come back as a prefix of a name)
        for n in range(len(good)):
            out = self.names(good[:n])
            self.assertIsInstance(out, list)
            self.assertTrue(all(o in ("t", "t1", "t2") for o in out), (n, out))
            self.assertLessEqual(len(out), 2)
        self.assertEqual(self.names(good), ["t1", "t2"])

    def test_chunk_size_larger_than_data_is_clamped(self):
        good = rw_dff([rw_geometry([rw_material("big")])])
        # Clump header claims far more data than the file holds
        huge = struct.pack("<III", 0x10, 0xFFFFFFF0, 0x1003FFFF) + good[12:]
        self.assertEqual(self.names(huge), ["big"])
        # same for the String chunk itself (name is still read, clamped to data)
        tex = rw_chunk(0x06, rw_chunk(0x02, b"clamped\0", size=0x7FFFFFFF))
        dff = rw_chunk(0x10, rw_chunk(0x1A, rw_chunk(0x0F, rw_chunk(0x08, rw_chunk(0x07, tex)))),
                       size=0x7FFFFFFF)
        self.assertEqual(self.names(dff), ["clamped"])

    def test_garbage_terminates_and_does_not_raise(self):
        rnd = random.Random(20240601)
        for _ in range(300):
            n = rnd.randrange(12, 400)
            body = bytes(rnd.randrange(256) for _ in range(n))
            data = struct.pack("<III", 0x10, rnd.choice([n, 0, 0xFFFFFFFF]), 0) + body
            self.assertIsInstance(self.names(data), list)
        # zero-size chunk soup and all-0xFF bytes must advance, not loop
        self.assertEqual(self.names(struct.pack("<III", 0x10, 0x1000, 0) + b"\0" * 0x1000), [])
        self.assertEqual(self.names(struct.pack("<III", 0x10, 0x100, 0) + b"\xff" * 0x100), [])
        # self-nested clumps / geometry lists are bounded
        deep = rw_chunk(0x10, b"")
        for _ in range(200):
            deep = rw_chunk(0x10, deep)
        self.assertEqual(self.names(deep), [])
        deep = rw_texture("leaf")
        for t in (0x07, 0x08, 0x0F, 0x1A) * 50:
            deep = rw_chunk(t, deep)
        self.assertIsInstance(self.names(rw_chunk(0x10, deep)), list)

    # ---- parse_ide ----

    def _ide(self, text):
        fd, path = tempfile.mkstemp(suffix=".ide", prefix="imgdup_")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return self.m.parse_ide(path)

    def test_pga_line_yields_model_and_txd(self):
        got = self._ide("peds\n95, PGa, PGa, GANG7, STAT_GANG7, gang1, 0, null, 9,9\nend\n")
        self.assertEqual(got, [("PGa", "PGa")])

    def test_sections_comments_blank_and_non_numeric_lines(self):
        text = "\n".join([
            "# header comment",
            "",
            "objs",
            "  # indented comment",
            "1000, obj1, generic, 1, 100, 0   # trailing comment",
            "end",
            "2dfx",                         # unrelated section: numeric-looking lines skipped
            "5, 1.0, 2.0, 3.0, 0, 0",
            "end",
            "peds",
            "\t",
            "abc, notanumber, skip",         # non-numeric first field
            "id, model, txd",                # column-title style line
            "1, cop, cop, COP, STAT_COP, cop, 0, null, 9,9",
            "2, 3, ",                        # missing txd
            "3, , x",                        # empty model
            "end",
            "path",
            "ped, 3, somename",              # unrelated section, non-numeric
            "7, 8, 9",                       # unrelated section, numeric
            "end",
            "cars",
            "150, landstal, landstal, car, LANDSTAL, LANDSTAL, null, richfamily, 10, 0, 0, -1, 0.7, 0.7, 0",
            "end",
            ""])
        self.assertEqual(self._ide(text),
                         [("obj1", "generic"), ("cop", "cop"), ("landstal", "landstal")])

    def test_file_without_section_header(self):
        got = self._ide("1, m1, t1, x\n# c\n\nbad, m2, t2\n2, m3, t3\n")
        self.assertEqual(got, [("m1", "t1"), ("m3", "t3")])

    def test_cars_section_layout_and_section_reset(self):
        text = "cars\n130, firetruk, firetruk, car, FIRETRUK, FIRETRUK, truck, null, 10, 0\n" \
               "end\n9, after_end, tex_after_end\n"
        # after `end` the section is closed, but a header-less numeric line is still accepted
        self.assertEqual(self._ide(text),
                         [("firetruk", "firetruk"), ("after_end", "tex_after_end")])


# --------------------------------------------------------------------------
# Task 3.7: unit tests + fake-converter integration tests
# --------------------------------------------------------------------------

import contextlib  # noqa: E402
import io  # noqa: E402
from unittest import mock  # noqa: E402

if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import img_common  # noqa: E402
import img_convert_astc  # noqa: E402


def pad_sec(b):
    return b + b"\0" * (-len(b) % SECTOR)


def expected_converted_slot(name, data):
    """Slot the (fake) converter must produce for a source entry: TXDs with a
    TEXDICT header become FAKE_MARKER + whole source slot, others are copied."""
    slot = pad_sec(data)
    if name.lower().endswith(".txd") and data and len(slot) >= 64 and slot[:4] == b"\x16\0\0\0":
        return pad_sec(FAKE_MARKER + slot)
    return slot


def md5_file(path):
    with open(path, "rb") as f:
        return md5(f.read())


def fake_tool_dir(parent, script_text):
    """Directory holding only a txd_compress.py with the given content."""
    d = tempfile.mkdtemp(prefix="imgdup_fake_", dir=parent)
    with open(os.path.join(d, "txd_compress.py"), "w") as f:
        f.write(script_text)
    return d


# constant output for every input -> merges same-name entries on purpose
CONST_TXD_COMPRESS = '''import sys
open(sys.argv[2], "wb").write(b"FAKEASTC" + b"CONSTANT" * 400)
'''
# differs on every call -> breaks identical same-name entries on purpose
RANDOM_TXD_COMPRESS = '''import os, sys
data = open(sys.argv[1], "rb").read()
open(sys.argv[2], "wb").write(b"FAKEASTC" + os.urandom(16) + data)
'''
FAILING_TXD_COMPRESS = '''import sys
sys.stderr.write("simulated conversion failure\\n")
sys.exit(1)
'''


def synth_src_entries():
    """Source archive with: a 2-member TXD group (mixed case), a 3-member TXD
    group, a TXD pair with IDENTICAL content, a DFF pair (different content),
    unique TXDs (mixed case), an empty TXD, col/ifp entries."""
    same = build_txd(["same_tex"], det_bytes("same", 1800))
    return [
        ("ped1.txd", build_txd(["ped1tex"], det_bytes("s_ped1", 500))),                   # 0
        ("Pga.txd", build_txd(["pgoona"], det_bytes("s_pgaA", 3000))),                    # 1
        ("pga.dff", b"\x10\0\0\0" + det_bytes("s_dffA", 1200)),                           # 2
        ("IGmike.txd", build_txd(["mike"], det_bytes("s_mike", 2500))),                   # 3
        ("pga.dff", b"\x10\0\0\0" + det_bytes("s_dffB", 300)),                            # 4
        ("PGA.txd", build_txd(["Player Hood1"], det_bytes("s_pgaB", 1500))),              # 5
        ("world.col", b"COLL" + det_bytes("s_col", 900)),                                 # 6
        ("LODhaiti.txd", build_txd(["haiti"], det_bytes("s_haiti", 4100))),               # 7
        ("generic.txd", build_txd(["gen_a", "gen_b"], det_bytes("s_gen1", 5000))),        # 8
        ("empty.txd", b""),                                                               # 9
        ("Generic.txd", build_txd(["gen_c"], det_bytes("s_gen2", 1000))),                 # 10
        ("chef.txd", build_txd(["chef0"], det_bytes("s_chef0", 800))),                    # 11
        ("other.txd", build_txd(["other"], det_bytes("s_other", 300))),                   # 12
        ("CHEF.txd", build_txd(["chef1"], det_bytes("s_chef1", 2400))),                   # 13
        ("Same.txd", same),                                                               # 14
        ("Chef.txd", build_txd(["chef2"], det_bytes("s_chef2", 100))),                    # 15
        ("same.txd", same),                                                               # 16
        ("walk.ifp", b"ANP3" + det_bytes("s_ifp", 2100)),                                 # 17
    ]


SYNTH_DUP_TXD_IDX = [1, 5, 8, 10, 11, 13, 14, 15, 16]  # members of TXD groups (sz > 0)


def names_of(entries):
    return [(0, 0, n) for n, _ in entries]


# --------------------------------------------------------------------------
# ImgCommonUnit
# --------------------------------------------------------------------------

class ImgCommonUnit(unittest.TestCase):
    """img_common: resolve_replacements, dup_groups, assert_dup_identity,
    txd_texture_names, directory entry encoding."""

    # (off, sz, name): "pga.txd" occurs 3 times with different case
    ENTRIES = [(0, 2, "ped1.txd"), (2, 3, "pga.txd"), (5, 1, "PGA.txd"),
               (6, 4, "world.col"), (10, 7, "Pga.TXD"), (17, 1, "IGmike.txd")]

    def resolve(self, args, entries=None):
        return img_common.resolve_replacements(entries or self.ENTRIES, args)

    def resolve_exit(self, args, entries=None, code=2):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                self.resolve(args, entries)
        self.assertEqual(cm.exception.code, code)
        return err.getvalue()

    # ---- resolve_replacements ----

    def test_resolve_unique_name(self):
        self.assertEqual(self.resolve(["ped1.txd=/x/a.txd", "world.col=b"]),
                         {0: "/x/a.txd", 3: "b"})

    def test_resolve_name_is_case_insensitive(self):
        self.assertEqual(self.resolve(["IGMIKE.TXD=f"]), {5: "f"})
        self.assertEqual(self.resolve(["Ped1.Txd=f"]), {0: "f"})

    def test_resolve_ambiguity_lists_all_candidates_and_hint(self):
        err = self.resolve_exit(["pga.txd=f"])
        for idx, (off, sz, nm) in ((1, self.ENTRIES[1]), (2, self.ENTRIES[2]),
                                   (4, self.ENTRIES[4])):
            self.assertRegex(
                err, r"idx %d +%s +sector offset %d +sector size %d" % (idx, re.escape(nm), off, sz))
        self.assertIn("idx:N=file", err)
        self.assertNotIn("ped1.txd", err)  # only the candidates are listed

    def test_resolve_ambiguity_wins_over_valid_args_and_reports_every_problem(self):
        err = self.resolve_exit(["ped1.txd=ok", "PGA.TXD=f", "idx:99=g", "noequals"])
        self.assertIn("ambiguous", err)
        self.assertIn("idx 1 ", err)
        self.assertIn("idx 4 ", err)
        self.assertIn("out of range", err)
        self.assertIn("noequals", err)

    def test_resolve_idx_selects_exactly_one_member_of_a_group(self):
        self.assertEqual(self.resolve(["idx:2=f"]), {2: "f"})
        self.assertEqual(self.resolve(["idx:1=a", "idx:4=b", "ped1.txd=c"]),
                         {1: "a", 4: "b", 0: "c"})

    def test_resolve_idx_out_of_range(self):
        n = len(self.ENTRIES)
        self.assertEqual(self.resolve(["idx:%d=f" % (n - 1)]), {n - 1: "f"})
        err = self.resolve_exit(["idx:%d=f" % n])
        self.assertIn("out of range", err)
        self.assertIn("idx %d" % n, err)
        self.resolve_exit(["idx:99999999999=f"])

    def test_resolve_same_idx_twice(self):
        self.assertIn("twice", self.resolve_exit(["idx:1=a", "idx:1=b"]))
        # idx + name resolving to the same unique entry
        err = self.resolve_exit(["idx:0=a", "ped1.txd=b"])
        self.assertIn("twice", err)
        self.resolve_exit(["PED1.TXD=a", "idx:0=b"])
        self.resolve_exit(["ped1.txd=a", "PED1.txd=b"])

    def test_resolve_unmatched_name_is_ignored(self):
        self.assertEqual(self.resolve(["nosuch.txd=f"]), {})
        self.assertEqual(self.resolve(["nosuch.txd=f", "idx:3=g"]), {3: "g"})
        self.assertEqual(self.resolve([]), {})

    def test_resolve_bad_argument_without_equals(self):
        self.assertIn("noequals", self.resolve_exit(["noequals"]))
        self.resolve_exit(["idx:3"])
        self.resolve_exit(["ped1.txd"])

    def test_resolve_path_containing_equals_splits_on_first(self):
        entries = [(0, 1, "name"), (1, 1, "other")]
        self.assertEqual(self.resolve(["name=a=b"], entries), {0: "a=b"})
        self.assertEqual(self.resolve(["idx:1=a=b=c"], entries), {1: "a=b=c"})
        self.assertEqual(self.resolve(["name==x"], entries), {0: "=x"})

    # ---- dup_groups ----

    def test_dup_groups_basic(self):
        self.assertEqual(img_common.dup_groups(self.ENTRIES), {"pga.txd": [1, 2, 4]})
        self.assertEqual(img_common.dup_groups([]), {})
        self.assertEqual(img_common.dup_groups(self.ENTRIES[:1]), {})

    def test_dup_groups_includes_dff_groups_and_is_case_insensitive(self):
        entries = names_of([("a.txd", b""), ("A.TXD", b""), ("x.dff", b""), ("b.col", b""),
                            ("X.dff", b""), ("a.Txd", b""), ("x.dff", b""), ("uniq.txd", b"")])
        self.assertEqual(img_common.dup_groups(entries),
                         {"a.txd": [0, 1, 5], "x.dff": [2, 4, 6]})

    def test_dup_groups_synthetic_archive(self):
        groups = img_common.dup_groups(names_of(synth_src_entries()))
        self.assertEqual(groups, {"pga.txd": [1, 5], "pga.dff": [2, 4], "generic.txd": [8, 10],
                                  "chef.txd": [11, 13, 15], "same.txd": [14, 16]})

    # ---- assert_dup_identity ----

    ENT2 = [(0, 1, "a.txd"), (1, 1, "A.txd"), (2, 1, "solo.txd")]
    ENT3 = [(0, 1, "a.txd"), (1, 1, "A.txd"), (2, 1, "A.TXD"), (3, 1, "solo.txd")]

    def test_identity_ok_cases(self):
        for before, after in ((["a", "b", "s"], ["x", "y", "s"]),
                              (["a", "a", "s"], ["x", "x", "q"]),
                              (["a", "b", "s"], ["y", "x", "s"])):
            self.assertEqual(img_common.assert_dup_identity(self.ENT2, before, after), [])
        self.assertEqual(img_common.assert_dup_identity([], [], []), [])
        self.assertEqual(img_common.assert_dup_identity([(0, 1, "x.txd")], ["a"], ["b"]), [])

    def test_identity_equal_before_different_after(self):
        v = img_common.assert_dup_identity(self.ENT2, ["a", "a", "s"], ["x", "y", "s"])
        self.assertEqual(len(v), 1)
        self.assertIn("idx 0", v[0])
        self.assertIn("idx 1", v[0])
        self.assertIn("identical in source but differ", v[0])

    def test_identity_different_before_equal_after(self):
        v = img_common.assert_dup_identity(self.ENT2, ["a", "b", "s"], ["x", "x", "s"])
        self.assertEqual(len(v), 1)
        self.assertIn("merged", v[0])

    def test_identity_three_members(self):
        ent = self.ENT3
        ok = img_common.assert_dup_identity(ent, ["a", "b", "b", "s"], ["x", "y", "y", "s"])
        self.assertEqual(ok, [])
        # 0 and 1 merged, 2 still apart: only the pairs (0,1) breaks
        v = img_common.assert_dup_identity(ent, ["a", "b", "c", "s"], ["x", "x", "y", "s"])
        self.assertEqual(len(v), 1)
        self.assertIn("idx 0", v[0])
        self.assertIn("idx 1", v[0])
        # everything merged: 3 pairs
        v = img_common.assert_dup_identity(ent, ["a", "b", "c", "s"], ["x", "x", "x", "s"])
        self.assertEqual(len(v), 3)
        # two equal sources split apart, third equal: pairs (0,2) and (1,2)
        v = img_common.assert_dup_identity(ent, ["a", "a", "a", "s"], ["x", "x", "y", "s"])
        self.assertEqual(len(v), 2)

    def test_identity_accepts_dicts_and_ignores_unique_entries(self):
        before = {0: "a", 1: "b", 2: "s"}
        after = {0: "x", 1: "x", 2: "other"}
        v = img_common.assert_dup_identity(self.ENT2, before, after)
        self.assertEqual(len(v), 1)
        self.assertNotIn("solo", v[0])

    def test_identity_covers_dff_groups(self):
        ent = [(0, 1, "t.dff"), (1, 1, "T.dff")]
        self.assertEqual(len(img_common.assert_dup_identity(ent, ["a", "b"], ["x", "x"])), 1)

    # ---- txd_texture_names ----

    NAMES = ["PGoona", "Player Hood1", "z" * 32]

    @staticmethod
    def gl3ish(names, fill=b""):
        """build_txd output with platform=12 (GL3) patched into every STRUCT."""
        data = bytearray(build_txd(names, fill))
        p = 12
        while p + 12 <= len(data):
            ct, cs, _ = struct.unpack_from("<III", data, p)
            if ct == 0x15:
                struct.pack_into("<I", data, p + 24, 12)
            p += 12 + cs
        return bytes(data)

    def test_names_d3d8_layout(self):
        got = img_common.txd_texture_names(build_txd(self.NAMES, b"\xaa" * 70))
        self.assertEqual(got, ["pgoona", "player hood1", "z" * 32])

    def test_names_gl3_layout(self):
        data = self.gl3ish(self.NAMES, b"\x55" * 300)
        # first TEXTURENATIVE STRUCT starts at 12 + 16 (dict struct) + 24: platform field patched
        self.assertEqual(struct.unpack_from("<I", data, 12 + 16 + 24)[0], 12)
        self.assertEqual(img_common.txd_texture_names(data),
                         ["pgoona", "player hood1", "z" * 32])

    def test_names_empty_txd_and_non_txd(self):
        self.assertEqual(img_common.txd_texture_names(build_txd([])), [])
        for bad in (b"", b"\x16\0\0\0", b"\x16" + b"\0" * 10,
                    b"\x10\0\0\0" + b"\0" * 60,                       # dff-like
                    b"COLL" + b"\0" * 100,
                    rw_dff([rw_geometry([rw_material("t")])]),
                    struct.pack("<III", 0x16, 0, 0)):                  # TXD header, no children
            self.assertEqual(img_common.txd_texture_names(bad), [], repr(bad[:12]))

    def test_names_truncated_at_every_prefix(self):
        for label, full in (("d3d8", build_txd(self.NAMES, b"\xaa" * 20)),
                            ("gl3", self.gl3ish(self.NAMES, b"\x55" * 20))):
            want = ["pgoona", "player hood1", "z" * 32]
            seen = set()
            for n in range(len(full) + 1):
                got = img_common.txd_texture_names(full[:n])  # must not raise
                self.assertEqual(got, want[:len(got)], (label, n))
                seen.add(len(got))
            self.assertEqual(seen, {0, 1, 2, 3}, label)
            self.assertEqual(img_common.txd_texture_names(full), want)

    def test_names_bogus_texdict_size_field_is_ignored(self):
        for bogus in (16, 0, 0xFFFFFFFF, 12345):
            data = bytearray(build_txd(self.NAMES, b"q" * 50))
            struct.pack_into("<I", data, 4, bogus)
            self.assertEqual(img_common.txd_texture_names(bytes(data)),
                             ["pgoona", "player hood1", "z" * 32], bogus)

    # ---- directory entry encoding ----

    def test_directory_entry_encoding(self):
        entries = [("a.txd", b"x" * 5000),               # 3 sectors
                   ("b.dff", b"y"),                      # 1 sector
                   ("empty.col", b""),                   # 0 sectors, offset stays
                   ("abcdefghijklmnopqrstuvwx", b"z" * 2048),   # 24-char name, no NUL
                   ("IGmike.txd", b"w" * 2049)]          # 2 sectors
        d, img = build_archive(entries)
        self.assertEqual(len(d), 32 * len(entries))
        offs = [0, 3, 4, 4, 5]
        szs = [3, 1, 0, 1, 2]
        for i, (name, _data) in enumerate(entries):
            raw = d[i * 32:(i + 1) * 32]
            self.assertEqual(struct.unpack_from("<II", raw, 0), (offs[i], szs[i]))
            self.assertEqual(raw[8:], name.encode("ascii").ljust(24, b"\0"))
            self.assertEqual(len(raw[8:]), 24)
        self.assertEqual(len(img), 7 * SECTOR)
        tmp = tempfile.mkdtemp(prefix="imgdup_direnc_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "x.dir")
        with open(path, "wb") as f:
            f.write(d)
        self.assertEqual(img_common.read_dir(path),
                         [(offs[i], szs[i], n) for i, (n, _) in enumerate(entries)])
        self.assertEqual(img_common.read_dir(path), img_convert_astc.read_dir(path))


# --------------------------------------------------------------------------
# archive-level fixtures for the CLI / integration classes
# --------------------------------------------------------------------------

class DupArchiveCase(TempDirCase):
    def assert_structure(self, dpath, ipath, names):
        with open(dpath, "rb") as f:
            parsed = parse_dir(f.read())
        self.assertEqual([n for _o, _s, n in parsed], names)
        exp = 0
        for i, (off, sz, _n) in enumerate(parsed):
            self.assertEqual(off, exp, "idx %d offset" % i)
            exp = off + sz
        self.assertEqual(os.path.getsize(ipath), exp * SECTOR)

    def converted_out(self, entries, stem="out"):
        """Run the (fixed) converter with the fake txd_compress; -> (sd, si, od, oi)."""
        sd, si = self.make_input(entries, "src")
        od, oi = self.p(stem + ".dir"), self.p(stem + ".img")
        r = run_converter(self.tool_dir, sd, si, od, oi)
        self.assertEqual(r.returncode, 0, "converter failed:\n%s\n%s" % (r.stdout, r.stderr))
        return sd, si, od, oi

    def merged_base(self, entries, stem="base"):
        """Device-like 'merged' base: unique non-empty TXDs fake-converted, every
        member of every same-name TXD group holds ONE shared payload (written
        through img_rebuild.py idx:N=file). -> (bd, bi)."""
        cnt = {}
        for n, _ in entries:
            cnt[n.lower()] = cnt.get(n.lower(), 0) + 1
        pre = []
        for n, data in entries:
            if n.lower().endswith(".txd") and data and cnt[n.lower()] == 1:
                pre.append((n, FAKE_MARKER + pad_sec(data)))
            else:
                pre.append((n, data))
        pd, pi = self.make_input(pre, stem + "_pre")
        specs = []
        for lname, idxs in sorted(img_common.dup_groups(names_of(entries)).items()):
            if not lname.endswith(".txd"):
                continue
            fp = self.p("%s_merged_%s.bin" % (stem, lname))
            with open(fp, "wb") as f:
                f.write(FAKE_MARKER + pad_sec(entries[idxs[0]][1]))
            specs += ["idx:%d=%s" % (i, fp) for i in idxs]
        bd, bi = self.p(stem + ".dir"), self.p(stem + ".img")
        r = run_tool("img_rebuild.py", pd, pi, bd, bi, *specs)
        self.assertEqual(r.returncode, 0, r.stderr)
        return bd, bi

    def variant(self, od, oi, stem, changes):
        """Copy of archive od/oi with slots replaced: changes = {idx: slot_bytes}."""
        lst = read_archive(od, oi)
        for idx, slot in changes.items():
            lst[idx] = (lst[idx][0], slot)
        return self.make_input(lst, stem)

    def check(self, sd, si, od, oi, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(TOOLS, "img_dup_check.py"),
             "--src-dir", sd, "--src-img", si, "--out-dir", od, "--out-img", oi] + list(extra),
            capture_output=True, text=True, timeout=120)


# --------------------------------------------------------------------------
# ImgDupCheckCli
# --------------------------------------------------------------------------

class ImgDupCheckCli(DupArchiveCase):
    SKIP = ["--out-prefix-skip", "8"]

    def setUp(self):
        super().setUp()
        self.entries = synth_src_entries()
        self.sd, self.si, self.od, self.oi = self.converted_out(self.entries)

    def test_correct_conversion_passes(self):
        bd, bi = self.merged_base(self.entries)
        r = self.check(self.sd, self.si, self.od, self.oi, *self.SKIP, "--preserve", bd, bi)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("RESULT: PASS", r.stdout)
        self.assertIn("PASS  identity", r.stdout)
        self.assertIn("PASS  preserve", r.stdout)
        self.assertNotIn("FAIL", r.stdout)
        # the group report lists the idx of both Pga.txd entries with their names
        self.assertRegex(r.stdout, r"idx 1 +Pga\.txd")
        self.assertRegex(r.stdout, r"idx 5 +PGA\.txd")
        self.assertIn("pgoona", r.stdout)

    def test_merged_output_fails(self):
        lst = read_archive(self.od, self.oi)
        od, oi = self.variant(self.od, self.oi, "merged", {5: lst[1][1]})  # copy 1 into 5
        r = self.check(self.sd, self.si, od, oi, *self.SKIP)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("FAIL  identity", r.stdout)
        self.assertIn("merged", r.stdout)
        self.assertIn("RESULT: FAIL", r.stdout)

    def test_split_identical_entries_fails(self):
        # Same.txd / same.txd are identical in the source; make them differ in
        # the output while keeping their texture names
        names = img_common.txd_texture_names(self.entries[14][1])
        other = pad_sec(FAKE_MARKER + build_txd(names, b"different"))
        od, oi = self.variant(self.od, self.oi, "split", {14: other})
        r = self.check(self.sd, self.si, od, oi, *self.SKIP)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("identical in source but differ", r.stdout)
        self.assertNotIn("FAIL  textures", r.stdout)

    def test_texture_name_changed_fails(self):
        changed = pad_sec(FAKE_MARKER + build_txd(["wrongname"], b"z" * 3000))
        od, oi = self.variant(self.od, self.oi, "renamed", {1: changed})
        r = self.check(self.sd, self.si, od, oi, *self.SKIP)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("FAIL  textures(dup groups)", r.stdout)
        self.assertIn("idx 1", r.stdout)
        self.assertIn("PASS  identity", r.stdout)  # contents are still all different

    def test_without_prefix_skip_fake_marker_hides_names(self):
        # sanity of the test knob: the 8-byte fake marker makes names unreadable
        r = self.check(self.sd, self.si, self.od, self.oi)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL  textures", r.stdout)

    def test_truncated_img_is_structure_error(self):
        oi = self.p("trunc.img")
        with open(self.oi, "rb") as f:
            data = f.read()
        with open(oi, "wb") as f:
            f.write(data[:-SECTOR])
        r = self.check(self.sd, self.si, self.od, oi, *self.SKIP)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("len(out.img)", r.stdout)
        self.assertIn("STRUCTURE ERROR", r.stdout)

    def test_entry_count_mismatch_is_structure_error(self):
        od = self.p("short.dir")
        with open(self.od, "rb") as f:
            d = f.read()
        with open(od, "wb") as f:
            f.write(d[:-32])
        r = self.check(self.sd, self.si, od, self.oi, *self.SKIP)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("entry count differs", r.stdout)

    def test_name_order_mismatch_is_structure_error(self):
        with open(self.od, "rb") as f:
            d = bytearray(f.read())
        a, b = d[8:32], d[12 * 32 + 8:12 * 32 + 32]
        d[8:32], d[12 * 32 + 8:12 * 32 + 32] = b, a
        od = self.p("swapped.dir")
        with open(od, "wb") as f:
            f.write(bytes(d))
        r = self.check(self.sd, self.si, od, self.oi, *self.SKIP)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("names differ", r.stdout)

    def test_non_contiguous_offsets_are_structure_error(self):
        with open(self.od, "rb") as f:
            d = bytearray(f.read())
        off = struct.unpack_from("<I", d, 3 * 32)[0]
        struct.pack_into("<I", d, 3 * 32, off + 1)
        od = self.p("gap.dir")
        with open(od, "wb") as f:
            f.write(bytes(d))
        r = self.check(self.sd, self.si, od, self.oi, *self.SKIP)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("not contiguous", r.stdout)

    def test_usage_errors_exit_2(self):
        r = self.check(self.sd, self.si, self.od, self.p("missing.img"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("not found", r.stderr)
        r = self.check(self.sd, self.si, self.od, self.oi, "--refs")  # no --ide
        self.assertEqual(r.returncode, 2)
        r = subprocess.run([sys.executable, os.path.join(TOOLS, "img_dup_check.py")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)

    # ---- --full ----

    def test_full_mode(self):
        r = self.check(self.sd, self.si, self.od, self.oi, *self.SKIP, "--full")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("PASS  textures(full)", r.stdout)
        # a UNIQUE txd renamed: invisible in fast mode, caught by --full
        changed = pad_sec(FAKE_MARKER + build_txd(["wrongname"], b"o" * 200))
        od, oi = self.variant(self.od, self.oi, "uniqbad", {12: changed})
        r = self.check(self.sd, self.si, od, oi, *self.SKIP)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("textures(full)", r.stdout)
        r = self.check(self.sd, self.si, od, oi, *self.SKIP, "--full")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL  textures(full)", r.stdout)
        self.assertIn("other.txd", r.stdout)

    # ---- --refs ----

    IDE = ("# peds\npeds\n"
           "95, pga, pga, GANG7, STAT_GANG7, gang1, 0, null, 9,9\n"
           "96, ped1, ped1, CIVMALE, STAT_STREET, man, 0, null, 9,9\n"
           "97, ghost, ghost, CIVMALE, STAT_STREET, man, 0, null, 9,9\n"
           "end\n")

    def refs_entries(self):
        return [
            ("ped1.dff", rw_dff([rw_geometry([rw_material("ped1tex"), rw_material("extra_missing")])])),
            ("ped1.txd", build_txd(["ped1tex"], det_bytes("r_ped1", 500))),
            ("pga.dff", rw_dff([rw_geometry([rw_material("PGoona")])])),
            ("pga.txd", build_txd(["pgoona"], det_bytes("r_pgaA", 3000))),
            ("pga.txd", build_txd(["Player Hood1"], det_bytes("r_pgaB", 1500))),
            ("pga.dff", rw_dff([rw_geometry([rw_material("player hood1")])])),  # not the first
        ]

    def refs_setup(self):
        entries = self.refs_entries()
        sd, si, od, oi = self.converted_out(entries, "refsout")
        ide = self.p("peds.ide")
        with open(ide, "w") as f:
            f.write(self.IDE)
        return entries, sd, si, od, oi, ide

    def test_refs_pass(self):
        _e, sd, si, od, oi, ide = self.refs_setup()
        r = self.check(sd, si, od, oi, *self.SKIP, "--refs", "--ide", ide)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PASS  refs", r.stdout)
        self.assertIn("refs: 3 (model, txd) pairs", r.stdout)
        # reference missing in the source already: info only
        self.assertIn("extra_missing", r.stdout)
        self.assertIn("already missing in src", r.stdout)

    def test_refs_fail_when_unique_txd_loses_a_texture(self):
        _e, sd, si, od, oi, ide = self.refs_setup()
        bad = pad_sec(FAKE_MARKER + build_txd(["somethingelse"], b"p" * 100))
        od2, oi2 = self.variant(od, oi, "refsbad", {1: bad})
        # ped1.txd is not in a dup group: only --refs notices
        r = self.check(sd, si, od2, oi2, *self.SKIP)
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.check(sd, si, od2, oi2, *self.SKIP, "--refs", "--ide", ide)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL  refs", r.stdout)
        self.assertIn("ped1tex", r.stdout)

    def test_refs_fail_when_first_pga_txd_is_merged(self):
        _e, sd, si, od, oi, ide = self.refs_setup()
        lst = read_archive(od, oi)
        od2, oi2 = self.variant(od, oi, "refsmerged", {3: lst[4][1]})  # first pga.txd := second
        r = self.check(sd, si, od2, oi2, *self.SKIP, "--refs", "--ide", ide)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL  refs: model pga", r.stdout)
        self.assertIn("pgoona", r.stdout)
        self.assertIn("FAIL  identity", r.stdout)


# --------------------------------------------------------------------------
# ImgDupFixEndToEnd
# --------------------------------------------------------------------------

class ImgDupFixEndToEnd(DupArchiveCase):
    def setUp(self):
        super().setUp()
        self.entries = synth_src_entries()
        self.sd, self.si = self.make_input(self.entries, "src")
        self.bd, self.bi = self.merged_base(self.entries)
        self.od, self.oi = self.p("fixed.dir"), self.p("fixed.img")

    def run_fix(self, tools=None, extra=(), bd=None, bi=None, sd=None, si=None):
        return subprocess.run(
            [sys.executable, os.path.join(TOOLS, "img_dup_fix.py"),
             "--base-dir", bd or self.bd, "--base-img", bi or self.bi,
             "--src-dir", sd or self.sd, "--src-img", si or self.si,
             "--out-dir", self.od, "--out-img", self.oi,
             "-j", "2", "--tools-dir", tools or self.tool_dir] + list(extra),
            capture_output=True, text=True, timeout=120)

    def assert_no_outputs(self):
        for f in (self.od, self.oi, self.od + ".partial", self.oi + ".partial"):
            self.assertFalse(os.path.exists(f), f + " exists")
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".partial")], [])

    def test_base_fixture_is_really_merged(self):
        base = read_archive(self.bd, self.bi)
        self.assertEqual(base[1][1], base[5][1])
        self.assertEqual(base[11][1], base[13][1])
        self.assertEqual(base[8][1], base[10][1])
        self.assertNotEqual(self.entries[1][1], self.entries[5][1])

    def test_targets_updated_others_identical(self):
        hashes_before = [md5_file(p) for p in (self.bd, self.bi, self.sd, self.si)]
        for p in (self.oi + ".partial", self.od + ".partial"):  # stale partials from an earlier run
            with open(p, "wb") as f:
                f.write(b"junk")
        r = self.run_fix(extra=["--spot-check", "8"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("%d target TXD entries" % len(SYNTH_DUP_TXD_IDX), r.stdout)
        self.assertIn("spot check: 4 match, 0 mismatch", r.stdout)
        self.assertIn("IGmike.txd: match", r.stdout)
        self.assertIn("LODhaiti.txd: match", r.stdout)

        base = read_archive(self.bd, self.bi)
        out = read_archive(self.od, self.oi)
        self.assertEqual([n for n, _ in out], [n for n, _ in self.entries])
        for i, (name, slot) in enumerate(out):
            if i in SYNTH_DUP_TXD_IDX:
                # exactly FAKE_MARKER + its OWN source slot (zero padded)
                self.assertEqual(slot, expected_converted_slot(name, self.entries[i][1]), "idx %d" % i)
                self.assertTrue(slot.startswith(FAKE_MARKER + pad_sec(self.entries[i][1])))
            else:
                self.assertEqual(slot, base[i][1], "non-target idx %d (%s) changed" % (i, name))
        # texture names come from each entry's own source
        self.assertEqual(img_common.txd_texture_names(out[1][1][len(FAKE_MARKER):]), ["pgoona"])
        self.assertEqual(img_common.txd_texture_names(out[5][1][len(FAKE_MARKER):]), ["player hood1"])
        # identity: equal sources stay equal, different sources stay different
        for i in SYNTH_DUP_TXD_IDX:
            for j in SYNTH_DUP_TXD_IDX:
                if i < j and self.entries[i][0].lower() == self.entries[j][0].lower():
                    self.assertEqual(self.entries[i][1] == self.entries[j][1], out[i][1] == out[j][1],
                                     (i, j))
        self.assertEqual(out[14][1], out[16][1])
        # structure
        self.assert_structure(self.od, self.oi, [n for n, _ in self.entries])
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith(".partial")], [])
        # inputs untouched
        self.assertEqual(hashes_before,
                         [md5_file(p) for p in (self.bd, self.bi, self.sd, self.si)])
        # and the independent checker agrees
        c = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "img_dup_check.py"),
             "--src-dir", self.sd, "--src-img", self.si, "--out-dir", self.od, "--out-img", self.oi,
             "--out-prefix-skip", "8", "--preserve", self.bd, self.bi, "--full"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(c.returncode, 0, c.stdout + c.stderr)

    def test_name_mismatch_exits_2(self):
        with open(self.bd, "rb") as f:
            d = bytearray(f.read())
        d[3 * 32 + 8:4 * 32] = b"Other.txd".ljust(24, b"\0")
        bd = self.p("badnames.dir")
        with open(bd, "wb") as f:
            f.write(bytes(d))
        r = self.run_fix(bd=bd)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("different names", r.stderr)
        self.assertIn("idx 3", r.stderr)
        self.assert_no_outputs()

    def test_entry_count_mismatch_exits_2(self):
        with open(self.bd, "rb") as f:
            d = f.read()
        bd = self.p("short.dir")
        with open(bd, "wb") as f:
            f.write(d[:-32])
        r = self.run_fix(bd=bd)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("entry count differs", r.stderr)
        self.assert_no_outputs()

    def test_missing_txd_compress_exits_2(self):
        r = self.run_fix(tools=tempfile.mkdtemp(prefix="imgdup_empty_", dir=self.tmp))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("txd_compress.py not found", r.stderr)
        self.assert_no_outputs()

    def test_conversion_failure_exits_1_without_outputs(self):
        r = self.run_fix(tools=fake_tool_dir(self.tmp, FAILING_TXD_COMPRESS))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("convert-failed", r.stderr)
        self.assert_no_outputs()

    def test_spot_check_mismatch_exits_1_without_outputs(self):
        base = read_archive(self.bd, self.bi)
        tampered = bytearray(base[3][1])  # IGmike.txd (unique) in base
        tampered[100] ^= 0xFF
        bd, bi = self.variant(self.bd, self.bi, "tampered", {3: bytes(tampered)})
        r = self.run_fix(bd=bd, bi=bi, extra=["--spot-check", "8"])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("idx 3 IGmike.txd: MISMATCH", r.stdout)
        self.assertIn("spot check mismatch", r.stderr)
        self.assert_no_outputs()
        # without --spot-check the same tampered base is simply copied through
        r = self.run_fix(bd=bd, bi=bi)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read_archive(self.od, self.oi)[3][1], bytes(tampered))


# --------------------------------------------------------------------------
# ConvertArchiveDirect
# --------------------------------------------------------------------------

def direct_entries():
    return [
        ("ped1.txd", build_txd(["ped1tex"], det_bytes("d_ped1", 400))),                  # 0
        ("Pga.txd", build_txd(["pgoona"], det_bytes("d_pgaA", 3000))),                   # 1
        ("twin.dff", b"\x10\0\0\0" + det_bytes("d_dffA", 700)),                          # 2
        ("IGmike.txd", build_txd(["mike"], det_bytes("d_mike", 2500))),                  # 3
        ("PGA.txd", build_txd(["Player Hood1"], det_bytes("d_pgaB", 1500))),             # 4
        ("twin.dff", b"\x10\0\0\0" + det_bytes("d_dffB", 300)),                          # 5
        ("chef.txd", build_txd(["chef0"], det_bytes("d_chef0", 800))),                   # 6
        ("Chef.txd", build_txd(["chef1"], det_bytes("d_chef1", 2300))),                  # 7
        ("same.txd", build_txd(["same"], det_bytes("d_same", 900))),                     # 8
        ("CHEF.txd", build_txd(["chef2"], det_bytes("d_chef2", 60))),                    # 9
        ("SAME.txd", build_txd(["same"], det_bytes("d_same", 900))),                     # 10
        ("world.col", b"COLL" + det_bytes("d_col", 900)),                                # 11
    ]


class ConvertArchiveDirect(DupArchiveCase):
    """img_convert_astc.convert_archive() called in-process with a fake tools_dir."""

    def setUp(self):
        super().setUp()
        self.work = self.p("work")
        os.mkdir(self.work)
        old = tempfile.tempdir
        tempfile.tempdir = self.work  # converter temp files must not leak
        self.addCleanup(setattr, tempfile, "tempdir", old)
        self.entries = direct_entries()
        self.sd, self.si = self.make_input(self.entries)
        self.od, self.oi = self.p("o.dir"), self.p("o.img")

    def run_convert(self, jobs=2, tools_dir=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = img_convert_astc.convert_archive(
                self.sd, self.si, self.od, self.oi, 1, jobs, tools_dir or self.tool_dir)
        return rc, out.getvalue(), err.getvalue()

    def assert_no_archive(self):
        self.assertFalse(os.path.exists(self.od))
        self.assertFalse(os.path.exists(self.oi))

    def test_duplicates_keep_their_own_content(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                for f in (self.od, self.oi):
                    if os.path.exists(f):
                        os.unlink(f)
                rc, out, err = self.run_convert(jobs)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("3 duplicate TXD groups (7 entries), keyed by directory index", out)
                self.assertIn("1 duplicate DFF groups are copied verbatim", out)
                got = read_archive(self.od, self.oi)
                self.assertEqual([n for n, _ in got], [n for n, _ in self.entries])
                for i, ((name, data), (_n, slot)) in enumerate(zip(self.entries, got)):
                    self.assertEqual(slot, expected_converted_slot(name, data), "idx %d %s" % (i, name))
                # mixed-case dup names: all three chef.txd differ, both same.txd equal
                self.assertEqual(len({got[6][1], got[7][1], got[9][1]}), 3)
                self.assertEqual(got[8][1], got[10][1])
                self.assertNotEqual(got[1][1], got[4][1])
                self.assertEqual(img_common.txd_texture_names(got[1][1][8:]), ["pgoona"])
                self.assertEqual(img_common.txd_texture_names(got[4][1][8:]), ["player hood1"])
                self.assertNotEqual(got[2][1], got[5][1])  # DFF twins untouched
                self.assert_structure(self.od, self.oi, [n for n, _ in self.entries])
                self.assertEqual(os.listdir(self.work), [])  # converter temp files cleaned up

    def test_real_merge_by_converter_is_rejected(self):
        # a converter that returns the SAME output for different inputs merges
        # the same-name entries: the guard must refuse to write the archive
        rc, out, err = self.run_convert(2, fake_tool_dir(self.tmp, CONST_TXD_COMPRESS))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("same-name identity check failed, archive NOT written", err)
        self.assertIn("merged", err)
        self.assertIn("idx 1", err)
        self.assertIn("idx 4", err)
        self.assert_no_archive()
        self.assertEqual(os.listdir(self.work), [])

    def test_real_split_by_converter_is_rejected(self):
        # nondeterministic converter: byte-identical same.txd entries come out different
        rc, out, err = self.run_convert(1, fake_tool_dir(self.tmp, RANDOM_TXD_COMPRESS))
        self.assertEqual(rc, 1, out + err)
        self.assertIn("identical in source but differ", err)
        self.assert_no_archive()

    def test_forced_violation_via_patched_guard(self):
        with mock.patch.object(img_common, "assert_dup_identity",
                               return_value=["forced violation"]) as m:
            rc, out, err = self.run_convert(1)
        self.assertEqual(rc, 1, out + err)
        m.assert_called_once()
        entries_arg, before, after = m.call_args[0]
        self.assertEqual(len(entries_arg), len(self.entries))
        self.assertIn("forced violation", err)
        self.assertIn("NOT written", err)
        self.assert_no_archive()
        self.assertEqual(os.listdir(self.work), [])

    def test_guard_is_called_with_md5_of_slots_per_group_member(self):
        with mock.patch.object(img_common, "assert_dup_identity", return_value=[]) as m:
            rc, out, err = self.run_convert(1)
        self.assertEqual(rc, 0, out + err)
        _entries, before, after = m.call_args[0]
        members = {1, 4, 2, 5, 6, 7, 9, 8, 10}
        self.assertEqual(set(before), members)
        self.assertEqual(set(after), members)
        self.assertNotEqual(before[1], before[4])
        self.assertEqual(before[8], before[10])
        self.assertEqual(after[8], after[10])
        self.assertNotEqual(after[1], after[4])
        self.assertEqual(before[2], md5(pad_sec(self.entries[2][1])))  # DFF: after == before
        self.assertEqual(after[2], before[2])


# --------------------------------------------------------------------------
# RebuildPatchIdx
# --------------------------------------------------------------------------

class RebuildPatchIdx(DupArchiveCase):
    def setUp(self):
        super().setUp()
        self.entries = [
            ("ped1.txd", build_txd(["ped1tex"], det_bytes("p_ped1", 500))),     # 0
            ("pga.txd", build_txd(["pgoona"], det_bytes("p_pgaA", 3000))),      # 1  (2 sectors)
            ("world.col", b"COLL" + det_bytes("p_col", 5000)),                  # 2
            ("Pga.txd", build_txd(["Player Hood1"], det_bytes("p_pgaB", 1000))),  # 3  (1 sector)
            ("other.txd", build_txd(["other"], det_bytes("p_other", 300))),     # 4
        ]
        self.sd, self.si = self.make_input(self.entries)
        self.od, self.oi = self.p("o.dir"), self.p("o.img")
        with open(self.si, "rb") as f:
            self.src_img = f.read()
        self.src_slots = [pad_sec(d) for _n, d in self.entries]

    def write(self, name, data):
        fp = self.p(name)
        with open(fp, "wb") as f:
            f.write(data)
        return fp

    def read_out(self):
        with open(self.od, "rb") as f:
            d = f.read()
        with open(self.oi, "rb") as f:
            img = f.read()
        return d, img

    # ---- rebuild ----

    def test_rebuild_idx_replaces_only_that_entry_grow(self):
        big = det_bytes("grow", 5000)
        r = run_tool("img_rebuild.py", self.sd, self.si, self.od, self.oi,
                     "idx:3=" + self.write("big.bin", big))
        self.assertEqual(r.returncode, 0, r.stderr)
        d, img = self.read_out()
        expect = [(n, big if i == 3 else data) for i, (n, data) in enumerate(self.entries)]
        self.assertEqual((d, img), build_archive(expect))
        out = read_archive(self.od, self.oi)
        for i in (0, 1, 2, 4):  # sibling pga.txd and the rest byte-identical
            self.assertEqual(out[i][1], self.src_slots[i], i)
        self.assert_structure(self.od, self.oi, [n for n, _ in self.entries])
        # offsets after the grown entry moved up by 2 sectors
        parsed = parse_dir(d)
        self.assertEqual(parsed[4][0], parse_dir(build_archive(self.entries)[0])[4][0] + 2)

    def test_rebuild_idx_shrink_and_both_members_and_unique_name(self):
        small = b"tiny"
        mid = det_bytes("mid", 2049)
        r = run_tool("img_rebuild.py", self.sd, self.si, self.od, self.oi,
                     "idx:1=" + self.write("small.bin", small),
                     "idx:3=" + self.write("mid.bin", mid),
                     "OTHER.TXD=" + self.write("o.bin", b"o" * 10))
        self.assertEqual(r.returncode, 0, r.stderr)
        expect = [(n, d) for n, d in self.entries]
        expect[1] = (expect[1][0], small)
        expect[3] = (expect[3][0], mid)
        expect[4] = (expect[4][0], b"o" * 10)
        self.assertEqual(self.read_out(), build_archive(expect))
        out = read_archive(self.od, self.oi)
        self.assertEqual(out[1][1], pad_sec(small))
        self.assertEqual(out[3][1], pad_sec(mid))
        self.assertEqual(len(out[3][1]), 2 * SECTOR)
        self.assert_structure(self.od, self.oi, [n for n, _ in self.entries])

    def test_rebuild_idx_equals_unique_name(self):
        f = self.write("x.bin", det_bytes("x", 777))
        run_tool("img_rebuild.py", self.sd, self.si, self.od, self.oi, "other.txd=" + f)
        by_name = self.read_out()
        os.unlink(self.od)
        os.unlink(self.oi)
        run_tool("img_rebuild.py", self.sd, self.si, self.od, self.oi, "idx:4=" + f)
        self.assertEqual(self.read_out(), by_name)

    # ---- patch ----

    def test_patch_idx_replaces_only_that_entry(self):
        new = det_bytes("fit", 900)  # fits in idx 3's slot (1 sector)
        r = run_tool("img_patch.py", self.sd, self.si, self.od, self.oi,
                     "idx:3=" + self.write("fit.bin", new))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[3]", r.stdout)
        d, img = self.read_out()
        self.assertEqual(d, build_archive(self.entries)[0])  # directory unchanged
        out = read_archive(self.od, self.oi)
        for i in (0, 1, 2, 4):
            self.assertEqual(out[i][1], self.src_slots[i], i)
        self.assertEqual(out[3][1], new + b"\0" * (SECTOR - len(new)))
        self.assertEqual(len(img), len(self.src_img))

    def test_patch_idx_capacity_is_per_entry_and_oversize_is_rejected(self):
        new = det_bytes("cap", 3000)  # fits idx 1 (2 sectors), too big for idx 3 (1 sector)
        f = self.write("cap.bin", new)
        r = run_tool("img_patch.py", self.sd, self.si, self.od, self.oi, "idx:1=" + f)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(read_archive(self.od, self.oi)[1][1], new + b"\0" * (2 * SECTOR - len(new)))
        os.unlink(self.od)
        os.unlink(self.oi)
        r = run_tool("img_patch.py", self.sd, self.si, self.od, self.oi, "idx:3=" + f)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("slot", r.stdout + r.stderr)
        self.assertIn("[3]", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.od))
        self.assertFalse(os.path.exists(self.oi))

    # ---- rejected invocations leave no output ----

    def test_invalid_specs_leave_no_output_files(self):
        f = self.write("f.bin", b"f" * 100)
        g = self.write("g.bin", b"g" * 100)
        cases = {
            "ambiguous": ["pga.txd=" + f],
            "ambiguous+valid": ["idx:2=" + f, "PGA.TXD=" + f],
            "valid+ambiguous order": ["other.txd=" + g, "pga.txd=" + f],
            "idx out of range": ["idx:5=" + f],
            "idx twice": ["idx:1=" + f, "idx:1=" + g],
            "idx and name twice": ["idx:4=" + f, "other.txd=" + g],
            "no equals": ["pga.txd"],
        }
        for script in ("img_rebuild.py", "img_patch.py"):
            for label, args in cases.items():
                with self.subTest(script=script, case=label):
                    r = run_tool(script, self.sd, self.si, self.od, self.oi, *args)
                    self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                    self.assertFalse(os.path.exists(self.od), "out dir created")
                    self.assertFalse(os.path.exists(self.oi), "out img created")
                    self.assertEqual(r.stdout, "")
            with self.subTest(script=script, case="stderr lists candidates"):
                r = run_tool(script, self.sd, self.si, self.od, self.oi, "pga.txd=" + f)
                for k in (1, 3):
                    self.assertRegex(r.stderr, r"idx %d " % k)
                self.assertIn("idx:N=file", r.stderr)

    def test_unmatched_name_is_still_ignored_by_both_tools(self):
        f = self.write("f.bin", b"f")
        for script in ("img_rebuild.py", "img_patch.py"):
            with self.subTest(script=script):
                r = run_tool(script, self.sd, self.si, self.od, self.oi, "nosuch.txd=" + f)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.read_out(), build_archive(self.entries))
                os.unlink(self.od)
                os.unlink(self.oi)


if __name__ == "__main__":
    unittest.main(verbosity=2)
