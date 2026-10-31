// present_thread.cpp - triple-buffer present chain (docs/09 §3.1)
//
// Puts the camera's just-rendered frame on screen WITHOUT ever touching the
// EGL window surface:
//
//   main thread (Present_Submit, called from librw showRaster):
//     - takes a FREE gbm scanout bo (waits up to 100ms, else drops the frame;
//       SUBMITTED bos are never taken back - their fences belong to the
//       present thread)
//     - blits the camera FBO into it (bo imported as GL texture via dmabuf)
//       on the game's own context, i.e. directly behind this frame's draws
//     - fences render+blit and hands the bo to the present thread
//     - GPU throttle: waits for the frame MAX_FRAMES_IN_FLIGHT ago
//   present thread (this file):
//     - picks the newest COMPLETED submitted bo (older completed ones are
//       dropped), flips it at most once per panel refresh (mode.vrefresh),
//       and retires the previous front bo one period after the flip
//   See docs/09 §8.
//
// The point: the blob EGL surface's per-swap fence (the ~10ms
// poll(/dev/mali0) wait measured in docs/08 §7) never happens on the main
// thread, and the copy is never queued behind the NEXT frame on the GPU
// (when the present thread did the blit, it waited a full frame in rain and
// every other frame was dropped).
//
// Interface:
//   Present_Init(w,h)          - create bo pool, KMS resources, thread
//   Present_Submit(fbo,tex,next) - main thread, after rendering a frame
//   Present_Shutdown()
//   Present_Poll()             - optional per-frame query: pending frames
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <unistd.h>
#include <pthread.h>
#include <fcntl.h>
#include <errno.h>
#include <poll.h>
#include <dlfcn.h>
#include <time.h>

#include "gl3device_present.h"

static inline uint64_t
mono_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000ull + (uint64_t)ts.tv_nsec / 1000ull;
}

// ------------------------------------------------------------------ config
// Scanout bo pool. 3 is the real triple buffer (one scanned out, one
// waiting to latch, one being blitted into); with 2 the main thread may wait
// up to one refresh period for a bo. The blob once refused to import the
// 3rd bo as an FBO (on the old present-thread context); imports now run on
// the main context and any bo that fails is dropped - at least 2 required.
#define PRESENT_BO_COUNT 3

// ------------------------------------------------------------------ state
static struct {
	int inited;
	int width, height;
	uint32_t modeW, modeH;   // panel mode (bo/flip size)

	// GL objects (present thread context)
	pthread_t thread;
	pthread_mutex_t mtx;
	pthread_cond_t cv;
	int quit;

	// SDL-owned drm fd (master, page-flip capable); hasSdlfd guards the
	// static-zero fd=0 (stdin) trap
	int sdlfd;
	int hasSdlfd;

	// frame handoff (main -> present), at the SCANOUT bo level.
	// The main thread blits each finished frame into a free scanout bo right
	// after rendering it (same GL context, so the copy executes on the GPU
	// directly behind that frame - not behind the NEXT one, which is what the
	// old present-thread blit did: in rain it waited ~22ms and every other
	// frame was dropped). The present thread only waits for the fence and
	// page-flips. Mailbox: one pending bo; a newer frame replaces it.
	//
	// Selection rule: the present thread flips the NEWEST frame whose GPU
	// work has COMPLETED (older completed ones are dropped). It never waits
	// on the newest SUBMITTED frame's fence while an older one is already
	// done - waiting on the newest serialised the present loop onto the full
	// GPU queue latency (~28ms) and dropped 20-48% of frames in rain.
	int boState[PRESENT_BO_COUNT]; // BO_FREE / BO_MAIN / BO_SUBMITTED / BO_FLIPPING / BO_FRONT
	void *boSync[PRESENT_BO_COUNT];   // fence of a SUBMITTED bo (owned by the present thread)
	uint32_t boSeq[PRESENT_BO_COUNT]; // submit order
	uint64_t boSubmitUs[PRESENT_BO_COUNT];
	uint32_t seq;
	int front;                     // bo currently scanned out, -1 = none yet
	int flipping;                  // bo whose flip is queued but not latched yet
	uint64_t lastFlipUs;           // when `flipping` was queued
	pthread_cond_t freeCv;         // signalled when a bo becomes free

	// GPU back-pressure (main thread only): one fence per submitted frame;
	// before returning from Submit of frame N we wait for frame N-2. This
	// caps the frames the CPU can queue ahead of the GPU. Without it the
	// main thread (which no longer waits on any swap) ran hundreds of ms
	// ahead: fence waits of 70-265ms, 80-90% of frames rendered then
	// dropped - "stutters, then smooth, then stutters".
#define MAX_FRAMES_IN_FLIGHT 2
	void *inflightSync[MAX_FRAMES_IN_FLIGHT];
	int inflightHead;
	uint64_t gpuWaitUs;            // main thread time waiting on the GPU (throttle)

	// stats; guarded by mtx, reported by the present thread every few seconds
	uint32_t nSubmit, nDrop;
	uint64_t backpressureUs;       // main thread time blocked inside Submit
	// touched by the present thread only
	uint32_t nFlip, nFlipErr;
	uint32_t periodUs;             // panel refresh period (flip pacing)

	// bo pool (device path)
#ifdef PRESENT_USE_KMS
	int drmfd;
	int ownFd;                     // drmfd was opened by us (SDL's fd must never be closed here)
	struct gbm_device *gbm;
	struct gbm_bo *bos[PRESENT_BO_COUNT];
	uint32_t fbs[PRESENT_BO_COUNT];
	uint32_t boTex[PRESENT_BO_COUNT];  // dmabuf-imported textures (main ctx)
	uint32_t boFbo[PRESENT_BO_COUNT];  // main-ctx fbo drawing into boTex
	uint32_t boEglImage[PRESENT_BO_COUNT];
#endif
} P;

