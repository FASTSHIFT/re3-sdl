#!/usr/bin/env python3
"""make_translation_batches.py - split the VC source text into translation
batches for an LLM, and merge the filled batches back.

Adapted from the re3 (GTA3) toolchain for Vice City:

  1. Parse Sergeanur/GXT "american.txt" into key->English pairs.
     VC keeps MAIN and 78 mission tables in ONE txt; some values carry a
     source tag like "{ reVC update }" which is stripped before batching.
     The batches themselves carry no table metadata; table grouping is
     recovered at GXT-build time (gen_cn_font.py reads the source txt).
  2. `split`  : emit batches/batch_NN.json, each a small JSON dict {key: english}
                plus a shared PROMPT.txt describing exactly how to translate.
     A cheap model fills each batch's English values with Chinese IN PLACE,
     writing batches/batch_NN.zh.json (same keys, Chinese values).
  3. `merge`  : combine all batch_NN.zh.json into translation.txt
                (upstream Sergeanur format, source key order preserved)
                ready for gen_cn_font.py.

Control codes like ~g~ ~w~ ~h~ ~1~ ~k~ and ~k~~ACTION~ MUST be preserved
verbatim; the prompt instructs the model accordingly and merge validates it.
"""
import argparse
import json
import os
import re
import sys

CTRL_RE = re.compile(r"~[A-Za-z0-9_]*~")
# Value-level source tags found in the VC txt (e.g. "[KEY] { reVC update }",
# or an embedded "{ reVC updates }" line inside a multi-line value).
TAG_RE = re.compile(r"\{\s*[^}]*\}\s*$")
# Mission-table boundary marker lines: "{==== MISSION TABLE XXX ====}".
TABLE_RE = re.compile(r"^\{[=\s]*MISSION TABLE ([^}]+?)\s*[=]*\}$")

PROMPT = """\
You are translating the in-game text of Grand Theft Auto: Vice City into
Simplified Chinese (简体中文) for a fan port. You are given a JSON object
mapping string keys to English source text. Return a JSON object with the
SAME keys, where each value is the Simplified Chinese translation of the
English value.

HARD RULES (violating any of these breaks the game):
1. Keep every key exactly as given. Do not add, remove, rename, or reorder keys.
2. Preserve ALL control codes verbatim, in place: tokens like ~g~ ~w~ ~h~ ~r~
   ~b~ ~y~ ~p~ ~1~ ~k~ and key tokens like ~k~~VEHICLE_ACCELERATE~. Never
   translate, delete, reorder, or add spaces inside them. They may appear at the
   start, middle, or end of a value; keep their exact positions relative to text.
3. Do NOT translate ALL-CAPS placeholder tokens inside ~k~...~ (e.g.
   ~VEHICLE_ENTER_EXIT~). Leave them exactly as-is.
4. Output ONLY a valid JSON object, UTF-8, no comments, no markdown fences.
5. Keep translations concise - this is for a 640x480 handheld screen. Prefer
   short, natural game Chinese (e.g. "WASTED"->"死亡", "BUSTED"->"被捕").
6. Proper nouns, Vice City conventions: 汤米·维赛迪 (Tommy Vercetti), 罪恶都市
   (Vice City), 肯·罗森博格 (Ken Rosenberg), 兰斯·万斯 (Lance Vance), and the
   well-known Chinese names for districts (Ocean Beach 海洋海滩, Downtown 市中心,
   Vice Point 罪恶角, Little Haiti 小海地, Starfish Island 海星岛). Radio
   stations keep English (Wave 103, Flash FM, Emotion 98.3, Espantoso,
   Fever 105, K-Chat, VCPR, V-Rock). Otherwise keep the English name.
7. If a value is empty or only control codes/punctuation, return it unchanged.
8. Do NOT translate these special keys - copy their value verbatim: any value
   that is purely ASCII letters/dashes with no real words to translate, and
   values that look like font/charset definitions or file paths.

Example input:
  {"IN_VEH": "~g~Hey! Get back in the vehicle!", "WELCOME": "WELCOME TO"}
Example output:
  {"IN_VEH": "~g~嘿！回到车上去！", "WELCOME": "欢迎来到"}
"""


def parse_source(path):
    txt = open(path, encoding="utf-8-sig", errors="replace").read()  # strip BOM
    entries = {}
    boundaries = []  # (insertion index of the NEXT key, table name)
    cur, buf = None, []
    for line in txt.splitlines():
        if TABLE_RE.match(line):
            # Mission-table boundary comment; not part of any value. Record
            # where the NEXT key lands so merge can re-emit the boundary.
            boundaries.append((len(entries) + (1 if cur is not None else 0),
                               TABLE_RE.match(line).group(1)))
            continue
        m = re.match(r"^\[([^\]]+)\]", line)
        if m:
            if cur is not None:
                entries[cur] = strip_tag("\n".join(buf).strip())
            cur, buf = m.group(1), []
        else:
            buf.append(line)
    if cur is not None:
        entries[cur] = strip_tag("\n".join(buf).strip())
    return entries, boundaries


