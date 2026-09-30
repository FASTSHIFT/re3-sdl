// gl3device_present.h - triple-buffer present chain interface (docs/09)
#ifndef GL3DEVICE_PRESENT_H
#define GL3DEVICE_PRESENT_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Device (KMS) path only: wire the shared gbm device + EGL display + the
// main render context BEFORE Present_Init so bo imports land on the same
// driver instance. Must be called with the EGL display initialized.
void Present_SetGbm(void *gbm, void *eglDisplay, void *shareCtx);
// Device (KMS) path only: hand over SDL's drm fd (already DRM master; needed
// for drmModePageFlip). Call before Present_Init.
void Present_SetDrmFd(int fd);

int  Present_Init(int w, int h);          // create bo pool / resources
void Present_Start(void);                 // spawn the present thread
void Present_Submit(uint32_t fbo, uint32_t tex); // main thread, per rendered frame
void Present_Poll(void);                  // optional diagnostics
void Present_Shutdown(void);

#ifdef __cplusplus
}
#endif
#endif