enum { BO_FREE = 0, BO_MAIN, BO_SUBMITTED, BO_FLIPPING, BO_FRONT, BO_DEAD };

// ============================================================ PC (SDL) path
// On the PC the "present" is just a swap on the main thread's context; the
// value of testing here is the frame-pacing/latency logic, not the fence
// semantics (Mesa has no blob-style surface fence).
#ifndef PRESENT_USE_KMS
// PC build (docs/09 §5 step 2): Mesa has no blob-style surface fence, so the
// submit callback degrades to a main-thread blit+swap (registered in sdl2.cpp,
// which owns the SDL window). This file only carries the device (KMS) path.
int Present_Init(int w, int h) { (void)w; (void)h; return 0; }
void Present_Start(void) {}
void Present_Shutdown(void) {}
void Present_Poll(void) {}
void Present_Submit(uint32_t fbo, uint32_t tex, uint32_t nextTex) { (void)fbo; (void)tex; (void)nextTex; }
void Present_SetGbm(void *gbm, void *eglDisplay, void *shareCtx) { (void)gbm; (void)eglDisplay; (void)shareCtx; }
void Present_SetDrmFd(int fd) { (void)fd; }
#endif // !PRESENT_USE_KMS

// ========================================================= device (KMS) path
#ifdef PRESENT_USE_KMS
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <dlfcn.h>
// GL entry points resolve through librw's glad table (function pointers,
// filled once for the process; valid on our present-thread context too).
#include "../../../vendor/librw/src/gl/glad/glad.h"

// Real drm/gbm headers, vendored from the device (tools/r36s/include/
// drm-vendor/). The builder image ships no libdrm-dev, and hand-rolled
// struct layouts cost us a day of debugging (drmModeRes field order:
// fbs, crtcs, connectors, encoders - NOT connectors before crtcs).
// Functions still resolve through dlopen: the system libgbm.so.1 is the
// blob's own trimmed one (no linker script for linking anyway).
#include "../../../tools/r36s/include/drm-vendor/xf86drm.h"
#include "../../../tools/r36s/include/drm-vendor/xf86drmMode.h"
#include "../../../tools/r36s/include/drm-vendor/gbm.h"

// ---- drm/gbm via dlopen ----------------------------------------------------
// On dArkOS the system libgbm.so.1 is a symlink to libMali.so (the blob's own
// trimmed gbm), and the PortMaster builder image ships no libdrm-dev linker
// scripts. Resolve everything at runtime instead; on the PC this whole TU is
// compiled out (no PRESENT_USE_KMS).
// Probe-verified on the device (tools probe_flip2.c):
//   - SDL_GetWindowWMInfo (2.0.16+ kmsdrm field, ABI-stable since 2.0.16)
//     gives the drm fd (DRM master) + the gbm_device EGL was created from.
//   - gbm_bo_create + drmModeAddFB + dmabuf import + page flip all work.
//   - drmModeAddFB2 HANGS on the blob gbm path; the legacy drmModeAddFB does
//     not. Always use drmModeAddFB.
static int (*p_drmModeAddFB)(int, uint32_t, uint32_t, uint8_t, uint8_t, uint32_t, uint32_t, uint32_t*);
static int (*p_drmModeRmFB)(int, uint32_t);
static int (*p_drmModePageFlip)(int, uint32_t, uint32_t, uint32_t, void*);
static drmModeRes *(*p_drmModeGetResources)(int);
static void (*p_drmModeFreeResources)(drmModeRes*);
static drmModeConnector *(*p_drmModeGetConnector)(int, uint32_t);
static void (*p_drmModeFreeConnector)(drmModeConnector*);
static drmModeEncoder *(*p_drmModeGetEncoder)(int, uint32_t);
static void (*p_drmModeFreeEncoder)(drmModeEncoder*);
static drmModeCrtc *(*p_drmModeGetCrtc)(int, uint32_t);
static void (*p_drmModeFreeCrtc)(drmModeCrtc*);
static int (*p_drmSetMaster)(int);
static int (*p_drmDropMaster)(int);
static int (*p_drmModeSetCrtc)(int, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t*, int, drmModeModeInfo*);
// gbm (from the blob's own libgbm)
static struct gbm_bo *(*p_gbm_bo_create)(struct gbm_device*, uint32_t, uint32_t, uint32_t, uint32_t);
static void (*p_gbm_bo_destroy)(struct gbm_bo*);
static uint32_t (*p_gbm_bo_get_stride)(struct gbm_bo*);
static int (*p_gbm_bo_get_fd)(struct gbm_bo*);
static union gbm_bo_handle (*p_gbm_bo_get_handle)(struct gbm_bo*);

static int
load_drm_gbm_syms(void)
{
	void *h_drm = dlopen("libdrm.so.2", RTLD_NOW | RTLD_GLOBAL);
	void *h_gbm = dlopen("libgbm.so.1", RTLD_NOW | RTLD_GLOBAL);
	if (!h_drm || !h_gbm) return -1;
#define LOAD(h, var, name) do { var = (__typeof__(var))dlsym(h, name); if (!var) return -1; } while (0)
	LOAD(h_drm, p_drmModeAddFB,        "drmModeAddFB");
	LOAD(h_drm, p_drmModeRmFB,         "drmModeRmFB");
	LOAD(h_drm, p_drmModePageFlip,     "drmModePageFlip");
	LOAD(h_drm, p_drmModeGetResources, "drmModeGetResources");
	LOAD(h_drm, p_drmModeFreeResources,"drmModeFreeResources");
	LOAD(h_drm, p_drmModeGetConnector, "drmModeGetConnector");
	LOAD(h_drm, p_drmModeFreeConnector,"drmModeFreeConnector");
	LOAD(h_drm, p_drmModeGetEncoder,   "drmModeGetEncoder");
	LOAD(h_drm, p_drmModeFreeEncoder,  "drmModeFreeEncoder");
	LOAD(h_drm, p_drmModeGetCrtc,      "drmModeGetCrtc");
	LOAD(h_drm, p_drmModeFreeCrtc,     "drmModeFreeCrtc");
	LOAD(h_drm, p_drmSetMaster,        "drmSetMaster");
	LOAD(h_drm, p_drmDropMaster,       "drmDropMaster");
	LOAD(h_drm, p_drmModeSetCrtc,      "drmModeSetCrtc");
	LOAD(h_gbm, p_gbm_bo_create,       "gbm_bo_create");
	LOAD(h_gbm, p_gbm_bo_destroy,      "gbm_bo_destroy");
	LOAD(h_gbm, p_gbm_bo_get_stride,   "gbm_bo_get_stride");
	LOAD(h_gbm, p_gbm_bo_get_fd,       "gbm_bo_get_fd");
	LOAD(h_gbm, p_gbm_bo_get_handle,   "gbm_bo_get_handle");
#undef LOAD
	return 0;
}

