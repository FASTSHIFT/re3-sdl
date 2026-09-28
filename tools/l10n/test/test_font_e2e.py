#!/usr/bin/env python3
"""test_font_e2e.py - runner for the reVC font rendering E2E harness (docs/06).

Drives the game binary with REVC_FONT_TEST pointing at each case script,
collects test-out/RESULT.txt, and prints a pass/fail summary.

Usage:
    python3 tools/l10n/test/test_font_e2e.py [--game-dir DIR] [--cases DIR]
                                             [--bin PATH] [--keep]

--game-dir: directory with game data + the reVC binary (cwd for the run).
--cases   : directory with *.txt case scripts (default tools/l10n/test/cases)
--bin     : explicit binary path (default <game-dir>/reVC)
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # re3-sdl root
DEFAULT_GAME_DIR = "/tmp/vc-local/game"
DEFAULT_CASES = os.path.join(HERE, "cases")

REQUIRED_GAME_FILES = ["TEXT/JAPANESE.GXT", "models/fonts_j.txd", "ANIM/cuts.dir"]


def check_game_dir(d):
    missing = [f for f in REQUIRED_GAME_FILES if not os.path.exists(os.path.join(d, f))]
    return missing


def run_case(case_path, game_dir, bin_path):
    """Returns (passed, failed, detail_lines, result_path)."""
    out_dir = tempfile.mkdtemp(prefix="revc-font-e2e-")
    result_path = os.path.join(out_dir, "RESULT.txt")
    env = dict(os.environ)
    env["REVC_FONT_TEST"] = os.path.abspath(case_path)
    env["REVC_FONT_TEST_OUT"] = result_path
    env.setdefault("REVC_HUD", "0")
    try:
        subprocess.run([os.path.abspath(bin_path)], cwd=game_dir, env=env,
                       timeout=120, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 0, 1, ["runner: TIMEOUT (harness never quit - possible hang)"], result_path

    if not os.path.exists(result_path):
        return 0, 1, ["runner: RESULT.txt missing (game crashed before writing)"], result_path

    passed = failed = 0
    detail = []
    with open(result_path, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("PASS "):
                passed += 1
            elif line.startswith("FAIL "):
                failed += 1
                detail.append("    " + line)
            elif line.startswith("  detail:"):
                detail.append("    " + line)
    return passed, failed, detail, result_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir", default=DEFAULT_GAME_DIR)
    ap.add_argument("--cases", default=DEFAULT_CASES)
    ap.add_argument("--bin", default="")
    ap.add_argument("--case", default="", help="run a single case by name substring")
    args = ap.parse_args()

    game_dir = os.path.abspath(args.game_dir)
    bin_path = args.bin or os.path.join(game_dir, "reVC")
    if not os.path.exists(bin_path):
        print(f"error: binary not found: {bin_path}")
        return 2
    missing = check_game_dir(game_dir)
    if missing:
        print(f"error: game dir {game_dir} missing: {missing}")
        return 2

    cases_dir = os.path.abspath(args.cases)
    if not os.path.isdir(cases_dir):
        print(f"error: cases dir not found: {cases_dir}")
        return 2

    cases = sorted(f for f in os.listdir(cases_dir) if f.endswith(".txt"))
    if args.case:
        cases = [c for c in cases if args.case in c]
    if not cases:
        print("error: no cases found")
        return 2

    print(f"running {len(cases)} case(s) against {bin_path}\n")
    total_pass = total_fail = 0
    failed_cases = []
    for c in cases:
        p, f, detail, rp = run_case(os.path.join(cases_dir, c), game_dir, bin_path)
        total_pass += p
        total_fail += f
        status = "PASS" if f == 0 and p > 0 else "FAIL"
        print(f"[{status}] {c}  ({p} pass / {f} fail)")
        if f:
            failed_cases.append(c)
            for d in detail[:12]:
                print(d)
            print(f"    result: {rp}")

    print(f"\n=== total: {total_pass} pass, {total_fail} fail, "
          f"{len(cases) - len(failed_cases)}/{len(cases)} cases green ===")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
