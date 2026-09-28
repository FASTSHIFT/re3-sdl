/*
 * astc_consistency_test.cpp - librw-level pipeline unit test.
 *
 * For a D3D8-platform TXD (original game asset) and its GL3-native ASTC
 * conversion (produced by tools/r36s/txd_compress.py), compare what the GPU
 * actually stores after the game's own load paths:
 *
 *   A) original DXT: Texture::streamReadNative -> convertTexToCurrentPlatform
 *      -> d3d_to_gl3 + flipDXT  (what the game renders with vanilla assets)
 *   B) converted ASTC: Texture::streamReadNative -> readNativeTexture
 *      (what our converted TXDs provide)
 *
 * Both rasters are read back from GL (glGetTexImage) and compared per pixel.
 * The test FAILS if the ASTC pipeline is flipped/rotated/otherwise wrong.
 *
 * Build (from repo root, against build-local):
 *   g++ -std=c++17 -o /tmp/astctest tools/r36s/astc_consistency_test.cpp \
 *       -Ivendor/librw/src -Ibuild-local/vendor/librw/src \
 *       -Isrc -DBUILD_DIR_PATHS ... (see run below)
 * Link: librw.a + SDL2 + GL.
 */
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <cmath>
#include <vector>
#include <string>

#include <SDL2/SDL.h>

#define RW_GL3 1
#include <rw.h>
#include <glad/glad.h>

using namespace rw;

static const char *origPath;   // D3D8 TXD
static const char *astcPath;   // GL3-native ASTC TXD

static bool
attachPlugins(void)
{
    registerMeshPlugin();
    registerNativeDataPlugin();
    registerAtomicRightsPlugin();
    registerMaterialRightsPlugin();
    registerSkinPlugin();
    registerHAnimPlugin();
    registerMatFXPlugin();
    registerUVAnimPlugin();
    return true;
}

struct CmpResult {
    int w, h;
    double psnr;
    double maxdiff;
    long nmismatch;   // pixels differing by > 16
    double psnrRGB, psnrA;
    long aBad;
};

// Read back a GL3 texture raster's level-0 content as RGBA by rendering a
// fullscreen quad that samples the texture into an RGBA8 FBO. Compressed
// formats (DXT/ASTC) are not color-renderable, so they cannot be attached
// directly - sampling is the only portable way.
static const char *vsrc =
    "#version 330\n"
    "const vec2 pos[4] = vec2[4](vec2(-1,-1), vec2(1,-1), vec2(-1,1), vec2(1,1));\n"
    "const vec2 uv[4] = vec2[4](vec2(0,0), vec2(1,0), vec2(0,1), vec2(1,1));\n"
    "out vec2 vUV;\n"
    "void main(){ vUV = uv[gl_VertexID]; gl_Position = vec4(pos[gl_VertexID], 0, 1); }\n";
static const char *fsrc =
    "#version 330\n"
    "in vec2 vUV; out vec4 frag; uniform sampler2D tex;\n"
    "void main(){ frag = texture(tex, vUV); }\n";