static EGLDisplay gDpy;
static EGLContext gCtx;
static PFNEGLCREATEIMAGEKHRPROC eglCreateImageKHR;
static PFNEGLDESTROYIMAGEKHRPROC eglDestroyImageKHR;
// GLES eglext.h has no PROC typedef for this OES entry point (and the
// glad profile may lack it too); declare it ourselves.
typedef void (*PFNGLIMAGETARGETTEXTURE2D)(unsigned int target, void *image);
static PFNGLIMAGETARGETTEXTURE2D glEGLImageTargetTexture2DOES;
static PFNEGLCREATESYNCKHRPROC eglCreateSyncKHR;
static PFNEGLDESTROYSYNCKHRPROC eglDestroySyncKHR;
static PFNEGLCLIENTWAITSYNCKHRPROC eglClientWaitSyncKHR;
static PFNEGLWAITSYNCKHRPROC eglWaitSyncKHR;

// ---- EGL via dlopen("libEGL.so") ----------------------------------------
// The binary links libEGL.so.1 = glvnd, which has no Mali vendor on dArkOS
// (eglGetCurrentDisplay returns nil). SDL itself dlopen's libEGL.so, which
// symlinks to libMali.so - that's the EGL world the render context lives
// in. Resolve the blob's EGL directly, same as SDL does.
typedef void *EGLDisplayT;
typedef void *EGLContextT;
static void *gEglHandle;
static EGLDisplay (*p_eglGetDisplay)(void *);
static EGLBoolean (*p_eglInitialize)(EGLDisplay, EGLint *, EGLint *);
static EGLContext (*p_eglCreateContext)(EGLDisplay, EGLConfig, EGLContext, const EGLint *);
static EGLBoolean (*p_eglMakeCurrent)(EGLDisplay, EGLSurface, EGLSurface, EGLContext);
static EGLBoolean (*p_eglDestroyContext)(EGLDisplay, EGLContext);
static void *(*p_eglGetProcAddress)(const char *);
static EGLBoolean (*p_eglBindAPI)(EGLenum);

static int
load_egl_syms(void)
{
	gEglHandle = dlopen("libEGL.so", RTLD_NOW | RTLD_LOCAL);
	if (!gEglHandle) gEglHandle = dlopen("libEGL.so.1", RTLD_NOW | RTLD_LOCAL);
	if (!gEglHandle) return -1;
#define LOADE(var, name) do { var = (__typeof__(var))dlsym(gEglHandle, name); if (!var) return -1; } while (0)
	LOADE(p_eglGetDisplay,       "eglGetDisplay");
	LOADE(p_eglInitialize,       "eglInitialize");
	LOADE(p_eglCreateContext,    "eglCreateContext");
	LOADE(p_eglMakeCurrent,      "eglMakeCurrent");
	LOADE(p_eglGetProcAddress,   "eglGetProcAddress");
	LOADE(p_eglBindAPI,          "eglBindAPI");
	// optional: only needed to tear the present context down at exit
	p_eglDestroyContext = (__typeof__(p_eglDestroyContext))dlsym(gEglHandle, "eglDestroyContext");
#undef LOADE
	return 0;
}

// drm resources
static drmModeConnector *gConn;
static drmModeCrtc *gCrtc;
static uint32_t gConnId, gCrtcId;

static int
find_crtc_for_connector(int fd, drmModeRes *res, drmModeConnector *conn)
{
	for (int i = 0; i < conn->count_encoders; i++) {
		drmModeEncoder *enc = p_drmModeGetEncoder(fd, conn->encoders[i]);
		if (!enc) continue;
		for (int j = 0; j < res->count_crtcs; j++) {
			if (enc->possible_crtcs & (1u << j)) {
				gCrtcId = res->crtcs[j];
				p_drmModeFreeEncoder(enc);
				return 0;
			}
		}
		p_drmModeFreeEncoder(enc);
	}
	return -1;
}

