/*
 * perfhud.cpp - perf HUD front end for the R36S SDL2 port.
 * Ported from re3/src/skel/gbm/hud_overlay.cpp (VIFEX, rpi-perf-optimization).
 *
 * The Pi version drew a 5x7 bitmap font into the RGB565 readback buffer.
 * There is no readback on the R36S, so this version renders with the engine's
 * own CFont + CSprite2d after the game's 2D pass, from Idle()/FrontendIdle().
 * Controlled exclusively via the PERF HUD menu entry (reVC.ini [Perf] PerfHud).
 */
#ifdef REVC_PERF_HUD

#include "common.h"
#include "perf.h"

#include "Sprite2d.h"
#include "Font.h"
#include "Text.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Persistent switch bound to the frontend menu option; stored in reVC.ini.
// -1 = not yet resolved from the env/ini (see Hud_Enabled).
int8_t gPerfHudEnabled = -1;

// Stage timings from Idle() (see main.cpp; zero-init each frame there).
double gStageProcMs = 0.0, gStageRLMs = 0.0, gStagePreMs = 0.0, gStageSceneMs = 0.0;

int
Hud_Enabled(void)
{
	if(gPerfHudEnabled < 0) gPerfHudEnabled = getenv("REVC_HUD") ? 1 : 0;
	return gPerfHudEnabled != 0;
}

// ---- metrics (front end) --------------------------------------------------
static HudMetrics sM;
static char sLines[8][48];
static int sNumLines = 0;
static HudStatsCtx *sStats = 0;

void
Hud_Update(const HudMetrics *m)
{
	if(!Hud_Enabled()) return;
	sM = *m;

	if(sStats == 0) sStats = HudStats_Create();
	HudStatsSample st;
	HudStats_Sample(sStats, &st);

	double fps = sM.frameMs > 0.0 ? 1000.0 / sM.frameMs : 0.0;

	sNumLines = 0;
	snprintf(sLines[sNumLines++], sizeof(sLines[0]), "FPS:%.0f %.1fms", fps, sM.frameMs);
	snprintf(sLines[sNumLines++], sizeof(sLines[0]), "CPU:%.1f GPU:%.1f", sM.cpuMs, sM.gpuMs);
	snprintf(sLines[sNumLines++], sizeof(sLines[0]), "DC:%d", (int)sM.drawCalls);
	if(st.cpuPct >= 0) snprintf(sLines[sNumLines++], sizeof(sLines[0]), "SYS:%d%%", st.cpuPct);
	if(st.rssMb >= 0 && st.sysMemPct >= 0)
		snprintf(sLines[sNumLines++], sizeof(sLines[0]), "MEM:%dM %d%%", st.rssMb, st.sysMemPct);
	else if(st.rssMb >= 0)
		snprintf(sLines[sNumLines++], sizeof(sLines[0]), "MEM:%dM", st.rssMb);
	if(st.armMhz > 0 && st.gpuMhz > 0)
		snprintf(sLines[sNumLines++], sizeof(sLines[0]), "A:%dM G:%dM", st.armMhz, st.gpuMhz);
	else if(st.armMhz > 0)
		snprintf(sLines[sNumLines++], sizeof(sLines[0]), "A:%dM", st.armMhz);
	if(st.tempMilliC >= 0) snprintf(sLines[sNumLines++], sizeof(sLines[0]), "T:%.1fC", st.tempMilliC / 1000.0);

	// ---- periodic log line (every 120 frames) -----------------------------
	// Averages over the window; carries the full stage breakdown plus
	// min/max frame time, which is too verbose for the on-screen HUD.
	{
		static double aF = 0, aC = 0, aG = 0, aP = 0, aR = 0, aE = 0, aS = 0;
		static double minF = 1e9, maxF = 0;
		static double aDC = 0;
		static int n = 0;
		aF += sM.frameMs;
		aC += sM.cpuMs;
		aG += sM.gpuMs;
		aP += sM.procMs;
		aR += sM.rlMs;
		aE += sM.preMs;
		aS += sM.sceneMs;
		aDC += sM.drawCalls;
		if(sM.frameMs > 0.0) {
			if(sM.frameMs < minF) minF = sM.frameMs;
			if(sM.frameMs > maxF) maxF = sM.frameMs;
		}
		if(++n >= 120) {
			double other = (aC / n) - (aP + aR + aE + aS) / n;
			if(other < 0.0) other = 0.0;
			printf("[perf] frame=%.2fms(min %.1f max %.1f) fps=%.0f | cpu=%.2f gpu=%.2f | "
			       "proc=%.2f rl=%.2f pre=%.2f scene=%.2f other=%.2f | dc=%.0f | "
			       "sys=%d%% mem=%dM/%d%% a=%dM g=%dM t=%.1fC\n",
			       aF / n, minF, maxF, aF > 0 ? 1000.0 / (aF / n) : 0.0,
			       aC / n, aG / n, aP / n, aR / n, aE / n, aS / n, other,
			       aDC / n, st.cpuPct, st.rssMb, st.sysMemPct, st.armMhz, st.gpuMhz,
			       st.tempMilliC >= 0 ? st.tempMilliC / 1000.0 : 0.0);
			aF = aC = aG = aP = aR = aE = aS = aDC = 0;
			minF = 1e9;
			maxF = 0;
			n = 0;
		}
	}
}

// ---- drawing --------------------------------------------------------------
// Renders through CFont/CSprite2d so no extra textures or GL state are needed.
// The Pi version anchored the block to the left-middle of the screen so it
// doesn't cover the game's top-left prompts/messages; keep that position.
void
Hud_Draw(void)
{
	// Drawn from both Idle() and FrontendIdle(); in the frontend there is no
	// Hud_Update call (metrics are meaningless there) so we keep showing the
	// last sampled lines, which lets the menu toggle give immediate feedback.
	if(!Hud_Enabled() || sNumLines == 0) return;

	const float lineH = SCREEN_SCALE_Y(12.0f);
	const float textScaleX = SCREEN_SCALE_X(0.5f);
	const float textScaleY = SCREEN_SCALE_Y(0.8f);
	const float blockH = sNumLines * lineH;
	const float pad = SCREEN_SCALE_X(4.0f);

	// Left-middle anchor.
	float top = (SCREEN_HEIGHT - blockH) / 2.0f;
	if(top < 0.0f) top = 0.0f;

	// Semi-transparent black backdrop for readability.
	CSprite2d::DrawRect(CRect(0.0f, top - pad, SCREEN_SCALE_X(110.0f), top + blockH),
	                    CRGBA(0, 0, 0, 110));

	wchar uline[48];
	CFont::SetPropOn();
	CFont::SetBackgroundOff();
	CFont::SetScale(textScaleX, textScaleY);
	CFont::SetCentreOff();
	CFont::SetRightJustifyOff();
	CFont::SetJustifyOff();
	CFont::SetBackGroundOnlyTextOff();
	CFont::SetWrapx(SCREEN_WIDTH);
	CFont::SetFontStyle(FONT_STANDARD);
	CFont::SetDropShadowPosition(0);
	CFont::SetColor(CRGBA(255, 255, 255, 255));

	for(int i = 0; i < sNumLines; i++) {
		AsciiToUnicode(sLines[i], uline);
		CFont::PrintString(SCREEN_SCALE_X(4.0f), top + i * lineH, uline);
	}

	CFont::DrawFonts();
}

#endif /* REVC_PERF_HUD */