static std::vector<uint8>
readbackRaster(Raster *raster)
{
    gl3::Gl3Raster *natras = (gl3::Gl3Raster*)((char*)raster + rw::gl3::nativeRasterOffset);
    int w = raster->width, h = raster->height;
    std::vector<uint8> px(w*h*4);

    static GLuint prog = 0, fbo = 0, rt = 0, rtW = 0, rtH = 0;
    if(!prog){
        GLuint vs = glCreateShader(GL_VERTEX_SHADER);
        glShaderSource(vs, 1, &vsrc, nil); glCompileShader(vs);
        GLuint fs = glCreateShader(GL_FRAGMENT_SHADER);
        glShaderSource(fs, 1, &fsrc, nil); glCompileShader(fs);
        prog = glCreateProgram();
        glAttachShader(prog, vs); glAttachShader(prog, fs); glLinkProgram(prog);
        GLint ok = 0; glGetProgramiv(prog, GL_LINK_STATUS, &ok);
        if(!ok){
            char log[512]; glGetProgramInfoLog(prog, 512, nil, log);
            fprintf(stderr, "shader link failed: %s\n", log);
            return px;
        }
        glGenFramebuffers(1, &fbo);
        glGenTextures(1, &rt);
    }
    // (re)create render target at the needed size
    if(rtW != w || rtH != h){
        glBindTexture(GL_TEXTURE_2D, rt);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nil);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
        rtW = w; rtH = h;
    }
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, rt, 0);
    if(glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE){
        fprintf(stderr, "fbo incomplete\n");
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        return px;
    }
    glViewport(0, 0, w, h);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, natras->texid);
    // mip-incomplete textures default to a mipmapped min filter
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glUseProgram(prog);
    glUniform1i(glGetUniformLocation(prog, "tex"), 0);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    // sanity: detect all-zero readbacks (failure)
    uint32 sum = 0;
    for(size_t i = 0; i < px.size(); i++) sum += px[i];
    if(sum == 0)
        fprintf(stderr, "  readback all-zero for texid %u\n", natras->texid);
    return px;
}

static CmpResult
compareRasters(Raster *a, Raster *b, const char *nameA, const char *nameB)
{
    CmpResult r = {0, 0, -1, 999, -1};
    if(a->width != b->width || a->height != b->height){
        printf("  DIM MISMATCH: %s %dx%d vs %s %dx%d\n",
               nameA, a->width, a->height, nameB, b->width, b->height);
        return r;
    }
    int w = a->width, h = a->height;
    std::vector<uint8> pa = readbackRaster(a);
    std::vector<uint8> pb = readbackRaster(b);

    double sse = 0; r.maxdiff = 0; r.nmismatch = 0;
    double sseRGB = 0, sseA = 0; long aBad = 0;
    for(int i = 0; i < w*h*4; i++){
        int d = (int)pa[i] - (int)pb[i];
        sse += (double)d*d;
        if((i & 3) != 3) sseRGB += (double)d*d; else sseA += (double)d*d;
        if((i & 3) == 3 && d > 40) aBad++;
        int ad = d < 0 ? -d : d;
        if(ad > r.maxdiff) r.maxdiff = ad;
        if(ad > 16 && (i & 3) != 3) r.nmismatch++;   // ignore alpha channel
    }
    r.psnrRGB = 10*log10(255.0*255.0/(sseRGB/(w*h*3)+1e-9));
    r.psnrA = 10*log10(255.0*255.0/(sseA/(w*h)+1e-9));
    r.aBad = aBad;
    r.w = w; r.h = h;
    double mse = sse/(w*h*4);
    r.psnr = mse == 0 ? 99.0 : 10*log10(255.0*255.0/mse);
    return r;
}

