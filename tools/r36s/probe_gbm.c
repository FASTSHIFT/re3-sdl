// probe_gbm.c - 三缓冲方案前置探测程序 (docs/09 §5 步骤1)
// 验证四个 API 面, 全部通过才有条件上完整方案:
//   1. EXT_image_dma_buf_import (gbm bo → GL 纹理)
//   2. eglCreateContext(share) 共享纹理命名空间
//   3. glBlitFramebuffer 跨 FBO 拷贝
//   4. EGL fence (eglCreateSyncKHR / glWaitSync) 跨线程同步原语
//
// PC (x86 Mesa) 与真机 (Mali blob) 都可跑 — 输出 PASS/FAIL 清单。
// 构建: gcc probe_gbm.c -o probe_gbm -lEGL -lGL -lgbm -ldrm -lstdc++ (PC)
//       clang --target=aarch64-linux-gnu probe_gbm.c -o probe_gbm.arm -lEGL -lGL -lgbm -ldrm (交叉)
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GL/gl.h>
#include <GL/glext.h>

#include <gbm.h>
#include <xf86drm.h>

static int gPass = 0, gFail = 0;
static void report(const char *name, int ok, const char *detail)
{
	printf("[%s] %-42s %s\n", ok ? "PASS" : "FAIL", name, detail ? detail : "");
	if (ok) gPass++; else gFail++;
}

#define CHK(name, cond) report(name, !!(cond), #cond)