// Import bo i as GL texture + fbo on the CURRENT context (main thread).
static int
import_bo(int i)
{
	if (!P.bos[i] || !eglCreateImageKHR || !eglDestroyImageKHR || !glEGLImageTargetTexture2DOES)
		return -1;
	int fd = p_gbm_bo_get_fd(P.bos[i]);
	if (fd < 0) return -1;
	EGLint attrs[] = {
		EGL_WIDTH, (EGLint)P.modeW, EGL_HEIGHT, (EGLint)P.modeH,
		EGL_LINUX_DRM_FOURCC_EXT, GBM_FORMAT_XRGB8888,
		EGL_DMA_BUF_PLANE0_FD_EXT, fd,
		EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
		EGL_DMA_BUF_PLANE0_PITCH_EXT, (EGLint)p_gbm_bo_get_stride(P.bos[i]),
		EGL_NONE };
	P.boEglImage[i] = (uint32_t)(uintptr_t)eglCreateImageKHR(gDpy, EGL_NO_CONTEXT,
	                       EGL_LINUX_DMA_BUF_EXT, (EGLClientBuffer)NULL, attrs);
	close(fd);
	if (P.boEglImage[i] == 0) { printf("[present] import bo %d failed\n", i); return -1; }
	GLuint tex = 0, fbo = 0;
	glGenTextures(1, &tex);
	P.boTex[i] = tex;
	glBindTexture(GL_TEXTURE_2D, P.boTex[i]);
	glEGLImageTargetTexture2DOES(GL_TEXTURE_2D, (void*)(uintptr_t)P.boEglImage[i]);
	glGenFramebuffers(1, &fbo);
	P.boFbo[i] = fbo;
	glBindFramebuffer(GL_FRAMEBUFFER, P.boFbo[i]);
	glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, P.boTex[i], 0);
	if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
		printf("[present] bo fbo %d incomplete (glerr=0x%x)", i, glGetError());
		while (glGetError() != GL_NO_ERROR) {}
		printf("\n");
		glDeleteTextures(1, &tex);
		glDeleteFramebuffers(1, &fbo);
		P.boTex[i] = 0; P.boFbo[i] = 0;
		if (P.boEglImage[i]) { eglDestroyImageKHR(gDpy, (EGLImageKHR)(uintptr_t)P.boEglImage[i]); P.boEglImage[i] = 0; }
		return -1;
	}
	return 0;
}


int
Present_Init(int w, int h)
{
	// NOTE: do NOT memset P here - Present_SetGbm/Present_SetDrmFd already
	// filled in the gbm device / share ctx / fd handed over by the skeleton
	// (that wipe is why "no gbm device" ever happened). Only reset the
	// fields this function owns.
	P.inited = 0;
	P.quit = 0;                    // re-init after a Present_Shutdown (video mode change)
	P.width = w; P.height = h;

	if (load_drm_gbm_syms() < 0) { printf("[present] dlopen drm/gbm failed\n"); return -1; }

	// Open our own card0. Probe-verified (t4): a fresh /dev/dri/card0 fd
	// enumerates connectors fine even while another process holds master;
	// SDL's handed-over fd was unreliable (GetResources/GetConnector NULL).
	// drmModePageFlip needs master - the launch flow runs the game after ES
	// gives up the display, so we try to TAKE master here; if that fails we
	// still fall back to swap (the chain reports it).
	P.drmfd = open("/dev/dri/card0", O_RDWR | O_CLOEXEC);
	if (P.drmfd < 0) { printf("[present] open card0: %s\n", strerror(errno)); return -1; }
	P.ownFd = 1;
	int master_rc = p_drmSetMaster(P.drmfd);
	printf("[present] card0 fd=%d drmSetMaster rc=%d\n", P.drmfd, master_rc);
	if (master_rc != 0 && P.hasSdlfd && P.sdlfd >= 0) {
		// couldn't take master; prefer SDL's fd if it is a working one
		drmModeRes *probe = p_drmModeGetResources(P.sdlfd);
		if (probe) { p_drmModeFreeResources(probe); close(P.drmfd); P.drmfd = P.sdlfd; P.ownFd = 0;
			printf("[present] using SDL fd %d instead\n", P.sdlfd); }
	}

	drmModeRes *res = p_drmModeGetResources(P.drmfd);
	if (!res) { printf("[present] drmModeGetResources failed (fd=%d)\n", P.drmfd); return -1; }
	printf("[present] res: conns=%d crtcs=%d\n", res->count_connectors, res->count_crtcs);
	for (int i = 0; i < res->count_connectors; i++) {
		drmModeConnector *c = p_drmModeGetConnector(P.drmfd, res->connectors[i]);
		if (!c) { printf("[present] conn[%d]=NULL id=%u\n", i, res->connectors[i]); continue; }
		printf("[present] conn[%d] id=%u conn=%d modes=%d encs=%d\n",
		       i, c->connector_id, c->connection, c->count_modes, c->count_encoders);
		if (c->connection == DRM_MODE_CONNECTED && c->count_modes > 0) {
			gConn = c; gConnId = c->connector_id;
			break;
		}
		p_drmModeFreeConnector(c);
	}
	if (!gConn) { printf("[present] no connected connector\n"); return -1; }
	if (find_crtc_for_connector(P.drmfd, res, gConn) < 0) { printf("[present] no crtc\n"); return -1; }
	p_drmModeFreeResources(res);
	gCrtc = p_drmModeGetCrtc(P.drmfd, gCrtcId);
	// Panel mode: the connector's first mode (SDL picked it too).
	P.modeW = gConn->modes[0].hdisplay; P.modeH = gConn->modes[0].vdisplay;
	if (P.modeW != (uint32_t)w || P.modeH != (uint32_t)h) {
		printf("[present] NOTE: bo size %dx%d != mode %ux%u (blit scales)\n", w, h, P.modeW, P.modeH);
	}
	// Flip pacing follows the REAL panel refresh (the R36S panel runs 640x480
	// at ~78Hz, not 60); the old hard-coded usleep(16000) assumed 60Hz.
	{
		uint32_t vr = gConn->modes[0].vrefresh;
		if (vr < 30 || vr > 240) vr = 60;
		P.periodUs = 1000000u / vr;
		printf("[present] mode %ux%u@%uHz (flip period %uus) conn=%u crtc=%u\n",
		       P.modeW, P.modeH, vr, P.periodUs, gConnId, gCrtcId);
	}

	// gbm + bo pool. Must use the SAME gbm device the EGL display was created
	// from for imports to work; the caller passes it in via Present_SetGbm.
	if (!P.gbm) { printf("[present] no gbm device (call Present_SetGbm)\n"); return -1; }

	for (int i = 0; i < PRESENT_BO_COUNT; i++) {
		P.bos[i] = p_gbm_bo_create(P.gbm, P.modeW, P.modeH, GBM_FORMAT_XRGB8888,
		                         GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING);
		if (!P.bos[i]) { printf("[present] bo %d create failed\n", i); return -1; }
		uint32_t stride = p_gbm_bo_get_stride(P.bos[i]);
		uint32_t handle = p_gbm_bo_get_handle(P.bos[i]).u32;
		// drm framebuffer per bo. NOTE: drmModeAddFB2 HANGS with the blob gbm
		// (probe-verified); the legacy drmModeAddFB (depth 24, bpp 32) works.
		int ret = p_drmModeAddFB(P.drmfd, P.modeW, P.modeH, 24, 32, stride, handle, &P.fbs[i]);
		if (ret) { printf("[present] AddFB %d: %s\n", i, strerror(errno)); return -1; }
	}
	printf("[present] %d bos created\n", PRESENT_BO_COUNT);

	pthread_mutex_init(&P.mtx, NULL);
	pthread_cond_init(&P.cv, NULL);
	pthread_cond_init(&P.freeCv, NULL);
	for (int i = 0; i < PRESENT_BO_COUNT; i++) { P.boSync[i] = NULL; P.boSeq[i] = 0; P.boSubmitUs[i] = 0; }
	P.seq = 0;
	P.front = -1;
	P.flipping = -1;
	P.lastFlipUs = 0;
	P.nSubmit = P.nDrop = P.nFlip = P.nFlipErr = 0;
	P.backpressureUs = 0;
	for (int i = 0; i < MAX_FRAMES_IN_FLIGHT; i++) P.inflightSync[i] = NULL;
	P.inflightHead = 0;
	P.gpuWaitUs = 0;

	// Import the scanout bos as textures + FBOs on THIS thread: Present_Init
	// runs on the main thread with the game's GL context current, and the
	// main thread is the one that blits into them. Any bo the blob refuses
	// is dropped; fewer than 2 usable bos -> fail (caller falls back to swap).
	// librw has the camera FBO bound and caches texture/FBO bindings:
	// restore exactly what was bound, or its cache goes stale.
	GLint prevDraw = 0, prevRead = 0, prevTex = 0;
	glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, &prevDraw);
	glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING, &prevRead);
	glGetIntegerv(GL_TEXTURE_BINDING_2D, &prevTex);
	int okcnt = 0;
	for (int i = 0; i < PRESENT_BO_COUNT; i++) {
		if (import_bo(i) == 0) { P.boState[i] = BO_FREE; okcnt++; }
		else P.boState[i] = BO_DEAD;
	}
	glBindTexture(GL_TEXTURE_2D, (GLuint)prevTex);
	glBindFramebuffer(GL_DRAW_FRAMEBUFFER, (GLuint)prevDraw);
	glBindFramebuffer(GL_READ_FRAMEBUFFER, (GLuint)prevRead);
	if (okcnt < 2) {
		printf("[present] only %d/%d bos usable on the main context - need 2\n", okcnt, PRESENT_BO_COUNT);
		return -1;
	}
	printf("[present] %d/%d scanout bos imported (main-thread blit)\n", okcnt, PRESENT_BO_COUNT);
	P.inited = 1;
	return 0;
}