def strip_tag(value):
    """Remove source-tag noise from a value.

    Two cases (both are repo metadata, not game text; neither may reach the
    translator - and the GXT writer wants the plain value anyway):
      - trailing tags like '{ reVC update }' at the end of a value
      - embedded tag lines like '{ reVC updates }' inside multi-line values
    Mission-table boundary markers are filtered at parse time (see
    parse_source) and never get here.
    """
    lines = [l for l in value.splitlines() if not TAG_RE.match(l)]
    return TAG_RE.sub("", "\n".join(lines)).rstrip()


def codes(s):
    return CTRL_RE.findall(s)


def cmd_split(args):
    entries, _ = parse_source(args.source)
    os.makedirs(args.outdir, exist_ok=True)
    with open(os.path.join(args.outdir, "PROMPT.txt"), "w", encoding="utf-8") as f:
        f.write(PROMPT)

    keys = list(entries.keys())
    nb = 0
    for i in range(0, len(keys), args.size):
        chunk = {k: entries[k] for k in keys[i:i + args.size]}
        path = os.path.join(args.outdir, "batch_%03d.json" % nb)
        json.dump(chunk, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        nb += 1
    print("wrote %d entries into %d batches (size %d) in %s"
          % (len(keys), nb, args.size, args.outdir))
    print("PROMPT.txt written. For each batch_NN.json, have the model produce")
    print("batch_NN.zh.json (same keys, Chinese values) using PROMPT.txt.")


def cmd_merge(args):
    src, boundaries = parse_source(args.source)
    out = {}
    missing_files = []
    warned = 0
    nb = 0
    while True:
        zpath = os.path.join(args.outdir, "batch_%03d.zh.json" % nb)
        opath = os.path.join(args.outdir, "batch_%03d.json" % nb)
        if not os.path.exists(opath):
            break
        if not os.path.exists(zpath):
            missing_files.append(zpath)
            nb += 1
            continue
        zh = json.load(open(zpath, encoding="utf-8"))
        for k, v in zh.items():
            # Validate control codes match the source (order-insensitive multiset).
            if k in src and sorted(codes(src[k])) != sorted(codes(v)):
                sys.stderr.write("WARN %s: control codes differ\n  EN=%s\n  ZH=%s\n"
                                 % (k, src[k], v))
                warned += 1
            out[k] = v
        nb += 1

    if missing_files:
        sys.stderr.write("Missing %d batch translations:\n  %s\n"
                         % (len(missing_files), "\n  ".join(missing_files)))

    # Fill any untranslated keys with the English source so the GXT stays complete.
    filled = 0
    for k, v in src.items():
        if k not in out:
            out[k] = v
            filled += 1

    # Preserve the ORIGINAL source key order (upstream groups related strings).
    ordered_keys = list(src.keys())

    if args.out:
        # Emit the upstream Sergeanur/GXT txt format, in the original source
        # order, with mission-table boundary lines re-inserted before the
        # first key of each table (gen_cn_font.py needs them for the VC
        # multi-table GXT layout).
        bounds = dict(boundaries)  # insertion index -> table name
        with open(args.out, "w", encoding="utf-8") as f:
            for i, k in enumerate(ordered_keys):
                if i in bounds:
                    f.write("{========= MISSION TABLE %s ==========}\n\n" % bounds[i])
                f.write("[%s]\n%s\n\n" % (k, out[k]))

    print("merged %d keys -> %s (%d fell back to English, %d code warnings)"
          % (len(out), args.out, filled, warned))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("split", help="split source into batch JSONs + PROMPT")
    sp.add_argument("source", help="american.txt (Sergeanur/GXT format)")
    sp.add_argument("--outdir", default="batches")
    sp.add_argument("--size", type=int, default=200, help="entries per batch")
    sp.set_defaults(func=cmd_split)

    mp = sub.add_parser("merge", help="merge batch_NN.zh.json into translation.txt")
    mp.add_argument("source", help="american.txt (for validation + fallback)")
    mp.add_argument("--outdir", default="batches")
    mp.add_argument("--out", default="translation.txt",
                    help="output Sergeanur/GXT txt for gen_cn_font.py")
    mp.set_defaults(func=cmd_merge)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