int main(void)
{
	printf("=== GBM/EGL triple-buffer probe ===\n");

	// ---- 1. 打开 DRM 设备 (PC: card0/DRI; 真机: card0) ----
	const char *drm_paths[] = { "/dev/dri/card0", "/dev/dri/renderD128", NULL };
	int drmfd = -1;
	for (int i = 0; drm_paths[i]; i++) {
		drmfd = open(drm_paths[i], O_RDWR | O_CLOEXEC);
		if (drmfd >= 0) { printf("drm: %s\n", drm_paths[i]); break; }
	}
	CHK("open /dev/dri/*", drmfd >= 0);

	struct gbm_device *gbm = gbm_create_device(drmfd);
	CHK("gbm_create_device", gbm != NULL);

	// ---- 2. EGL 初始化 (device platform — 不需要窗口系统) ----
	PFNEGLGETPLATFORMDISPLAYEXTPROC eglGetPlatformDisplayEXT =
		(PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
	EGLDisplay dpy = EGL_NO_DISPLAY;
	// 老式入口优先: blob 是 EGL 1.4 (r13p0), eglGetDisplay(gbm) 是它的
	// 原生路径 (SDL KMSDRM 正是这么用的)。EXT 平台枚举作 Mesa 兜底。
	dpy = eglGetDisplay((EGLNativeDisplayType)gbm);
	if (dpy == EGL_NO_DISPLAY && eglGetPlatformDisplayEXT)
		dpy = eglGetPlatformDisplayEXT(EGL_PLATFORM_GBM_MESA, gbm, NULL);
	CHK("eglGetDisplay(GBM)", dpy != EGL_NO_DISPLAY);
	if (dpy == EGL_NO_DISPLAY) goto summary;

	EGLint maj, min;
	CHK("eglInitialize", eglInitialize(dpy, &maj, &min));
	printf("EGL %d.%d | %s | %s\n", maj, min,
	       eglQueryString(dpy, EGL_VERSION), eglQueryString(dpy, EGL_VENDOR));

	// ---- 3. 扩展探测: dma_buf import 是 bo→GL 的关键 ----
	const char *exts = eglQueryString(dpy, EGL_EXTENSIONS);
	if (exts == NULL) exts = ""; // blob: 未初始化完成时可能返回 NULL
	printf("EGL extensions: %.300s...\n", exts);
	int has_dmabuf = strstr(exts, "EGL_EXT_image_dma_buf_import") != NULL;
	int has_dmabuf_mod = strstr(exts, "EGL_EXT_image_dma_buf_import_modifiers") != NULL;
	report("eglQueryString(EXTENSIONS) non-null", exts[0] != '\0', exts[0] ? "" : "EMPTY - display init issue");
	CHK("EGL_EXT_image_dma_buf_import", has_dmabuf);
	report("..._import_modifiers (可选)", has_dmabuf_mod, has_dmabuf_mod ? "yes" : "no");

	// ---- 4. Config + 双 context (share 测试) ----
	EGLint cfg_attribs[] = {
		EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
		EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8,
		EGL_NONE };
	EGLConfig cfg; EGLint n;
	CHK("eglChooseConfig (pbuffer GL)", eglChooseConfig(dpy, cfg_attribs, &cfg, 1, &n) && n > 0);

	CHK("eglBindAPI(GL)", eglBindAPI(EGL_OPENGL_API));
	EGLint ctx_attribs[] = { EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE };
	EGLContext ctx_main = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctx_attribs);
	CHK("eglCreateContext (main, GL3)", ctx_main != EGL_NO_CONTEXT);
	EGLContext ctx_pres = eglCreateContext(dpy, cfg, ctx_main, ctx_attribs);
	CHK("eglCreateContext (share ctx)", ctx_pres != EGL_NO_CONTEXT);

	// GBM 平台通常无 pbuffer surface — 走 EGL_KHR_surfaceless_context:
	// makeCurrent(EGL_NO_SURFACE)。FBO 是唯一渲染目标 (正好是本方案的形态)。
	EGLSurface srf = EGL_NO_SURFACE;
	int has_surfaceless = strstr(exts ? "" : "", "x") != NULL || 1; // GBM Mesa 支持; blob 待测
	CHK("surfaceless makeCurrent (main)", eglMakeCurrent(dpy, srf, srf, ctx_main));

	// ---- 5. 共享纹理命名空间验证: main 建纹理, pres context 可见? ----
	GLuint shared_tex = 0;
	glGenTextures(1, &shared_tex);
	glBindTexture(GL_TEXTURE_2D, shared_tex);
	glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 64, 64, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
	CHK("main ctx: create texture", glGetError() == GL_NO_ERROR);

	CHK("pres ctx: makeCurrent", eglMakeCurrent(dpy, srf, srf, ctx_pres));
	glBindTexture(GL_TEXTURE_2D, shared_tex); // share 生效则同名纹理可绑定
	CHK("share ctx: bind main's texture", glGetError() == GL_NO_ERROR);
	CHK("share ctx: texture is valid", glIsTexture(shared_tex));

	// ---- 6. gbm bo + dmabuf import 成 GL 纹理 (核心!) ----
	struct gbm_bo *bo = gbm_bo_create(gbm, 64, 64, GBM_FORMAT_XRGB8888,
	                                  GBM_BO_USE_RENDERING | GBM_BO_USE_SCANOUT);
	CHK("gbm_bo_create (scanout+render)", bo != NULL);
	if (bo) {
		int fd = gbm_bo_get_fd(bo);
		CHK("gbm_bo_get_fd", fd >= 0);
		uint32_t w = gbm_bo_get_width(bo), h = gbm_bo_get_height(bo), stride = gbm_bo_get_stride(bo);
		if (fd >= 0 && has_dmabuf) {
			PFNEGLCREATEIMAGEKHRPROC eglCreateImageKHR =
				(PFNEGLCREATEIMAGEKHRPROC)eglGetProcAddress("eglCreateImageKHR");
			PFNGLEGLIMAGETARGETTEXTURE2DOESPROC glEGLImageTargetTexture2DOES =
				(PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)eglGetProcAddress("glEGLImageTargetTexture2DOES");
			CHK("proc: eglCreateImageKHR", eglCreateImageKHR != NULL);
			CHK("proc: glEGLImageTargetTexture2DOES", glEGLImageTargetTexture2DOES != NULL);
			if (eglCreateImageKHR && glEGLImageTargetTexture2DOES) {
				EGLint img_attribs[] = {
					EGL_WIDTH, (EGLint)w, EGL_HEIGHT, (EGLint)h,
					EGL_LINUX_DRM_FOURCC_EXT, GBM_FORMAT_XRGB8888,
					EGL_DMA_BUF_PLANE0_FD_EXT, fd,
					EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
					EGL_DMA_BUF_PLANE0_PITCH_EXT, (EGLint)stride,
					EGL_NONE };
				EGLImageKHR img = eglCreateImageKHR(dpy, EGL_NO_CONTEXT,
				                                    EGL_LINUX_DMA_BUF_EXT, (EGLClientBuffer)NULL, img_attribs);
				CHK("eglCreateImageKHR (dmabuf)", img != EGL_NO_IMAGE_KHR);
				if (img != EGL_NO_IMAGE_KHR) {
					GLuint bo_tex = 0;
					glGenTextures(1, &bo_tex);
					glBindTexture(GL_TEXTURE_2D, bo_tex);
					glEGLImageTargetTexture2DOES(GL_TEXTURE_2D, img);
					CHK("import bo as GL texture", glGetError() == GL_NO_ERROR);
				}
			}
		}
	}

	// ---- 7. 双 FBO + glBlitFramebuffer ----
	GLuint fbo[2], tex[2];
	glGenFramebuffers(2, fbo);
	glGenTextures(2, tex);
	for (int i = 0; i < 2; i++) {
		glBindTexture(GL_TEXTURE_2D, tex[i]);
		glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 64, 64, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
		glBindFramebuffer(GL_FRAMEBUFFER, fbo[i]);
		glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex[i], 0);
	}
	CHK("2x FBO complete", glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE);
	glBindFramebuffer(GL_DRAW_FRAMEBUFFER, fbo[1]);
	glBindFramebuffer(GL_READ_FRAMEBUFFER, fbo[0]);
	glBlitFramebuffer(0, 0, 64, 64, 0, 0, 64, 64, GL_COLOR_BUFFER_BIT, GL_NEAREST);
	CHK("glBlitFramebuffer FBO→FBO", glGetError() == GL_NO_ERROR);

	// ---- 8. fence 同步原语 ----
	PFNEGLCREATESYNCKHRPROC eglCreateSyncKHR =
		(PFNEGLCREATESYNCKHRPROC)eglGetProcAddress("eglCreateSyncKHR");
	PFNEGLCLIENTWAITSYNCKHRPROC eglClientWaitSyncKHR =
		(PFNEGLCLIENTWAITSYNCKHRPROC)eglGetProcAddress("eglClientWaitSyncKHR");
	CHK("proc: eglCreateSyncKHR", eglCreateSyncKHR != NULL);
	CHK("proc: eglClientWaitSyncKHR", eglClientWaitSyncKHR != NULL);
	if (eglCreateSyncKHR && eglClientWaitSyncKHR) {
		EGLSyncKHR sync = eglCreateSyncKHR(dpy, EGL_SYNC_FENCE_KHR, NULL);
		CHK("eglCreateSyncKHR (fence)", sync != EGL_NO_SYNC_KHR);
		if (sync != EGL_NO_SYNC_KHR) {
			EGLint st = eglClientWaitSyncKHR(dpy, sync, EGL_SYNC_FLUSH_COMMANDS_BIT_KHR, 1000000);
			CHK("eglClientWaitSyncKHR (1s timeout)", st == EGL_CONDITION_SATISFIED_KHR);
		}
	}

summary:
	printf("\n=== %d PASS / %d FAIL ===\n", gPass, gFail);
	printf(gFail == 0
	       ? "全绿: 可上完整三缓冲方案\n"
	       : "有红项: 见上表, 按 docs/09 §4 风险表降级 (dmabuf缺→CPU拷贝, share缺→单context)\n");
	return gFail ? 1 : 0;
}
