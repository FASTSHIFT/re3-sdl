#!/usr/bin/env bash
# Build reVC for R36S (aarch64) inside the PortMaster builder image via qemu.
# Usage: tools/r36s/build.sh [Release|RelWithDebInfo] [--minimal]
#   (default)    : the shipping/optimal config - all R36S optimizations ON
#                 (R36S tweaks, lean FX, perf HUD, Chinese, triple-buffer
#                  present chain docs/09, gamepad cheats docs/10)
#   --minimal    : stock nosro behaviour - R36S tweaks/lean FX/CN/triplebuf/
#                 cheats OFF, only the perf HUD stays (A/B comparison baseline)
# The old --baseline/--triplebuf/--cheats flags are accepted as no-ops for
# muscle-memory compatibility (their settings are the default now).
# --as-needed: GL is loaded at runtime via SDL_GL_GetProcAddress (glad), so the
# libOpenGL.so.0 (GLVND) link dep is unused and absent on dArkOS/libMali.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TYPE="${1:-Release}"
# The optimal config: everything the port has tuned for this hardware.
EXTRA_CMAKE="-DREVC_R36S=ON -DREVC_LEAN_FX=ON -DREVC_PERF_HUD=ON -DREVC_CHINESE=ON -DREVC_TRIPLEBUF=ON -DREVC_CHEATS=ON"
if [[ "${2:-}" == "--minimal" || "${2:-}" == "--baseline" ]]; then
  # Bare-bones: only the perf HUD (docs/08/09 measurement harness)
  EXTRA_CMAKE="-DREVC_R36S=OFF -DREVC_LEAN_FX=OFF -DREVC_PERF_HUD=ON -DREVC_CHINESE=OFF -DREVC_TRIPLEBUF=OFF -DREVC_CHEATS=OFF"
fi
# --triplebuf / --cheats: accepted for compatibility; both are ON by default now.
if [[ "${2:-}" == "--triplebuf" ]]; then
  EXTRA_CMAKE="$EXTRA_CMAKE -DREVC_TRIPLEBUF=ON"
fi
if [[ "${2:-}" == "--cheats" ]]; then
  EXTRA_CMAKE="$EXTRA_CMAKE -DREVC_CHEATS=ON"
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
