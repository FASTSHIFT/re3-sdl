/*
 * perf.h - perf HUD front-end interface for the R36S SDL2 port.
 * Ported from re3/src/skel/gbm/hud_overlay.h (VIFEX, rpi-perf-optimization).
 *
 * On the Pi (GBM) the HUD was drawn straight into the RGB565 readback buffer
 * with a 5x7 bitmap font. The R36S has no readback path: SDL2 presents via
 * eglSwapBuffers directly. So here the HUD renders through the engine's own
 * 2D path (CSprite2d rects + CFont text) from Idle(), enabled with REVC_HUD=1.
 */
#ifndef REVC_PERF_H
#define REVC_PERF_H

// int8 is engine-common.h shorthand; use the standard type so this header
// can be included from both engine and standalone (skeleton) contexts.
#include <stdint.h>

// Per-frame timings fed by the skeleton (milliseconds).
struct HudMetrics {
	double frameMs;   // wall time between frames
	double cpuMs;     // game CPU (RsEventHandler rsIDLE) time
	double gpuMs;     // showRaster/swap (GPU scene wait)
	double drawCalls; // GL draw calls this frame (from librw counter)

	// Stage breakdown of cpuMs (Idle() phases; 0 when not measured).
	double procMs;      // CGame::Process (game logic update)
	double rlMs;        // ConstructRenderList (visibility/LOD)
	double preMs;       // PreRender (entities' pre-render)
	double sceneMs;     // RenderScene + RenderEffects (GL submission)
};

// ---- front end (perfhud.cpp) ----

// Persistent HUD switch, bound to the frontend menu option (CFO) and stored
// in reVC.ini as [Perf] PerfHud. Kept as int8_t so CCFOSelect can bind to it.
extern int8_t gPerfHudEnabled;

// Is the HUD enabled? (menu setting; falls back to REVC_HUD env before the
// ini has been read). Cheap to call per frame.
int
Hud_Enabled(void);


// Feed the latest per-frame metrics + sample telemetry + remember lines.
// Call once per frame, BEFORE rendering 2D stuff.
void
Hud_Update(const HudMetrics *m);

// Stage timings from Idle() (REVC_PERF_HUD). Written by main.cpp stage
// timers, read by the skeleton when it fills HudMetrics.
extern double gStageProcMs, gStageRLMs, gStagePreMs, gStageSceneMs;

// Read back the GPU timer-query markers placed around the render passes
// (scene/fx/hud2d) after the vsync swap drained the pipeline, then reset
// the marker set. Implemented in perfhud.cpp; called from the skeleton.
void PerfHud_CollectGpuMarkers(void);

// GPU timer-query marker bracing (librw gl3; forward-declared here so
// engine code can place markers without pulling in rwgl3impl.h).
namespace rw { namespace gl3 {
void gl3GpuMarkerBegin(const char *name);
void gl3GpuMarkerEnd(void);
// Draw-call trace (batching feasibility; see gl3render.cpp).
void gl3DrawTraceBegin(void);
void gl3DrawTraceEnd(void);
} }

// Draw the HUD with the engine font/sprite system. Call from the 2D render
// phase (after Render2dStuffAfterFade, before DoRWStuffEndOfFrame).
// No-op if disabled.
void
Hud_Draw(void);

// ---- telemetry backend (perfhud_stats.cpp) ----

// Opaque backend context: owns persistent file descriptors + CPU delta state.
struct HudStatsCtx;

// Sampled telemetry. A field is <0 (or 0 for clocks) when unavailable.
struct HudStatsSample {
	int cpuPct;     // whole-system CPU load 0-100 (per-frame delta)
	int armMhz;     // ARM core clock MHz (0 = unknown)
	int gpuMhz;     // GPU clock MHz (0 = unknown)
	int tempMilliC; // SoC temperature in milli-degrees C (-1 = unknown)
	int rssMb;      // this process's resident set size, MB (-1 = unknown)
	int sysMemPct;  // system memory used %, 0-100 (-1 = unknown)
};

HudStatsCtx *
HudStats_Create(void);

void
HudStats_Destroy(HudStatsCtx *ctx);

// Sample telemetry. cpuPct is computed every call (it needs a per-frame delta);
// the heavier clock/temp/memory reads are refreshed on a throttle inside and
// otherwise return the cached values, so this is cheap to call per frame.
void
HudStats_Sample(HudStatsCtx *ctx, HudStatsSample *out);

#endif /* REVC_PERF_H */
