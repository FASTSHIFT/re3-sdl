#include "common.h"

#ifdef REVC_FONT_HARNESS

#include "FontHarness.h"
#include "Sprite2d.h"
#include "Font.h"
#include "Text.h"
#include "Timer.h"
#include "FileMgr.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

// ---- recorded quad ---------------------------------------------------------

#define MAX_QUADS 4096
struct RecordedQuad {
    float x, y, w, h;              // screen rect
    float u0, v0, u1, v1, u3, v3, u2, v2;
    uint8 r, g, b, a;              // color
    int   isShadow;                // tagged by geometry offset heuristic (filled in assert phase)
};
static RecordedQuad sQuads[MAX_QUADS];
static int sQuadCount = 0;
static int sIconCount = 0;
static int sFrameIcons = 0;

// per-batch (flush) capture
static int sBatchFirstQuad = 0;
// quads recorded BEFORE the first script op ran (the menu's own text);
// asserts only look at quads at or after this baseline.
static int sScriptBaseQuad = 0;

void
FontHarness_RecordQuad(const CRect &r, const CRGBA &c,
                       float u0, float v0, float u1, float v1,
                       float u3, float v3, float u2, float v2)
{
    if (sQuadCount >= MAX_QUADS) return;
    RecordedQuad &q = sQuads[sQuadCount++];
    q.x = r.left; q.y = r.top;
    q.w = r.right - r.left; q.h = r.bottom - r.top;
    q.u0 = u0; q.v0 = v0; q.u1 = u1; q.v1 = v1;
    q.u3 = u3; q.v3 = v3; q.u2 = u2; q.v2 = v2;
    q.r = c.r; q.g = c.g; q.b = c.b; q.a = c.a;
    q.isShadow = 0;
}

void
FontHarness_RecordIcon(float x, float y)
{
    sIconCount++; sFrameIcons++;
}

void
FontHarness_BeginFlush(void)
{
    // a new flush batch starts: remember the boundary
    sBatchFirstQuad = sQuadCount;
    sIconCount = 0;   // icon count is per flush for icon_count asserts
}

// ---- script -----------------------------------------------------------------

// Line format (deliberately simple; see docs/06 section 7 note):
//   frame                 -- begin a new frame (asserts on previous at end)
//   style N               -- SetFontStyle(N)  (0=BANK 1=STANDARD 2=HEADING 3=JAPANESE)
//   locale_style N        -- SetFontStyle(FONT_LOCALE(N))
//   scale X Y             -- SetScale
//   shadow N              -- SetDropShadowPosition
//   color R G B           -- SetColor
//   prop on|off           -- SetPropOn/Off
//   wrapx W               -- SetWrapx
//   print X Y TEXT        -- PrintString (TEXT: rest of line; supports
//                            \uXXXX, {ICON:L} -> ~L~, {JTOKEN:L} -> flagged ~L~)
//   printbottom X Y TEXT  -- PrintStringFromBottom
//   grab NAME             -- grab screen at end of current frame
//   assert ID ARGS...     -- assertions (evaluated at script end)
// Assert forms:
//   assert quad_count NAME EXPECTED
//   assert uv_in_atlas NAME
//   assert shadow_present NAME [dx dy]   (every main quad has an offset twin)
//   assert icon_count NAME EXPECTED
//   assert glyph_spacing NAME Y0 Y1 MINW MAXW [TOL]
//   assert advance_match NAME [TOL]      (measure vs replay sum, from print op)
//   assert line_rows NAME X Y TEXT EXPECTED_ROWS   (GetNumberLines)
//   assert no_crash

#define MAX_LINE 1024
#define MAX_ASSERTS 128
#define MAX_PRINTS 256

struct AssertRec {
    char id[64];
    char name[64];
    float a0, a1, a2, a3, a4;
    char text[256];
    int   i0;
};

struct PrintRec {
    // to verify measure-vs-replay: the measured width at submit time and
    // the quad-geometry sum at replay (filled from recorded quads by y-band)
    float x, y;
    float measuredWidth;
    wchar text[128];
    int   textLen;
};

