// present_thread.cpp - triple-buffer present chain (docs/09 §3.1)
//
// Takes the camera's just-rendered FBO (ping-pong pair in librw) and puts it
// on screen WITHOUT ever touching the EGL window surface:
//
//   main thread:  render -> FBO_A -> flip -> record (never swaps)
//   present thread (this file):
//     - takes the previous frame's completed FBO
//     - blits it into a gbm scanout bo (imported as a GL texture FBO via
//       dmabuf; on PC/SDL builds falls back to the window backbuffer)
//     - queues a non-blocking drmModePageFlip (device) / SDL_GL_SwapWindow (PC)
//     - waits for the flip event to return the bo to the pool
//
// The whole point: the blob EGL surface's per-swap fence (the ~10ms
// poll(/dev/mali0) wait measured in docs/08 §7) never happens on the main
// thread; CPU submit and GPU drain run in parallel.
//
// Interface:
//   Present_Init(w,h)          - create bo pool, KMS resources, thread
//   Present_Submit(fbo,tex)    - main thread, after rendering a frame
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

#include "gl3device_present.h"

// ------------------------------------------------------------------ config
// 3 would be ideal (render + flip-pending + free) but the blob choked on
// importing the 3rd bo's dmabuf as an FBO ("bo fbo 2 incomplete") - 2 is
// enough: one being scanned out, one being blitted into.
#define PRESENT_BO_COUNT 2

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

	// frame handoff: main -> present
	volatile int pendingCount;
	struct { uint32_t fbo; uint32_t tex; } pending[4];

	// bo pool (device path)
#ifdef PRESENT_USE_KMS
	int drmfd;
	struct gbm_device *gbm;
	struct gbm_bo *bos[PRESENT_BO_COUNT];
	uint32_t fbs[PRESENT_BO_COUNT];
	int boBusy[PRESENT_BO_COUNT];    // 0 free, 1 pending flip
	uint32_t boTex[PRESENT_BO_COUNT];  // dmabuf-imported textures
	uint32_t boFbo[PRESENT_BO_COUNT];  // fbo rendering into boTex
	uint32_t boEglImage[PRESENT_BO_COUNT];
#endif
} P;

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
void Present_Submit(uint32_t fbo, uint32_t tex) { (void)fbo; (void)tex; }
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