void
Present_SetDrmFd(int fd)
{
	P.sdlfd = fd;
	P.hasSdlfd = 1;
}
void
Present_SetGbm(void *gbm, void *eglDisplay, void *shareCtx)
{
	P.gbm = (struct gbm_device *)gbm;
	gDpy = (EGLDisplay)eglDisplay;

	// The binary links libEGL.so.1 = glvnd, which has NO Mali vendor on
	// dArkOS (eglGetCurrentDisplay returned nil). SDL itself dlopen's
	// libEGL.so -> libMali.so; resolve the blob's EGL the same way.
	if (load_egl_syms() < 0) { printf("[present] dlopen libEGL.so failed\n"); return; }

	// If no usable display was handed over, (re)create one from the gbm
	// device - same driver instance the render context lives in.
	EGLint maj = 0, min = 0;
	if (!gDpy)
		gDpy = p_eglGetDisplay(gbm);
	if (!p_eglInitialize(gDpy, &maj, &min)) {
		printf("[present] eglInitialize(dpy=%p) failed 0x%x, retry from gbm\n", gDpy, eglGetError());
		gDpy = p_eglGetDisplay(gbm);
		if (!p_eglInitialize(gDpy, &maj, &min)) { printf("[present] eglInitialize retry failed\n"); return; }
	}

	// Present context: shares textures with the main context.
	// GLES3 (blob has no desktop GL; docs/09 probe result).
	p_eglBindAPI(EGL_OPENGL_ES_API);
	EGLint attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE };
	gCtx = p_eglCreateContext(gDpy, EGL_NO_CONFIG_KHR, (EGLContext)shareCtx, attribs);
	if (gCtx == EGL_NO_CONTEXT) {
		printf("[present] share ctx failed (err 0x%x, dpy=%p share=%p)\n",
		       eglGetError(), gDpy, shareCtx);
		// Retry WITHOUT sharing (imports then fail, but the chain still
		// reports what broke instead of silently dying).
		gCtx = p_eglCreateContext(gDpy, EGL_NO_CONFIG_KHR, NULL, attribs);
		if (gCtx == EGL_NO_CONTEXT) { printf("[present] unshared ctx also failed\n"); return; }
		printf("[present] NOTE: running unshared (share failed)\n");
	}

	eglCreateImageKHR = (PFNEGLCREATEIMAGEKHRPROC)p_eglGetProcAddress("eglCreateImageKHR");
	eglDestroyImageKHR = (PFNEGLDESTROYIMAGEKHRPROC)p_eglGetProcAddress("eglDestroyImageKHR");
	glEGLImageTargetTexture2DOES = (PFNGLIMAGETARGETTEXTURE2D)p_eglGetProcAddress("glEGLImageTargetTexture2DOES");
	eglCreateSyncKHR = (PFNEGLCREATESYNCKHRPROC)p_eglGetProcAddress("eglCreateSyncKHR");
	eglDestroySyncKHR = (PFNEGLDESTROYSYNCKHRPROC)p_eglGetProcAddress("eglDestroySyncKHR");
	eglClientWaitSyncKHR = (PFNEGLCLIENTWAITSYNCKHRPROC)p_eglGetProcAddress("eglClientWaitSyncKHR");
	eglWaitSyncKHR = (PFNEGLWAITSYNCKHRPROC)p_eglGetProcAddress("eglWaitSyncKHR");
	printf("[present] share ctx ok (EGL %d.%d), exts %s, fence %s\n", maj, min,
	       eglCreateImageKHR && glEGLImageTargetTexture2DOES ? "ok" : "MISSING",
	       eglCreateSyncKHR && eglDestroySyncKHR && (eglWaitSyncKHR || eglClientWaitSyncKHR)
	           ? (eglWaitSyncKHR ? "server-wait" : "client-wait") : "MISSING (glFinish fallback)");
}