struct ScriptState {
    char  scriptPath[512];
    char  grabName[64];       // current frame's grab id ("" = none)
    AssertRec asserts[MAX_ASSERTS];
    int   nAsserts;
    PrintRec prints[MAX_PRINTS];
    int   nPrints;
    int   done;               // script fully executed
    int   armed;
};
static ScriptState S;

// ---- utf8 / escapes ---------------------------------------------------------

static int
hexVal(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// Parse TEXT into wchars. Supports \uXXXX, {ICON:L} (~L~), {JTOKEN:L}
// (each char of ~L~ carrying the 0x8000 flag, as the CJK GXT stores them).
// Returns length, or -1 on syntax error.
static int
ParseText(const char *s, wchar *out, int maxLen)
{
    int n = 0;
    while (*s && n < maxLen - 1) {
        if (s[0] == '\\' && s[1] == 'u' &&
            hexVal(s[2]) >= 0 && hexVal(s[3]) >= 0 && hexVal(s[4]) >= 0 && hexVal(s[5]) >= 0) {
            out[n++] = (wchar)((hexVal(s[2]) << 12) | (hexVal(s[3]) << 8) |
                               (hexVal(s[4]) << 4) | hexVal(s[5]));
            s += 6;
        } else if (s[0] == '{') {
            // {ICON:L} or {JTOKEN:L}
            const char *close = strchr(s, '}');
            const char *colon = strchr(s, ':');
            if (!close || !colon || colon > close || close - s > 24)
                return -1;
            char kind[16] = {0};
            strncpy(kind, s + 1, colon - s - 1 < 15 ? colon - s - 1 : 15);
            char letter = colon[1] ? colon[1] : '~';
            int flagged = strcmp(kind, "JTOKEN") == 0;
            if (!flagged && strcmp(kind, "ICON") != 0)
                return -1;
            // opener, letter, closer
            wchar open = flagged ? (0x8000 | '~') : '~';
            wchar mid  = flagged ? (0x8000 | letter) : (wchar)letter;
            wchar cl   = flagged ? (0x8000 | '~') : '~';
            if (n + 3 >= maxLen - 1) return -1;
            out[n++] = open; out[n++] = mid; out[n++] = cl;
            s = close + 1;
        } else {
            // pass through as-is (ASCII; the harness scripts are ASCII+escapes)
            out[n++] = (wchar)(unsigned char)*s;
            s++;
        }
    }
    out[n] = 0;
    return n;
}

// ---- driving ----------------------------------------------------------------

static void
DoLine(char *line)
{
    // strip trailing \r\n and spaces
    size_t len = strlen(line);
    while (len && (line[len-1] == '\n' || line[len-1] == '\r' ||
                   line[len-1] == ' ' || line[len-1] == '\t'))
        line[--len] = 0;

    if (len == 0 || line[0] == '#') return;

    char cmd[32] = {0};
    sscanf(line, "%31s", cmd);

    if (strcmp(cmd, "frame") == 0) {
        // boundary is implicit: FrontendIdle calls us once per frame
        S.grabName[0] = 0;
    } else if (strcmp(cmd, "style") == 0) {
        int st = 0; sscanf(line, "%*s %d", &st);
        CFont::SetFontStyle(st);
    } else if (strcmp(cmd, "locale_style") == 0) {
        int st = 0; sscanf(line, "%*s %d", &st);
#ifdef MORE_LANGUAGES
        CFont::SetFontStyle(FONT_LOCALE(st));
#else
        CFont::SetFontStyle(st);
#endif
    } else if (strcmp(cmd, "scale") == 0) {
        float x = 1.0f, y = 1.0f;
        sscanf(line, "%*s %f %f", &x, &y);
        CFont::SetScale(x, y);
    } else if (strcmp(cmd, "shadow") == 0) {
        int p = 0; sscanf(line, "%*s %d", &p);
        CFont::SetDropShadowPosition(p);
    } else if (strcmp(cmd, "color") == 0) {
        int r = 255, g = 255, b = 255;
        sscanf(line, "%*s %d %d %d", &r, &g, &b);
        CFont::SetColor(CRGBA(r, g, b, 255));
    } else if (strcmp(cmd, "prop") == 0) {
        char onoff[8] = {0};
        sscanf(line, "%*s %7s", onoff);
        if (strcmp(onoff, "off") == 0) CFont::SetPropOff();
        else CFont::SetPropOn();
    } else if (strcmp(cmd, "wrapx") == 0) {
        float w = SCREEN_STRETCH_X(DEFAULT_SCREEN_WIDTH);
        sscanf(line, "%*s %f", &w);
        CFont::SetWrapx(w);
    } else if (strcmp(cmd, "print") == 0 || strcmp(cmd, "printbottom") == 0) {
        float x = 0, y = 0;
        char text[256] = {0};
        // find TEXT after the two numbers: scan past "cmd x y "
        if (sscanf(line, "%*s %f %f %255[^\n]", &x, &y, text) >= 3) {
            wchar wtext[128];
            int n = ParseText(text, wtext, 128);
            if (n >= 0) {
                // record measured width BEFORE submitting (submit semantics:
                // reads Details - that is the point of the invariant test)
                if (S.nPrints < MAX_PRINTS) {
                    PrintRec &pr = S.prints[S.nPrints++];
                    pr.x = x; pr.y = y;
#ifdef MORE_LANGUAGES
                    pr.measuredWidth = CFont::GetStringWidth_Jap(wtext);
#else
                    pr.measuredWidth = CFont::GetStringWidth(wtext);
#endif
                    memcpy(pr.text, wtext, sizeof(pr.text));
                    pr.textLen = n;
                }
                if (cmd[5] == 'b')
                    CFont::PrintStringFromBottom(x, y, wtext);
                else
                    CFont::PrintString(x, y, wtext);
            }
        }
    } else if (strcmp(cmd, "grab") == 0) {
        sscanf(line, "%*s %63s", S.grabName);
    } else if (strcmp(cmd, "assert") == 0) {
        if (S.nAsserts < MAX_ASSERTS) {
            AssertRec &ar = S.asserts[S.nAsserts++];
            memset(&ar, 0, sizeof(ar));
            char kind[32] = {0};
            // common: assert KIND NAME [num|floats|text...]
            sscanf(line, "%*s %31s %63s", kind, ar.name);
            strcpy(ar.id, kind);
            if (strcmp(kind, "quad_count") == 0 || strcmp(kind, "icon_count") == 0 ||
                strcmp(kind, "line_rows") == 0) {
                sscanf(line, "%*s %*s %*s %d", &ar.i0);
                if (strcmp(kind, "line_rows") == 0) {
                    // assert line_rows NAME X Y TEXT EXPECTED
                    float fx, fy; char tx[256] = {0};
                    sscanf(line, "%*s %*s %*s %f %f %255[^\n]", &fx, &fy, tx);
                    ar.a0 = fx; ar.a1 = fy;
                    ParseText(tx, (wchar*)ar.text, 128);
                }
            } else if (strcmp(kind, "glyph_spacing") == 0) {
                sscanf(line, "%*s %*s %*s %f %f %f %f %f",
                       &ar.a0, &ar.a1, &ar.a2, &ar.a3, &ar.a4);
                if (ar.a4 == 0.0f) ar.a4 = 2.0f;    // default tol
            } else if (strcmp(kind, "shadow_present") == 0) {
                sscanf(line, "%*s %*s %*s %f %f", &ar.a0, &ar.a1);
                if (ar.a0 == 0.0f && ar.a1 == 0.0f) { ar.a0 = 2.0f; ar.a1 = 2.0f; }
            } else if (strcmp(kind, "advance_match") == 0) {
                sscanf(line, "%*s %*s %*s %f", &ar.a0);
                if (ar.a0 <= 0.0f) ar.a0 = 1.0f;    // default tol (px)
            }
            // uv_in_atlas / no_crash: no args
        }
    }
}

// ---- script loading ---------------------------------------------------------

static char *sScriptBuf = nil;
static int   sScriptPos = 0, sScriptLen = 0;
static int   sScriptStarted = 0;
static int   sPendingFinish = 0;

static char *
NextLine(void)
{
    static char line[MAX_LINE];
    if (sScriptPos >= sScriptLen) return nil;
    int n = 0;
    while (sScriptPos < sScriptLen && n < MAX_LINE - 1) {
        char c = sScriptBuf[sScriptPos++];
        line[n++] = c;
        if (c == '\n') break;
    }
    line[n] = 0;
    return line;
}

bool
FontHarness_Arm(void)
{
    const char *path = getenv("REVC_FONT_TEST");
    if (!path || !*path) return false;

    FILE *f = fopen(path, "rb");
    if (!f) { printf("[FONTHARNESS] cannot open %s\n", path); return false; }
    fseek(f, 0, SEEK_END);
    sScriptLen = ftell(f);
    fseek(f, 0, SEEK_SET);
    sScriptBuf = new char[sScriptLen + 1];
    if (fread(sScriptBuf, 1, sScriptLen, f) != (size_t)sScriptLen) {
        fclose(f); delete[] sScriptBuf; sScriptBuf = nil; return false;
    }
    sScriptBuf[sScriptLen] = 0;
    fclose(f);
    sScriptPos = 0;

    memset(&S, 0, sizeof(S));
    strncpy(S.scriptPath, path, sizeof(S.scriptPath) - 1);
    S.armed = 1;
    S.done = 0;

    // perf HUD would submit text through CFont and pollute the capture
#ifdef REVC_PERF_HUD
    extern int8_t gPerfHudEnabled;
    gPerfHudEnabled = 0;
#endif
    printf("[FONTHARNESS] armed: %s\n", path);
    return true;
}

// ---- assertions --------------------------------------------------------------

static int  sPassCount = 0, sFailCount = 0;
static char sResultText[16384];
static int  sResultLen = 0;

static void
RLog(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    sResultLen += vsnprintf(sResultText + sResultLen,
                            sizeof(sResultText) - sResultLen - 1, fmt, ap);
    va_end(ap);
    if (sResultLen >= (int)sizeof(sResultText) - 1)
        sResultLen = (int)sizeof(sResultText) - 1;
}

static void
Check(bool ok, const char *id, const char *name, const char *detail)
{
    if (ok) { sPassCount++; RLog("PASS %s %s %s\n", id, name, detail ? detail : ""); }
    else    { sFailCount++; RLog("FAIL %s %s %s\n", id, name, detail ? detail : ""); }
}

// count quads whose top-left is inside a y band (dedupe identical quads from
// shadow passes by accepting all: spacing asserts use x-sorted edges)
static int
QuadsInYBand(float y0, float y1, RecordedQuad *out, int maxOut)
{
    int n = 0;
    for (int i = sScriptBaseQuad; i < sQuadCount && n < maxOut; i++) {
        if (sQuads[i].y >= y0 && sQuads[i].y <= y1)
            out[n++] = sQuads[i];
    }
    // sort by x then y
    for (int i = 0; i < n - 1; i++)
        for (int j = i + 1; j < n; j++)
            if (out[j].x < out[i].x ||
                (out[j].x == out[i].x && out[j].y < out[i].y)) {
                RecordedQuad t = out[i]; out[i] = out[j]; out[j] = t;
            }
    return n;
}

static void
RunAsserts(void)
{
    for (int i = 0; i < S.nAsserts; i++) {
        AssertRec &ar = S.asserts[i];
        char detail[256];

        if (strcmp(ar.id, "quad_count") == 0) {
            int cnt = sQuadCount - sScriptBaseQuad;
            sprintf(detail, "count=%d want=%d", cnt, ar.i0);
            Check(cnt == ar.i0, ar.id, ar.name, detail);
        } else if (strcmp(ar.id, "icon_count") == 0) {
            sprintf(detail, "count=%d want=%d", sIconCount, ar.i0);
            Check(sIconCount == ar.i0, ar.id, ar.name, detail);
        } else if (strcmp(ar.id, "uv_in_atlas") == 0) {
            int bad = 0;
            for (int q = sScriptBaseQuad; q < sQuadCount; q++) {
                RecordedQuad &Q = sQuads[q];
                float umin = Q.u0, umax = Q.u1, vmin = Q.v0, vmax = Q.v1;
                if (Q.u2 < umin) umin = Q.u2; if (Q.u2 > umax) umax = Q.u2;
                if (Q.v2 < vmin) vmin = Q.v2; if (Q.v2 > vmax) vmax = Q.v2;
                if (Q.u3 < umin) umin = Q.u3; if (Q.u3 > umax) umax = Q.u3;
                if (Q.v3 < vmin) vmin = Q.v3; if (Q.v3 > vmax) vmax = Q.v3;
                if (umin < -0.001f || umax > 1.001f || vmin < -0.001f || vmax > 1.001f) {
                    bad++;
                    if (bad <= 3)
                        RLog("  detail: quad[%d] uv u[%.3f..%.3f] v[%.3f..%.3f]\n",
                             q, umin, umax, vmin, vmax);
                }
            }
            sprintf(detail, "bad_uv_quads=%d/%d", bad, sQuadCount);
            Check(bad == 0, ar.id, ar.name, detail);
        } else if (strcmp(ar.id, "shadow_present") == 0) {
            // every quad at (x,y) with size (w,h) must have a twin at
            // (x+dx, y+dy) same size, within tolerance
            int missing = 0;
            for (int q = sScriptBaseQuad; q < sQuadCount; q++) {
                RecordedQuad &Q = sQuads[q];
                // only check "main" quads: those that are not themselves a
                // shadow (shadow quads are the offset ones; use a visited set)
                int isShadow = 0;
                for (int o = sScriptBaseQuad; o < sQuadCount; o++) {
                    if (o == q) continue;
                    RecordedQuad &M = sQuads[o];
                    if (fabsf(M.x + ar.a0 - Q.x) < 1.0f && fabsf(M.y + ar.a1 - Q.y) < 1.0f &&
                        fabsf(M.w - Q.w) < 1.0f && fabsf(M.h - Q.h) < 1.0f) {
                        // Q is at M+offset => Q is M's shadow
                        isShadow = 1; break;
                    }
                }
                if (isShadow) continue;
                int found = 0;
                for (int o = sScriptBaseQuad; o < sQuadCount && !found; o++) {
                    if (o == q) continue;
                    RecordedQuad &T = sQuads[o];
                    if (fabsf(T.x - (Q.x + ar.a0)) < 1.0f && fabsf(T.y - (Q.y + ar.a1)) < 1.0f &&
                        fabsf(T.w - Q.w) < 1.0f && fabsf(T.h - Q.h) < 1.0f)
                        found = 1;
                }
                if (!found) missing++;
            }
            sprintf(detail, "quads_without_shadow=%d/%d", missing, sQuadCount);
            Check(missing == 0, ar.id, ar.name, detail);
        } else if (strcmp(ar.id, "glyph_spacing") == 0) {
            // in y band [a0,a1], adjacent x-sorted quads: successive left-edge
            // diffs must lie in [a2*tol, a3*tol] (all in screen px)
            RecordedQuad inBand[512];
            int n = QuadsInYBand(ar.a0, ar.a1, inBand, 512);
            int badSp = 0; float minS = 1e9, maxS = -1e9;
            for (int q = 1; q < n; q++) {
                // same visual row: y within 3px
                if (fabsf(inBand[q].y - inBand[q-1].y) > 3.0f) continue;
                float d = inBand[q].x - inBand[q-1].x;
                if (d < minS) minS = d;
                if (d > maxS) maxS = d;
                if (d < ar.a2 - ar.a4 || d > ar.a3 + ar.a4) badSp++;
            }
            sprintf(detail, "quads=%d spacing=[%.1f..%.1f] want=[%.1f..%.1f]+-%.1f bad=%d",
                    n, minS, maxS, ar.a2, ar.a3, ar.a4, badSp);
            Check(n > 0 && badSp == 0, ar.id, ar.name, detail);
        } else if (strcmp(ar.id, "advance_match") == 0) {
            // invariant: GetStringWidth at submit == sum of replay advances.
            // Replay advances are reconstructed from quad x-edges per print
            // (each glyph's advance == next quad left - this quad left; last
            // glyph approximated by its quad width, which CJK_DRAWW==advance).
            int bad = 0;
            for (int p = 0; p < S.nPrints && bad <= 3; p++) {
                PrintRec &pr = S.prints[p];
                // collect quads near this print's first row
                RecordedQuad inBand[128];
                int n = QuadsInYBand(pr.y - 4.0f, pr.y + 4.0f, inBand, 128);
                if (n < 2) { // too few quads to sum; skip single-glyph prints
                    continue;
                }
                float sum = inBand[n-1].x + inBand[n-1].w - inBand[0].x;
                if (fabsf(sum - pr.measuredWidth) > ar.a0) {
                    bad++;
                    RLog("  detail: print#%d measured=%.1f replay=%.1f\n",
                         p, pr.measuredWidth, sum);
                }
            }
            sprintf(detail, "mismatched=%d/%d", bad, S.nPrints);
            Check(bad == 0, ar.id, ar.name, detail);
        } else if (strcmp(ar.id, "line_rows") == 0) {
            // re-run GetNumberLines with the same settings as the print had:
            // acceptable as approximation because asserts run with the LAST
            // submitted state; scripts should set state before this assert.
            int rows = CFont::GetNumberLines(ar.a0, ar.a1, (wchar*)ar.text);
            sprintf(detail, "rows=%d want=%d", rows, ar.i0);
            Check(rows == ar.i0, ar.id, ar.name, detail);
        } else if (strcmp(ar.id, "no_crash") == 0) {
            Check(true, ar.id, ar.name, "still running");
        }
    }
}

static void
WriteResult(void)
{
    char path[600];
    const char *out = getenv("REVC_FONT_TEST_OUT");
    if (out && *out)
        snprintf(path, sizeof(path), "%s", out);
    else
        snprintf(path, sizeof(path), "%stest-out/RESULT.txt",
                 CFileMgr::GetRootDirName());

    FILE *f = fopen(path, "w");
    if (!f) {
        printf("[FONTHARNESS] cannot write %s\n", path);
        return;
    }
    fprintf(f, "script %s\n", S.scriptPath);
    fprintf(f, "pass %d\nfail %d\nquads %d icons %d\n",
            sPassCount, sFailCount, sQuadCount - sScriptBaseQuad, sFrameIcons);
    fprintf(f, "%s", sResultText);
    fclose(f);
    printf("[FONTHARNESS] result: %d pass, %d fail -> %s\n",
           sPassCount, sFailCount, path);
}

// ---- frame loop ----------------------------------------------------------------

bool
FontHarness_Frame(void)
{
    if (!S.armed || S.done) return false;

    if (sPendingFinish) {
        // previous call submitted the last lines; the caller has since
        // flushed them through DrawFonts - now it's safe to assert.
        printf("[FONTHARNESS] finish: quads=%d\n", sQuadCount);
        RunAsserts();
        WriteResult();
        S.done = 1;
        sPendingFinish = 0;
        RsGlobal.quit = TRUE;
        return true;
    }

    if (!sScriptStarted) {
        sScriptStarted = 1;
        // The menu (RenderMenus) draws in the same frame before us; keep a
        // baseline instead of clearing so every frame's menu text stays
        // below it (clearing once is not enough - the menu redraws every
        // frame, including the finish frame).
        sScriptBaseQuad = sQuadCount;
        sFrameIcons = 0;
    }

    // execute up to the next "frame" directive (or EOF)
    int guard = 4096;
    while (guard--) {
        char *line = NextLine();
        if (!line) {
            // script consumed: DON'T assert yet - the caller flushes
            // (DrawFonts) after we return; assert on the next call.
            if (S.nPrints > 0 || sQuadCount > 0) {
                sPendingFinish = 1;
                printf("[FONTHARNESS] EOF, deferring finish (prints=%d quads=%d)\n",
                       S.nPrints, sQuadCount);
                return true;
            }
            RunAsserts();
            WriteResult();
            S.done = 1;
            return true;    // caller quits after this
        }
        char cmd[32] = {0};
        sscanf(line, "%31s", cmd);
        if (strcmp(cmd, "frame") == 0 && sScriptStarted && sQuadCount + S.nPrints > 0) {
            // a "frame" line ends the previous frame: push it back by
            // rewinding past this line minus its newline
            int ll = strlen(line);
            sScriptPos -= ll;
            // note: NextLine consumed the '\n'; rewind exactly ll bytes
            return true;
        }
        DoLine(line);
    }
    return true;
}

int
FontHarness_ExitCode(void)
{
    if (!S.armed) return -1;
    if (S.done) return sFailCount == 0 ? 0 : 1;
    return 1;   // armed but never finished: treat as failure
}

#endif // REVC_FONT_HARNESS
