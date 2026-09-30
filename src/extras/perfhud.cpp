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

#include "../../vendor/librw/src/gl/rwgl3impl.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Persistent switch bound to the frontend menu option; stored in reVC.ini.
// -1 = not yet resolved from the env/ini (see Hud_Enabled).
int8_t gPerfHudEnabled = -1;

// Stage timings from Idle() (see main.cpp; zero-init each frame there).
double gStageProcMs = 0.0, gStageRLMs = 0.0, gStagePreMs = 0.0, gStageSceneMs = 0.0;

// ---- GPU timer-query markers (filled in PerfHud_CollectGpuMarkers) ---------
static struct { char name[16]; double gpuMs; } sGpuMarks[8];
static int sNumGpuMarks = 0;

void
PerfHud_CollectGpuMarkers(void)
{
        rw::gl3::G3GpuMarkerStat st[8];
        int n = rw::gl3::gl3GpuFrameStats(st, 8);
        if(n > 8) n = 8;
        for(int i = 0; i < n; i++) {
                strncpy(sGpuMarks[i].name, st[i].name, sizeof(sGpuMarks[i].name));
                sGpuMarks[i].gpuMs = st[i].gpuMs;
        }
        sNumGpuMarks = n;
        rw::gl3::gl3GpuMarkerFrameReset();
}

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
        // GPU-side marker durations (timer queries; latest frame)
        if(sNumGpuMarks > 0 && sNumLines < 8) {
                char buf[48] = "G:";
                for(int i = 0; i < sNumGpuMarks; i++) {
                        char one[16];
                        snprintf(one, sizeof(one), "%s%.1f ", sGpuMarks[i].name, sGpuMarks[i].gpuMs);
                        if(strlen(buf) + strlen(one) >= sizeof(buf)) break;
                        strcat(buf, one);
                }
                snprintf(sLines[sNumLines++], sizeof(sLines[0]), "%s", buf);
        }
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
			       "sys=%d%% mem=%dM/%d%% a=%dM g=%dM t=%.1fC",
			       aF / n, minF, maxF, aF > 0 ? 1000.0 / (aF / n) : 0.0,
			       aC / n, aG / n, aP / n, aR / n, aE / n, aS / n, other,
			       aDC / n, st.cpuPct, st.rssMb, st.sysMemPct, st.armMhz, st.gpuMhz,
			       st.tempMilliC >= 0 ? st.tempMilliC / 1000.0 : 0.0);
			// GPU-side marker durations (timer queries, drained at vsync)
			if(sNumGpuMarks > 0) {
				printf(" | gpu:");
				for(int i = 0; i < sNumGpuMarks; i++)
					printf(" %s=%.2f", sGpuMarks[i].name, sGpuMarks[i].gpuMs);
			}
			printf("\n");
			// One-frame draw-call batching report every 4th log period
			// (needs a trace captured during the previous frame; see the
			// Begin/End hooks in main.cpp's RenderScene).
			{
				static int period = 0;
				if(++period % 4 == 0) {
					int draws, rsw, ssw, asw, runs;
					rw::gl3::gl3DrawTraceReport(&draws, &rsw, &ssw, &asw, &runs);
					int distinct, top1, top4;
					rw::gl3::gl3DrawTraceRasters(&distinct, &top1, &top4);
					if(draws > 0)
						printf("[batch] draws=%d texsw=%d alphatestsw=%d alphasw=%d mergeable=%d (%.0f%% of draws) | distinct_tex=%d top1=%d top4=%d\n",
						       draws, rsw, ssw, asw, runs, draws > 0 ? 100.0*runs/draws : 0.0,
						       distinct, top1, top4);
				}
			}
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

	// Left-middle anchor.
	float top = (SCREEN_HEIGHT - blockH) / 2.0f;
	if(top < 0.0f) top = 0.0f;

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
	CFont::SetDropShadowPosition(1);
	CFont::SetDropColor(CRGBA(0, 0, 0, 255));
	CFont::SetColor(CRGBA(255, 255, 255, 255));

	for(int i = 0; i < sNumLines; i++) {
		AsciiToUnicode(sLines[i], uline);
		CFont::PrintString(SCREEN_SCALE_X(4.0f), top + i * lineH, uline);
	}

	CFont::DrawFonts();
}

#endif /* REVC_PERF_HUD */