// With P.mtx held: once the queued flip has certainly latched (one refresh
// period after the flip call; SDL steals the flip events), the bo it
// replaced on screen is no longer read and goes back to the pool.
static void
retire_if_due_locked(uint64_t now)
{
	if (P.flipping < 0 || now < P.lastFlipUs + P.periodUs) return;
	if (P.front >= 0 && P.boState[P.front] == BO_FRONT)
		P.boState[P.front] = BO_FREE;
	P.front = P.flipping;
	P.boState[P.front] = BO_FRONT;
	P.flipping = -1;
	pthread_cond_broadcast(&P.freeCv);
}

static int
sync_done(void *sync)
{
	if (!sync) return 1; // glFinish fallback: already complete
	return eglClientWaitSyncKHR(gDpy, (EGLSyncKHR)sync, 0, 0) == EGL_CONDITION_SATISFIED_KHR;
}

static void *
present_thread(void *arg)
{
	(void)arg;
	// No GL work happens here (the blit runs on the main thread); the share
	// context is made current only so EGL sync calls have a defined context.
	p_eglMakeCurrent(gDpy, EGL_NO_SURFACE, EGL_NO_SURFACE, gCtx);

	uint64_t lastReport = mono_us();
	uint32_t rFlip = 0, rSub = 0, rDrop = 0;
	uint64_t rBp = 0, rLat = 0, rGw = 0;

	pthread_mutex_lock(&P.mtx);
	while (!P.quit) {
		retire_if_due_locked(mono_us());

		// newest completed frame, and the oldest still-running one
		int pick = -1, oldest = -1;
		for (int i = 0; i < PRESENT_BO_COUNT; i++) {
			if (P.boState[i] != BO_SUBMITTED) continue;
			if (oldest < 0 || P.boSeq[i] < P.boSeq[oldest]) oldest = i;
			if (sync_done(P.boSync[i]) && (pick < 0 || P.boSeq[i] > P.boSeq[pick])) pick = i;
		}

		if (pick < 0) {
			uint64_t now = mono_us();
			uint64_t untilRetire = (P.flipping >= 0 && P.lastFlipUs + P.periodUs > now)
			                       ? P.lastFlipUs + P.periodUs - now : 0;
			if (oldest >= 0) {
				// GPU still busy: wait on the OLDEST frame (it completes
				// first). The main thread never touches SUBMITTED bos or
				// their fences, so this is safe without the lock. Bounded
				// so a due retire is never delayed by more than ~4ms.
				void *sy = P.boSync[oldest];
				uint64_t to = 4000;
				if (P.flipping >= 0 && untilRetire < to) to = untilRetire ? untilRetire : 500;
				pthread_mutex_unlock(&P.mtx);
				eglClientWaitSyncKHR(gDpy, (EGLSyncKHR)sy, 0, (EGLTimeKHR)(to * 1000ull));
				pthread_mutex_lock(&P.mtx);
			} else if (P.flipping >= 0) {
				// nothing submitted: sleep until the queued flip retires
				struct timespec dl;
				clock_gettime(CLOCK_REALTIME, &dl);
				uint64_t ns = (uint64_t)dl.tv_nsec + (untilRetire ? untilRetire : 500) * 1000ull;
				dl.tv_sec += ns / 1000000000ull; dl.tv_nsec = ns % 1000000000ull;
				pthread_cond_timedwait(&P.cv, &P.mtx, &dl);
			} else {
				pthread_cond_wait(&P.cv, &P.mtx);
			}
			continue;
		}

		// Take `pick`; older completed frames were superseded before they
		// could be shown (frames are completed in submit order, so every
		// older SUBMITTED one is done too).
		for (int i = 0; i < PRESENT_BO_COUNT; i++) {
			if (i == pick || P.boState[i] != BO_SUBMITTED || P.boSeq[i] > P.boSeq[pick]) continue;
			if (P.boSync[i]) eglDestroySyncKHR(gDpy, (EGLSyncKHR)P.boSync[i]);
			P.boSync[i] = NULL;
			P.boState[i] = BO_FREE;
			P.nDrop++;
		}
		if (P.boSync[pick]) eglDestroySyncKHR(gDpy, (EGLSyncKHR)P.boSync[pick]);
		P.boSync[pick] = NULL;
		P.boState[pick] = BO_FLIPPING;
		rLat += mono_us() - P.boSubmitUs[pick];
		pthread_cond_broadcast(&P.freeCv);

		// One flip per refresh: the 4.4 rockchip driver rejects a second
		// flip while one is pending (and DRM_MODE_PAGE_FLIP_ASYNC: EINVAL).
		uint64_t now = mono_us();
		if (P.flipping >= 0 && now < P.lastFlipUs + P.periodUs) {
			uint64_t d = P.lastFlipUs + P.periodUs - now;
			pthread_mutex_unlock(&P.mtx);
			usleep((useconds_t)d);
			pthread_mutex_lock(&P.mtx);
		}
		retire_if_due_locked(mono_us());

		// queue the flip (vblank-aligned; NO event flag - SDL KMSDRM polls the
		// same fd and its drmHandleEvent would steal the event)
		pthread_mutex_unlock(&P.mtx);
		uint64_t tFlip = mono_us();
		int ret = p_drmModePageFlip(P.drmfd, gCrtcId, P.fbs[pick], 0, NULL);
		pthread_mutex_lock(&P.mtx);
		if (ret) {
			P.nFlipErr++;
			if (P.nFlipErr <= 5 || (P.nFlipErr % 200) == 0) {
				printf("[present] flip: %s (err #%u)\n", strerror(errno), P.nFlipErr);
				fflush(stdout);
			}
			P.boState[pick] = BO_FREE;
			pthread_cond_broadcast(&P.freeCv);
			continue;
		}
		P.nFlip++;
		P.flipping = pick;
		P.lastFlipUs = tFlip;

		// ~3s stats line: frames actually shown vs rendered (the HUD fps
		// counts rendered frames, not displayed ones)
		uint64_t t = mono_us();
		if (t - lastReport >= 3000000ull) {
			uint32_t sub = P.nSubmit, drop = P.nDrop;
			uint64_t bp = P.backpressureUs, gw = P.gpuWaitUs;
			double secs = (double)(t - lastReport) / 1e6;
			uint32_t df = P.nFlip - rFlip, ds = sub - rSub, dd = drop - rDrop;
			printf("[present] %.1fs: shown=%.1f/s submitted=%.1f/s dropped=%u (%.0f%%) "
			       "lat=%.2fms gpuwait=%.2fms/s mainblock=%.2fms/s fliperr=%u\n",
			       secs, df / secs, ds / secs, dd, ds ? 100.0 * dd / ds : 0.0,
			       df ? (rLat / 1000.0) / df : 0.0,
			       (double)(gw - rGw) / 1000.0 / secs,
			       (double)(bp - rBp) / 1000.0 / secs, P.nFlipErr);
			fflush(stdout);
			lastReport = t; rFlip = P.nFlip; rSub = sub; rDrop = drop;
			rBp = bp; rGw = gw; rLat = 0;
		}
	}
	pthread_mutex_unlock(&P.mtx);
	// a context left current on a dying thread confuses the blob's EGL teardown
	p_eglMakeCurrent(gDpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
	return NULL;
}


void
Present_Start(void)
{
	if (!P.inited) return;
	pthread_create(&P.thread, NULL, present_thread, NULL);
}

// Pick a free bo for the main thread to blit into, with P.mtx held; -1 if
// none. SUBMITTED bos are never taken back here: their fences belong to the
// present thread (which may be waiting on them unlocked).
static int
take_bo_locked(void)
{
	for (int i = 0; i < PRESENT_BO_COUNT; i++)
		if (P.boState[i] == BO_FREE) { P.boState[i] = BO_MAIN; return i; }
	return -1;
}

void
Present_Submit(uint32_t fbo, uint32_t tex, uint32_t nextTex)
{
	(void)tex; (void)nextTex; // only the main thread touches camera textures now
	if (!P.inited) return;

	pthread_mutex_lock(&P.mtx);
	P.nSubmit++;
	int bo = take_bo_locked();
	if (bo < 0) {
		// All bos busy (scanned / latching / waiting to be shown). Wait,
		// bounded, so a wedged present thread can never hang the game: on
		// timeout the frame is simply not shown.
		uint64_t t0 = mono_us();
		struct timespec dl;
		clock_gettime(CLOCK_REALTIME, &dl);
		dl.tv_nsec += 100 * 1000000L;
		if (dl.tv_nsec >= 1000000000L) { dl.tv_sec++; dl.tv_nsec -= 1000000000L; }
		while (bo < 0 && !P.quit) {
			if (pthread_cond_timedwait(&P.freeCv, &P.mtx, &dl) != 0) break;
			bo = take_bo_locked();
		}
		P.backpressureUs += mono_us() - t0;
		if (bo < 0) { P.nDrop++; pthread_mutex_unlock(&P.mtx); return; }
	}
	pthread_mutex_unlock(&P.mtx);

	// Blit the just-rendered camera frame into the scanout bo, on this
	// (main) context: the copy is queued directly behind this frame's draws.
	// Y-flip: GL texture origin is bottom-up, the panel scans top-down.
	// Blits ignore blend/depth/masks but honour the scissor test.
	GLboolean scissor = glIsEnabled(GL_SCISSOR_TEST);
	if (scissor) glDisable(GL_SCISSOR_TEST);
	glBindFramebuffer(GL_READ_FRAMEBUFFER, fbo);
	glBindFramebuffer(GL_DRAW_FRAMEBUFFER, P.boFbo[bo]);
	glBlitFramebuffer(0, P.height, P.width, 0,
	                  0, 0, P.modeW, P.modeH,
	                  GL_COLOR_BUFFER_BIT, GL_NEAREST);
	if (scissor) glEnable(GL_SCISSOR_TEST);
	// (librw rebinds its render target after this callback returns)

	// Two fences at the same point (render + blit, same context, in order):
	// one handed to the present thread with the bo, one kept here for the
	// GPU throttle below. Flush so the GPU starts now.
	int useFence = eglCreateSyncKHR && eglDestroySyncKHR && eglClientWaitSyncKHR;
	void *sync = NULL, *thr = NULL;
	if (useFence) {
		EGLSyncKHR s1 = eglCreateSyncKHR(gDpy, EGL_SYNC_FENCE_KHR, NULL);
		EGLSyncKHR s2 = eglCreateSyncKHR(gDpy, EGL_SYNC_FENCE_KHR, NULL);
		if (s1 != EGL_NO_SYNC_KHR) sync = (void *)s1;
		if (s2 != EGL_NO_SYNC_KHR) thr = (void *)s2;
	}
	if (sync) glFlush();
	else glFinish(); // no usable fence: block until the frame is really done

	pthread_mutex_lock(&P.mtx);
	P.boSync[bo] = sync;
	P.boSeq[bo] = ++P.seq;
	P.boSubmitUs[bo] = mono_us();
	P.boState[bo] = BO_SUBMITTED;
	pthread_cond_signal(&P.cv);
	pthread_mutex_unlock(&P.mtx);

	// GPU back-pressure: wait until the frame submitted MAX_FRAMES_IN_FLIGHT
	// ago has finished on the GPU, so the CPU stays at most that far ahead
	// (unbounded, the GPU queue grew to 100-265ms of latency).
	int slot = P.inflightHead;
	void *older = P.inflightSync[slot];
	P.inflightSync[slot] = thr;
	P.inflightHead = (slot + 1) % MAX_FRAMES_IN_FLIGHT;
	if (older) {
		uint64_t g0 = mono_us();
		eglClientWaitSyncKHR(gDpy, (EGLSyncKHR)older, EGL_SYNC_FLUSH_COMMANDS_BIT_KHR,
		                     (EGLTimeKHR)500000000ull); // 500ms cap
		eglDestroySyncKHR(gDpy, (EGLSyncKHR)older);
		P.gpuWaitUs += mono_us() - g0;
	}
}

void
Present_Poll(void) {}

void
Present_Shutdown(void)
{
	if (!P.inited) return;
	P.inited = 0;          // idempotent; Present_Submit() ignores calls from now on
	printf("[present] shutdown: stopping flip thread\n"); fflush(stdout);

	// 1. Stop the flip thread FIRST. It used to be left running while SDL tore
	//    KMS down (this function was never called), flipping bos that were
	//    about to vanish.
	pthread_mutex_lock(&P.mtx);
	P.quit = 1;
	pthread_cond_signal(&P.cv);
	pthread_cond_broadcast(&P.freeCv); // release a Submit() blocked on a bo
	pthread_mutex_unlock(&P.mtx);
	pthread_join(P.thread, NULL);

	// 2. The GPU must be done writing into the scanout bos (main-context
	//    blits) before they are unmapped or freed; the display must also have
	//    latched the last queued flip before we change the CRTC.
	glFinish();
	for (int i = 0; i < PRESENT_BO_COUNT; i++)
		if (P.boSync[i]) { eglDestroySyncKHR(gDpy, (EGLSyncKHR)P.boSync[i]); P.boSync[i] = NULL; }
	for (int i = 0; i < MAX_FRAMES_IN_FLIGHT; i++)
		if (P.inflightSync[i]) { eglDestroySyncKHR(gDpy, (EGLSyncKHR)P.inflightSync[i]); P.inflightSync[i] = NULL; }
	usleep(2 * P.periodUs);

	// 3. Take the CRTC off our FBs before they are removed. Removing an FB
	//    that is being scanned out makes the kernel disable the CRTC behind
	//    our back. Put back what was on screen when the chain started (SDL's
	//    own FB, still alive here), or disable the CRTC explicitly if that is
	//    not usable. SetCrtc needs master: harmless EACCES if we lost it.
	{
		int rc;
		if (gCrtc && gCrtc->buffer_id && gCrtc->mode_valid)
			rc = p_drmModeSetCrtc(P.drmfd, gCrtcId, gCrtc->buffer_id, gCrtc->x, gCrtc->y,
			                      &gConnId, 1, &gCrtc->mode);
		else
			rc = p_drmModeSetCrtc(P.drmfd, gCrtcId, 0, 0, 0, NULL, 0, NULL);
		if (rc != 0 && gCrtc && gCrtc->buffer_id) {
			// the saved FB is gone: disable instead of leaving it dangling
			rc = p_drmModeSetCrtc(P.drmfd, gCrtcId, 0, 0, 0, NULL, 0, NULL);
			printf("[present] shutdown: saved crtc fb unusable, crtc disabled rc=%d\n", rc);
		} else {
			printf("[present] shutdown: crtc %s rc=%d\n",
			       (gCrtc && gCrtc->buffer_id && gCrtc->mode_valid) ? "restored" : "disabled", rc);
		}
		fflush(stdout);
	}

	// 4. GL objects on the main context, then the FBs and the bos.
	for (int i = 0; i < PRESENT_BO_COUNT; i++) {
		if (P.boFbo[i]) { glDeleteFramebuffers(1, &P.boFbo[i]); P.boFbo[i] = 0; }
		if (P.boTex[i]) { glDeleteTextures(1, &P.boTex[i]); P.boTex[i] = 0; }
		if (P.boEglImage[i]) { eglDestroyImageKHR(gDpy, (EGLImageKHR)(uintptr_t)P.boEglImage[i]); P.boEglImage[i] = 0; }
		if (P.fbs[i]) { p_drmModeRmFB(P.drmfd, P.fbs[i]); P.fbs[i] = 0; }
		if (P.bos[i]) { p_gbm_bo_destroy(P.bos[i]); P.bos[i] = NULL; }
		P.boState[i] = BO_DEAD;
	}

	// 5. the present (share) context. The gbm device and EGL display belong to
	//    SDL and are released by it.
	if (gCtx != EGL_NO_CONTEXT && p_eglDestroyContext) p_eglDestroyContext(gDpy, gCtx);
	gCtx = EGL_NO_CONTEXT;

	if (gCrtc) { p_drmModeFreeCrtc(gCrtc); gCrtc = NULL; }
	if (gConn) { p_drmModeFreeConnector(gConn); gConn = NULL; }
	// 6. only close an fd we opened; the fd SDL handed over is SDL's (it still
	//    restores the display and destroys its gbm device through it).
	if (P.ownFd && P.drmfd >= 0) {
		p_drmDropMaster(P.drmfd);
		close(P.drmfd);
	}
	P.drmfd = -1; P.ownFd = 0;
	printf("[present] shutdown: done\n"); fflush(stdout);
}
#endif // PRESENT_USE_KMS
