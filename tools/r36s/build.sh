#!/usr/bin/env bash
# Build reVC for R36S (aarch64) inside the PortMaster builder image via qemu.
# Usage: tools/r36s/build.sh [Release|RelWithDebInfo] [--baseline]
#   --baseline : only the perf HUD, everything else stock nosro behaviour
#                (REVC_R36S=OFF, REVC_LEAN_FX=OFF, REVC_PERF_HUD=ON)
# --as-needed: GL is loaded at runtime via SDL_GL_GetProcAddress (glad), so the
# libOpenGL.so.0 (GLVND) link dep is unused and absent on dArkOS/libMali.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TYPE="${1:-Release}"
EXTRA_CMAKE=""
if [[ "${2:-}" == "--baseline" ]]; then
  EXTRA_CMAKE="-DREVC_R36S=OFF -DREVC_LEAN_FX=OFF -DREVC_PERF_HUD=ON"
fi
IMAGE="${REVC_BUILDER_IMAGE:-revc-r36s-builder:focal}"
mkdir -p "$REPO/build-r36s" "$HOME/.cache/revc-ccache"

docker run --rm --platform linux/arm64 \
  --user "$(id -u):$(id -g)" \
  -e CCACHE_DIR=/ccache -e HOME=/tmp \
  -v "$REPO":/src -v "$HOME/.cache/revc-ccache":/ccache \
  -w /src/build-r36s \
  "$IMAGE" bash -c "
    set -e
    cmake .. -G Ninja \
      -DCMAKE_BUILD_TYPE=$TYPE \
      -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
      -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
      -DLIBRW_PLATFORM=GL3 -DLIBRW_GL3_GFXLIB=SDL2 -DLIBRW_FORCE_GLES=ON \
      -DREVC_SNES_PAD=ON \
      $EXTRA_CMAKE \
      -DCMAKE_EXE_LINKER_FLAGS=-Wl,--as-needed \
      -DLIBRW_TOOLS=OFF -DLIBRW_INSTALL=OFF
    ninja -j\$(nproc)
  "