int
main(int argc, char **argv)
{
    if(argc != 3){
        fprintf(stderr, "usage: %s original-D3D8.txd converted-ASTC.txd\n", argv[0]);
        return 2;
    }
    origPath = argv[1];
    astcPath = argv[2];

    // --- bootstrap librw like the game does ---
    // (the GL3 device creates its own hidden SDL2 window+GL context)
    Engine::init();
    attachPlugins();
    EngineOpenParams openParams;
    SDL_Window *dummyWin = nil;
    openParams.window = &dummyWin;
    openParams.fullscreen = 0;
    openParams.width = 64;
    openParams.height = 64;
    openParams.windowtitle = "astctest";
    if(!Engine::open(&openParams)){
        fprintf(stderr, "Engine::open failed\n");
        return 1;
    }
    if(!Engine::start()){
        fprintf(stderr, "Engine::start failed\n");
        return 1;
    }
    printf("librw up, platform GL3, gles=%d, astc=%d dxt=%d\n",
           gl3::gl3Caps.gles, gl3::gl3Caps.astcSupported, gl3::gl3Caps.dxtSupported);

    // --- load both TXDs through the game's native read path ---
    auto loadDict = [](const char *path, bool convert) -> std::vector<Texture*> {
        std::vector<Texture*> out;
        StreamFile in;
        if(!in.open(path, "rb")){
            fprintf(stderr, "cannot open %s\n", path);
            return out;
        }
        if(!findChunk(&in, ID_TEXDICTIONARY, nil, nil)){
            fprintf(stderr, "%s: no TEXDICTIONARY\n", path);
            in.close();
            return out;
        }
        if(!findChunk(&in, ID_STRUCT, nil, nil)){
            fprintf(stderr, "%s: no STRUCT\n", path);
            in.close();
            return out;
        }
        int32 numTex = in.readI16();
        in.readI16(); // deviceId
        for(int32 i = 0; i < numTex; i++){
            if(!findChunk(&in, ID_TEXTURENATIVE, nil, nil)){
                fprintf(stderr, "%s: missing TEXTURENATIVE %d/%d\n", path, i, numTex);
                break;
            }
            Texture *tex = Texture::streamReadNative(&in);
            if(tex && tex->raster && convert)
                tex->raster = Raster::convertTexToCurrentPlatform(tex->raster);
            if(tex) out.push_back(tex);
            else fprintf(stderr, "%s: texture %d read failed\n", path, i);
        }
        in.close();
        return out;
    };

    std::vector<Texture*> orig = loadDict(origPath, true);   // game path
    std::vector<Texture*> astc = loadDict(astcPath, false);  // our path
    if(orig.empty() || astc.empty()){
        fprintf(stderr, "load failed: orig=%zu astc=%zu\n", orig.size(), astc.size());
        return 1;
    }

    // --- compare every texture pair by name ---
    int npass = 0, nfail = 0, nmiss = 0;
    for(Texture *t : orig){
        Texture *match = nil;
        for(Texture *u : astc)
            if(!strcmp(t->name, u->name)){ match = u; break; }
        if(!match){
            printf("  MISSING in ASTC dict: %s\n", t->name);
            nmiss++;
            continue;
        }
        CmpResult r = compareRasters(t->raster, match->raster, t->name, t->name);
        if(r.psnr > 0 && r.psnr < 45){
            std::vector<uint8> da = readbackRaster(t->raster);
            std::vector<uint8> db = readbackRaster(match->raster);
            fprintf(stderr, "  DUMP %s A[0..7]:", t->name);
            for(int i = 0; i < 32; i++) fprintf(stderr, " %02x", da[i]);
            fprintf(stderr, "\n  DUMP %s B[0..7]:", t->name);
            for(int i = 0; i < 32; i++) fprintf(stderr, " %02x", db[i]);
            fprintf(stderr, "\n");
        }
        if(r.psnr < 0){
            printf("  FAIL(dims) %-24s\n", t->name);
            nfail++;
        }else if(r.nmismatch > (long)(r.w*r.h/1000)){   // >0.1% bad pixels
            printf("  FAIL       %-24s %dx%d PSNR=%.1f RGB=%.1f A=%.1f maxΔ=%ld bad=%ld aBad=%ld\n",
                   t->name, r.w, r.h, r.psnr, r.psnrRGB, r.psnrA, (long)r.maxdiff, r.nmismatch, r.aBad);
            nfail++;
        }else{
            printf("  ok         %-24s %dx%d PSNR=%.1f maxΔ=%ld bad=%ld\n",
                   t->name, r.w, r.h, r.psnr, (long)r.maxdiff, r.nmismatch);
            npass++;
        }
    }
    printf("== %s vs %s: %d pass, %d fail, %d missing\n",
           origPath, astcPath, npass, nfail, nmiss);

    Engine::stop();
    Engine::close();
    Engine::term();
    return nfail || nmiss ? 1 : 0;
}
