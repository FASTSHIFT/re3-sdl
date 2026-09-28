#pragma once

// Font rendering end-to-end harness (docs/06).
// Compile-time gated by REVC_FONT_HARNESS; zero cost in release builds.
// Drives controlled CFont submit sequences (replicating the bug-triggering
// orderings from docs/05), records intercepted quads, and asserts.

#ifdef REVC_FONT_HARNESS

#include <stdint.h>

class CRect;
class CRGBA;

// ---- interception API (called from Sprite2d.cpp / Font.cpp) ----
void FontHarness_RecordQuad(const CRect &r, const CRGBA &c,
                            float u0, float v0, float u1, float v1,
                            float u3, float v3, float u2, float v2);
void FontHarness_RecordIcon(float x, float y);

// Called at the start of every RenderFontBuffer: closes the current batch.
void FontHarness_BeginFlush(void);

// ---- driver API (called from FrontendIdle) ----
// Returns true when a test script was found via REVC_FONT_TEST and armed.
bool FontHarness_Arm(void);
// Runs one frame of the armed script; writes RESULT.json and sets
// RsGlobal.quit when done. Returns false when inactive.
bool FontHarness_Frame(void);
// Exit code for main(): 0 pass, 1 fail, -1 not armed.
int  FontHarness_ExitCode(void);

#endif // REVC_FONT_HARNESS
