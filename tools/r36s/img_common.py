#!/usr/bin/env python3
"""img_common.py - shared helpers for the GTA .img/.dir (V1) archive tools.

Used by img_rebuild.py, img_patch.py, img_dup_check.py and img_dup_fix.py.
Python 3 standard library only.

Why this exists: gta3.dir contains names that appear more than once (7 TXD
names, 14 entries; plus 4 DFF names). Tools must therefore key everything by
directory ENTRY INDEX, never by name. Names are only used for human input
(`name=file`, accepted when it matches exactly one entry) and for logging.
"""
import re
import struct
import sys

SECTOR = 2048

_IDX_ARG = re.compile(r"^idx:(\d+)=(.*)$", re.S)


def read_dir(path):
    """Read a gta3.dir -> [(off, sz, name)] in directory order.

    32-byte entries: u32 sector offset, u32 sector count, char name[24]
    (NUL-terminated ascii at entry offset 8).
    """
    with open(path, "rb") as f:
        d = f.read()
    entries = []
    for i in range(len(d) // 32):
        off, sz = struct.unpack_from("<II", d, i * 32)
        name = d[i * 32 + 8:i * 32 + 32].split(b"\0")[0].decode("ascii", "replace")
        entries.append((off, sz, name))
    return entries


def dup_groups(entries):
    """-> {lower_name: [idx, ...]}, only groups with more than one member."""
    groups = {}
    for idx, (_off, _sz, name) in enumerate(entries):
        groups.setdefault(name.lower(), []).append(idx)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _die(msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(2)


def resolve_replacements(entries, args):
    """Parse `name=file` / `idx:N=file` arguments -> {idx: path}.

    - `idx:N=file` (regex ^idx:(\\d+)=(.*)$, tried first): replaces entry N.
      N out of range -> exit 2.
    - `name=file` (split on the first '='; name matched case-insensitively):
        * matches 0 entries -> ignored (historic behaviour)
        * matches 1 entry   -> mapped to that idx
        * matches >1 entries-> ambiguity: every candidate (idx / sector offset
          / sector size) is printed to stderr with a hint to use idx:N=file,
          then exit 2
    - the same idx specified twice (in any combination of forms) -> exit 2.
    - an argument without '=' -> exit 2.

    All validation happens here, so callers can exit before creating output.
    """
    by_name = {}
    for idx, (_off, _sz, name) in enumerate(entries):
        by_name.setdefault(name.lower(), []).append(idx)

    repl = {}
    origin = {}  # idx -> the argument that claimed it
    ambiguous = []  # (arg, name, [idx...])
    errors = []

    def claim(idx, path, arg):
        if idx in repl:
            errors.append(
                "error: entry idx %d (%s) specified twice: '%s' and '%s'"
                % (idx, entries[idx][2], origin[idx], arg))
            return
        repl[idx] = path
        origin[idx] = arg

    for arg in args:
        m = _IDX_ARG.match(arg)
        if m:
            idx = int(m.group(1))
            if idx >= len(entries):
                errors.append("error: '%s': idx %d out of range (0..%d)"
                              % (arg, idx, len(entries) - 1))
                continue
            claim(idx, m.group(2), arg)
            continue
        if "=" not in arg:
            errors.append("error: bad argument '%s' (expected name=file or idx:N=file)" % arg)
            continue
        name, path = arg.split("=", 1)
        cands = by_name.get(name.lower(), [])
        if not cands:
            continue  # unmatched names are ignored, as before
        if len(cands) > 1:
            ambiguous.append((arg, name, cands))
            continue
        claim(cands[0], path, arg)

    if ambiguous:
        for arg, name, cands in ambiguous:
            errors.append("error: '%s' is ambiguous: name '%s' matches %d entries:"
                          % (arg, name, len(cands)))
            for idx in cands:
                off, sz, nm = entries[idx]
                errors.append("    idx %d  %s  sector offset %d  sector size %d"
                              % (idx, nm, off, sz))
            errors.append("  hint: select one entry with idx:N=file (e.g. idx:%d=file)" % cands[0])
    if errors:
        _die("\n".join(errors))
    return repl


def assert_dup_identity(entries, content_before, content_after):
    """Content-identity guard for same-name groups.

    content_before / content_after are indexable by idx (list or dict) of
    bytes or hash values. For every dup group and every pair of members,
    (before equal) must equal (after equal). Returns a list of human readable
    violation strings (empty list == OK). Works for groups of 3+ members.
    """
    violations = []
    for lname, idxs in sorted(dup_groups(entries).items(), key=lambda kv: kv[1][0]):
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                same_before = content_before[i] == content_before[j]
                same_after = content_after[i] == content_after[j]
                if same_before == same_after:
                    continue
                if same_after:
                    why = "contents differ in source but are identical after conversion (merged)"
                else:
                    why = "contents are identical in source but differ after conversion"
                violations.append("dup group '%s': idx %d (%s) vs idx %d (%s): %s"
                                  % (lname, i, entries[i][2], j, entries[j][2], why))
    return violations


def txd_texture_names(data):
    """Lower-cased TEXTURENATIVE names of a TXD; [] for empty/truncated/non-TXD.

    Walks TEXDICT(0x16) top-level children up to the END OF DATA (the TEXDICT
    size field in real converted files can be bogus, e.g. 16, so it is not
    trusted). For each TEXTURENATIVE(0x15) takes the name[32] at offset 8 of
    its STRUCT payload (data offset child + 12 + 12 + 8), cut at the first NUL.
    """
    out = []
    if len(data) < 12 or struct.unpack_from("<I", data, 0)[0] != 0x16:
        return out
    p = 12
    while p + 12 <= len(data):
        ct, cs, _ver = struct.unpack_from("<III", data, p)
        if ct == 0:
            break
        if ct == 0x15 and p + 24 + 40 <= len(data):
            raw = data[p + 24 + 8:p + 24 + 40]
            out.append(raw.split(b"\0")[0].decode("ascii", "replace").lower())
        p += 12 + cs
    return out