int
Present_Init(int w, int h)
{
	// NOTE: do NOT memset P here - Present_SetGbm/Present_SetDrmFd already
	// filled in the gbm device / share ctx / fd handed over by the skeleton
	// (that wipe is why "no gbm device" ever happened). Only reset the
	// fields this function owns.
	P.inited = 0;
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
	int master_rc = p_drmSetMaster(P.drmfd);
	printf("[present] card0 fd=%d drmSetMaster rc=%d\n", P.drmfd, master_rc);
	if (master_rc != 0 && P.hasSdlfd && P.sdlfd >= 0) {
		// couldn't take master; prefer SDL's fd if it is a working one
		drmModeRes *probe = p_drmModeGetResources(P.sdlfd);
		if (probe) { p_drmModeFreeResources(probe); close(P.drmfd); P.drmfd = P.sdlfd;
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
	printf("[present] mode %ux%u conn=%u crtc=%u\n", P.modeW, P.modeH, gConnId, gCrtcId);

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
	printf("[present] share ctx ok (EGL %d.%d), exts %s\n", maj, min,
	       eglCreateImageKHR && glEGLImageTargetTexture2DOES ? "ok" : "MISSING");
}

// Import bo i as GL texture + fbo (must run with gCtx current).
static int
import_bo(int i)
{
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


static GLuint gReadFbo = 0;

static void *
present_thread(void *arg)
{
	(void)arg;
	p_eglMakeCurrent(gDpy, EGL_NO_SURFACE, EGL_NO_SURFACE, gCtx);

	// import all bos on this context; a failed bo is skipped (the pool just
	// runs one smaller) instead of killing the thread
	int okcnt = 0;
	for (int i = 0; i < PRESENT_BO_COUNT; i++)
		if (import_bo(i) == 0) okcnt++; else P.bos[i] = NULL; // mark unusable
	if (okcnt == 0) { printf("[present] fatal: no importable bo\n"); return NULL; }
	printf("[present] thread: %d/%d bos imported\n", okcnt, PRESENT_BO_COUNT);

	glViewport(0, 0, P.modeW, P.modeH);
	glDisable(GL_DEPTH_TEST);
	glDisable(GL_BLEND);
	glDisable(GL_SCISSOR_TEST);

	// this thread's own read fbo (FBO state is per-context in GLES)
	glGenFramebuffers(1, &gReadFbo);

	while (1) {
		uint32_t srcFbo = 0, srcTex = 0;
		pthread_mutex_lock(&P.mtx);
		while (!P.quit && P.pendingCount == 0)
			pthread_cond_wait(&P.cv, &P.mtx);
		if (P.quit && P.pendingCount == 0) { pthread_mutex_unlock(&P.mtx); break; }
		srcFbo = P.pending[0].fbo;
		srcTex = P.pending[0].tex;
		memmove(&P.pending[0], &P.pending[1], sizeof(P.pending[0]) * 3);
		P.pendingCount--;
		pthread_mutex_unlock(&P.mtx);

		// find a free bo
		int bo = -1;
		for (int i = 0; i < PRESENT_BO_COUNT; i++)
			if (P.bos[i] && !P.boBusy[i]) { bo = i; break; }
		if (bo < 0) {
			// shouldn't happen with the FIFO free above, but be safe
			usleep(4000);
			continue;
		}

		// blit completed frame -> bo. IMPORTANT: FBO attachment state is
		// PER-CONTEXT in GLES (only container objects like textures are
		// shared) - the render thread's fbo id is meaningless here. Attach
		// the shared SOURCE TEXTURE to this thread's own read fbo instead.
		glBindFramebuffer(GL_READ_FRAMEBUFFER, gReadFbo);
		glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
		                       GL_TEXTURE_2D, srcTex, 0);
		glBindFramebuffer(GL_DRAW_FRAMEBUFFER, P.boFbo[bo]);
		// Y-flip: the panel scans out top-down while GL texture origin is
		// bottom-up - swap the source rect to land upright (the R36S panel
		// also rotates, matching what SDL's GL swap used to do)
		glBlitFramebuffer(0, P.height, P.width, 0,
		                  0, 0, P.modeW, P.modeH,
		                  GL_COLOR_BUFFER_BIT, GL_NEAREST);

		// Fence: the page flip must not scan out the bo before the GPU has
		// finished blitting into it. glFlush submits the blit; the fence
		// inserts a dependency so KMS waits (drmModePageFlip with a
		// in-fence would be the precise mechanism; the blob exposes
		// EGL_KHR_fence_sync but not per-fb explicit in-fences via this
		// path, so we wait server-side - cheap on the present thread,
		// which exists precisely to absorb this wait).
		glFlush();
		if (eglCreateSyncKHR && eglClientWaitSyncKHR) {
			EGLSyncKHR sync = eglCreateSyncKHR(gDpy, EGL_SYNC_FENCE_KHR, NULL);
			if (sync != EGL_NO_SYNC_KHR) {
				eglClientWaitSyncKHR(gDpy, sync, EGL_SYNC_FLUSH_COMMANDS_BIT_KHR, EGL_FOREVER_KHR);
				eglDestroySyncKHR(gDpy, sync);
			}
		}

		// queue the flip. NOTEs from the device bring-up:
		//  - the 4.4 rockchip drm driver rejects DRM_MODE_PAGE_FLIP_ASYNC
		//    with EINVAL (probe-verified t6): vblank-aligned only.
		//  - NO EVENT flag: SDL KMSDRM polls this same fd and its
		//    drmHandleEvent steals our flip events (the bo never came back
		//    and the thread wedged in poll). Without events we simply
		//    assume the bo is reusable after one vblank period, which the
		//    2-bo rotation guarantees (the other bo is being scanned out
		//    while we blit into this one).
		int ret = p_drmModePageFlip(P.drmfd, gCrtcId, P.fbs[bo], 0, NULL);
		if (ret) {
			printf("[present] flip: %s\n", strerror(errno));
			fflush(stdout);
			P.boBusy[bo] = 0;
			continue;
		}
		P.boBusy[bo] = 1;

		// Free the bo that was flipped LAST round (FIFO): with no events
		// (SDL steals them) we conservatively hold each bo for one extra
		// vblank after its flip, which the 2-bo rotation can absorb.
		for (int i = 0; i < PRESENT_BO_COUNT; i++) {
			if (P.boBusy[i] && i != bo) { P.boBusy[i] = 0; break; }
		}
		// Pace to the panel: ~16.7ms at 60Hz. The present thread absorbs
		// this wait; the render thread never blocks.
		usleep(16000);
	}
	return NULL;
}


void
Present_Start(void)
{
	if (!P.inited) return;
	pthread_create(&P.thread, NULL, present_thread, NULL);
}

void
Present_Submit(uint32_t fbo, uint32_t tex)
{
	if (!P.inited) return;
	pthread_mutex_lock(&P.mtx);
	if (P.pendingCount < 4) {
		P.pending[P.pendingCount].fbo = fbo;
		P.pending[P.pendingCount].tex = tex;
		P.pendingCount++;
	}
	pthread_cond_signal(&P.cv);
	pthread_mutex_unlock(&P.mtx);
}

void
Present_Poll(void) {}

void
Present_Shutdown(void)
{
	if (!P.inited) return;
	pthread_mutex_lock(&P.mtx);
	P.quit = 1;
	pthread_cond_signal(&P.cv);
	pthread_mutex_unlock(&P.mtx);
	pthread_join(P.thread, NULL);
	for (int i = 0; i < PRESENT_BO_COUNT; i++) {
		if (P.boFbo[i]) glDeleteFramebuffers(1, &P.boFbo[i]);
		if (P.boTex[i]) glDeleteTextures(1, &P.boTex[i]);
		if (P.boEglImage[i]) eglDestroyImageKHR(gDpy, (EGLImageKHR)(uintptr_t)P.boEglImage[i]);
		if (P.fbs[i]) p_drmModeRmFB(P.drmfd, P.fbs[i]);
		if (P.bos[i]) p_gbm_bo_destroy(P.bos[i]);
	}
	if (gCrtc) p_drmModeFreeCrtc(gCrtc);
	if (gConn) p_drmModeFreeConnector(gConn);
	if (P.drmfd >= 0) close(P.drmfd);
	P.inited = 0;
}
#endif // PRESENT_USE_KMS
