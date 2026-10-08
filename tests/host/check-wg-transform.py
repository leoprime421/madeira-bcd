#!/usr/bin/env python3
"""winegstreamer's H.264 and AAC wg_transform (build/ntdll-unix/wg_transform_av_ios.c); no Wine runs.

1. Source checks on build/ntdll-unix/winegstreamer_unixlib_ios.c: the core is included, both
   create entries (64-bit and wow64) go through transform_create_any() with the right bitness,
   every other transform entry dispatches the new objects before the WMA ones, the sample flags
   are asserted equal, and 32-bit callers are off by default.
2. The core on its own, under ASan/UBSan, with stub decoder backends of the
   wg_parser_backend_ios.h shape (VideoToolbox / AudioToolbox do not exist on Linux):
   - the SPS parser against SPS units written bit by bit here (cropping, interlace, VUI timing,
     aspect ratio, video signal type, HRD, bitstream restriction, scaling lists, POC types);
   - H.264 through the transform protocol as video_decoder.c drives it: the stub emits one
     picture per access unit in DECODE order, with a pattern derived from its timestamp, and the
     transform must hand them back in presentation order, after one stream change, in the MF
     layout of NV12 / I420 / YV12 / YUY2 (checked byte by byte against formulas written here
     independently of the core, including the 16-line padding and 2D strides), with the
     timestamp flags; the input queue limit, buffer too small, flush (waits for the next IDR,
     marks a discontinuity), drain, decode errors, a resolution change mid-stream (decoder
     reopened, second stream change), SPS/PPS/AUD stripped from the packets the decoder gets;
   - when the system has ffmpeg with libx264: REAL streams (High profile with B-frames and a
     cropped height, Baseline with an odd width), converted to Annex B with parameter sets in
     front of every keyframe, the way Unreal's Electra feeds the Windows decoder;
   - AAC: raw access units with an AudioSpecificConfig, ADTS (several frames per sample),
     16-bit and float output, 5.1 to stereo, 24 kHz mono to 48 kHz stereo, decode errors
     (concealed with silence), flush, the AAC decoder's own support-check configuration.
3. When a configured Wine tree is available (MADEIRA_WINE_BUILD, as tests/host/check-wma-decoder.py):
   the production winegstreamer_unixlib_ios.c with the same stubs, through the real unix call
   entries: create from MFVIDEOFORMAT / HEAACWAVEFORMAT media types, the two-phase
   get_output_type, set_output_type, push / read with struct wg_sample, get_status, drain,
   flush, destroy; the 32-bit create refused by default and allowed with MADEIRA_WG_H264_AAC=1;
   MADEIRA_WG_H264_AAC=0 refusing 64-bit callers.

Environment: MADEIRA_FFMPEG_TARBALL / MADEIRA_HOST_FFMPEG as the other media tests,
MADEIRA_WINE_BUILD (part 3; skipped without it).
"""
from pathlib import Path
import hashlib, json, os, re, shutil, subprocess, sys, tempfile

root = Path(__file__).resolve().parents[2]
ntdll_unix = root / "build/ntdll-unix"
core = (ntdll_unix / "wg_transform_av_ios.c").read_text()
glue = (ntdll_unix / "winegstreamer_unixlib_ios.c").read_text()
wine = Path(os.environ.get("MADEIRA_WINE_SRC") or root / "wine")

# ------------------------------------------------------------------ 1. source checks
assert '#include "wg_transform_av_ios.c"' in glue, "the core is not included"
assert glue.index('#include "wg_transform_av_ios.c"') < glue.index('#include "windef.h"'), \
    "the core must come before the Wine headers (it includes none)"
entry = glue[glue.index("static NTSTATUS wma_transform_create( void *args )"):]
entry = entry[:entry.index("}") + 1]
assert "transform_create_any( args, FALSE )" in entry, entry
wow = glue[glue.index("static NTSTATUS wow64_wma_transform_create( void *args )"):]
wow = wow[:wow.index("\n}\n")]
assert "transform_create_any( &params, TRUE )" in wow, wow
for fn in ["static NTSTATUS transform_destroy(", "static NTSTATUS transform_push_data(",
           "static NTSTATUS transform_read_data(", "static NTSTATUS transform_get_output_type(",
           "static NTSTATUS transform_set_output_type(", "static NTSTATUS wma_transform_get_status(",
           "static NTSTATUS wma_transform_drain(", "static NTSTATUS wma_transform_flush("]:
    body = glue[glue.index(fn):]
    body = body[:body.index("\n}\n")]
    assert body.index("get_av_transform(") < body.index("get_transform("), ("H.264/AAC must be dispatched first", fn)
for flag in ["INCOMPLETE", "HAS_PTS", "HAS_DURATION", "SYNC_POINT", "DISCONTINUITY", "PRESERVE_TIMESTAMPS"]:
    assert "C_ASSERT( MTX_FLAG_%s == WG_SAMPLE_FLAG_%s );" % (flag, flag) in glue, flag
allowed = glue[glue.index("static BOOL av_allowed( BOOL wow )"):]
allowed = allowed[:allowed.index("\n}\n")]
assert 'getenv( "MADEIRA_WG_H264_AAC" )' in allowed and "return !wow;" in allowed, allowed
includes = re.findall(r'^#include\s+(\S+)', core, re.M)
assert all(i.startswith("<") or i == '"wg_parser_backend_ios.h"' for i in includes), ("the core includes no Wine header", includes)
media_build = (root / "build/wine-pe/build-media.sh").read_text()
assert "--enable-winegstreamer" in media_build
assert 'DLLS="winegstreamer msmpeg2vdec msauddecmft"' in media_build
assert 'dlls/$d/arm64ec-windows/$d.dll' in media_build
assert 'bash build/wine-pe/build-media.sh' in (root / ".github/workflows/build-ipa.yml").read_text()
print("PASS: source checks (dispatch, wow64 bitness, flag values, 32-bit default off)")

# ------------------------------------------------------------------ host FFmpeg (avutil + swresample are enough
# for the core; the glue harness needs the decoder set check-wma-decoder.py builds, so use exactly its flags and
# share its cache)
tarball = os.environ.get("MADEIRA_FFMPEG_TARBALL") or str(root / "build/ffmpeg/src/ffmpeg-7.1.1.tar.xz")
FLAGS = [
    "--disable-everything", "--disable-autodetect", "--disable-gpl", "--disable-nonfree", "--disable-version3",
    "--enable-decoder=wmav1,wmav2,wmapro,wmalossless,xma1,xma2",
    "--enable-decoder=mp1,mp2,mp3", "--enable-decoder=pcm_u8,pcm_s16le,pcm_s24le,pcm_s32le,pcm_f32le,pcm_f64le",
    "--enable-demuxer=mp3,wav,mov", "--enable-parser=mpegaudio",
    "--enable-encoder=wmav1,wmav2",
    "--disable-programs", "--disable-doc", "--disable-network", "--disable-avdevice", "--disable-swscale",
    "--disable-avfilter", "--disable-postproc", "--enable-avformat", "--enable-avcodec", "--enable-swresample",
    "--disable-asm", "--disable-shared", "--enable-static", "--enable-pic", "--disable-debug",
]
prefix = os.environ.get("MADEIRA_HOST_FFMPEG")
if not prefix:
    key = hashlib.sha256(" ".join(FLAGS).encode()).hexdigest()[:12]
    prefix = str(Path.home() / ".cache/madeira-host-ffmpeg" / ("7.1.1-" + key))
    if not (Path(prefix) / "lib/libavcodec.a").exists():
        assert Path(tarball).exists(), "no FFmpeg tarball at %s" % tarball
        src = Path(prefix + "-src")
        src.mkdir(parents=True, exist_ok=True)
        if not (src / "ffmpeg-7.1.1/configure").exists():
            subprocess.run(["tar", "--no-same-owner", "-xJf", tarball, "-C", str(src)], check=True)
        bld = Path(prefix + "-build"); bld.mkdir(parents=True, exist_ok=True)
        print("building host FFmpeg into", prefix, flush=True)
        log = open(str(bld) + ".log", "w")
        subprocess.run([str(src / "ffmpeg-7.1.1/configure"), "--prefix=" + prefix, "--cc=cc"] + FLAGS,
                       cwd=bld, check=True, stdout=log, stderr=subprocess.STDOUT)
        subprocess.run(["make", "-j%d" % (os.cpu_count() or 4)], cwd=bld, check=True, stdout=log, stderr=subprocess.STDOUT)
        subprocess.run(["make", "install"], cwd=bld, check=True, stdout=log, stderr=subprocess.STDOUT)
inc, lib = Path(prefix) / "include", Path(prefix) / "lib"
ff_libs = [str(lib / "libavformat.a"), str(lib / "libavcodec.a"), str(lib / "libswresample.a"),
           str(lib / "libavutil.a"), "-lm", "-lpthread"]

# ------------------------------------------------------------------ real H.264 streams (optional)
def nal_split(data):
    """Annex B to NAL payloads (start codes removed)."""
    out, i, start = [], 0, None
    n = len(data)
    while i + 3 <= n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            if start is not None:
                end = i
                while end > start and data[end - 1] == 0:
                    end -= 1
                out.append(data[start:end])
            i += 3
            start = i
            continue
        i += 1
    if start is not None:
        end = n
        while end > start and data[end - 1] == 0:
            end -= 1
        out.append(data[start:end])
    return out

def make_stream(tmp, name, size, profile, extra):
    """An Annex B stream split into access units, with each unit's presentation time (100 ns) in decode order."""
    mp4 = tmp / (name + ".mp4")
    es = tmp / (name + ".264")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=%s:rate=30" % size, "-t", "1.2",
                    "-c:v", "libx264", "-profile:v", profile, "-pix_fmt", "yuv420p"] + extra + [str(mp4)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-c:v", "copy", "-bsf:v", "h264_mp4toannexb",
                    "-f", "h264", str(es)], check=True)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "packet=pts_time,duration_time,flags", "-of", "json", str(mp4)],
                           check=True, capture_output=True, text=True)
    packets = json.loads(probe.stdout)["packets"]
    nals = nal_split(es.read_bytes())
    # 7.4.1.2.3: after a picture's slices, an AUD / SPS / PPS / SEI or the first slice of the
    # next picture starts the next access unit (the bitstream filter puts SPS/PPS before the AUD)
    units, cur, has_vcl = [], [], False
    for nal in nals:
        t = nal[0] & 0x1f
        starts = t in (6, 7, 8, 9) or (t in (1, 5) and len(nal) > 1 and nal[1] & 0x80)
        if has_vcl and starts:
            units.append(cur)
            cur, has_vcl = [], False
        cur.append(nal)
        if t in (1, 2, 3, 4, 5):
            has_vcl = True
    if cur:
        units.append(cur)
    assert len(units) == len(packets), (name, len(units), len(packets))
    blob = bytearray()
    for unit, pkt in zip(units, packets):
        data = b"".join(b"\x00\x00\x00\x01" + n for n in unit)
        pts = int(round(float(pkt["pts_time"]) * 10000000))
        dur = int(round(float(pkt.get("duration_time") or 0) * 10000000))
        key = 1 if "K" in pkt["flags"] else 0
        blob += len(data).to_bytes(4, "little") + pts.to_bytes(8, "little", signed=True) + \
                dur.to_bytes(8, "little") + key.to_bytes(4, "little") + data
    out = tmp / (name + ".units")
    out.write_bytes(bytes(blob))
    return out, len(units)

harness = r'''
#include "wg_transform_av_ios.c"
#include <math.h>

static int failures;
#define CHECK(c, ...) do { if (!(c)) { printf( "FAIL %s:%d: ", __func__, __LINE__ ); printf( __VA_ARGS__ ); printf( "\n" ); fflush( stdout ); failures++; } } while (0)

/* ============================================================== bit writer for SPS / PPS */
struct bw { uint8_t buf[512]; uint32_t bits; };
static void bw_u( struct bw *w, uint64_t v, int n )
{
    while (n-- > 0)
    {
        if ((v >> n) & 1) w->buf[w->bits >> 3] |= 0x80 >> (w->bits & 7);
        w->bits++;
    }
}
static void bw_ue( struct bw *w, uint32_t v )
{
    uint64_t x = (uint64_t)v + 1;
    int len = 0;
    while ((x >> len) > 1) len++;
    bw_u( w, 0, len );
    bw_u( w, x, len + 1 );
}
static void bw_se( struct bw *w, int32_t v ) { bw_ue( w, v > 0 ? 2 * (uint32_t)v - 1 : 2 * (uint32_t)(-v) ); }
/* rbsp trailing bits, header byte, emulation prevention */
static uint32_t bw_nal( struct bw *w, uint8_t header, uint8_t *out )
{
    uint32_t n, i, o = 0, zeros = 0;
    bw_u( w, 1, 1 );
    while (w->bits & 7) bw_u( w, 0, 1 );
    n = w->bits / 8;
    out[o++] = header;
    for (i = 0; i < n; i++)
    {
        if (zeros >= 2 && w->buf[i] <= 3) { out[o++] = 3; zeros = 0; }
        out[o++] = w->buf[i];
        zeros = w->buf[i] ? 0 : zeros + 1;
    }
    return o;
}

struct sps_desc
{
    uint32_t profile, constraints, level, id;
    uint32_t chroma, scaling;
    uint32_t poc_type;
    uint32_t width_mbs, height_units, frame_mbs_only;
    uint32_t crop_l, crop_r, crop_t, crop_b;
    int vui, sar_idc, sar_w, sar_h, full_range, timing, units, scale, hrd, restriction, reorder, dec_buf;
};

static uint32_t make_sps( const struct sps_desc *d, uint8_t *out )
{
    struct bw w;
    memset( &w, 0, sizeof(w) );
    bw_u( &w, d->profile, 8 );
    bw_u( &w, d->constraints, 8 );
    bw_u( &w, d->level, 8 );
    bw_ue( &w, d->id );
    if (d->profile == 100 || d->profile == 110 || d->profile == 122 || d->profile == 244)
    {
        bw_ue( &w, d->chroma );
        if (d->chroma == 3) bw_u( &w, 0, 1 );
        bw_ue( &w, 0 );
        bw_ue( &w, 0 );
        bw_u( &w, 0, 1 );
        bw_u( &w, d->scaling, 1 );
        if (d->scaling)
        {
            int i, j, lists = d->chroma != 3 ? 8 : 12;
            for (i = 0; i < lists; i++)
            {
                bw_u( &w, i == 0 || i == 6, 1 );     /* two lists present */
                if (i == 0 || i == 6)
                    for (j = 0; j < (i < 6 ? 16 : 64); j++) bw_se( &w, j == 0 ? 8 : (j & 1 ? 3 : -3) );
            }
        }
    }
    bw_ue( &w, 4 );                 /* log2_max_frame_num_minus4 */
    bw_ue( &w, d->poc_type );
    if (d->poc_type == 0) bw_ue( &w, 6 );
    else if (d->poc_type == 1)
    {
        bw_u( &w, 0, 1 ); bw_se( &w, -2 ); bw_se( &w, 1 ); bw_ue( &w, 2 ); bw_se( &w, 4 ); bw_se( &w, -1 );
    }
    bw_ue( &w, 4 );                 /* max_num_ref_frames */
    bw_u( &w, 0, 1 );
    bw_ue( &w, d->width_mbs - 1 );
    bw_ue( &w, d->height_units - 1 );
    bw_u( &w, d->frame_mbs_only, 1 );
    if (!d->frame_mbs_only) bw_u( &w, 1, 1 );
    bw_u( &w, 1, 1 );
    bw_u( &w, d->crop_l || d->crop_r || d->crop_t || d->crop_b, 1 );
    if (d->crop_l || d->crop_r || d->crop_t || d->crop_b)
    {
        bw_ue( &w, d->crop_l ); bw_ue( &w, d->crop_r ); bw_ue( &w, d->crop_t ); bw_ue( &w, d->crop_b );
    }
    bw_u( &w, d->vui, 1 );
    if (d->vui)
    {
        bw_u( &w, d->sar_idc != 0, 1 );
        if (d->sar_idc)
        {
            bw_u( &w, d->sar_idc, 8 );
            if (d->sar_idc == 255) { bw_u( &w, d->sar_w, 16 ); bw_u( &w, d->sar_h, 16 ); }
        }
        bw_u( &w, 0, 1 );           /* overscan */
        bw_u( &w, d->full_range >= 0, 1 );
        if (d->full_range >= 0)
        {
            bw_u( &w, 5, 3 ); bw_u( &w, d->full_range, 1 ); bw_u( &w, 1, 1 );
            bw_u( &w, 1, 8 ); bw_u( &w, 1, 8 ); bw_u( &w, 1, 8 );
        }
        bw_u( &w, 0, 1 );           /* chroma loc */
        bw_u( &w, d->timing, 1 );
        if (d->timing) { bw_u( &w, d->units, 32 ); bw_u( &w, d->scale, 32 ); bw_u( &w, 1, 1 ); }
        bw_u( &w, d->hrd, 1 );      /* nal hrd */
        if (d->hrd)
        {
            bw_ue( &w, 1 ); bw_u( &w, 4, 4 ); bw_u( &w, 6, 4 );
            bw_ue( &w, 1000 ); bw_ue( &w, 2000 ); bw_u( &w, 0, 1 );
            bw_ue( &w, 5000 ); bw_ue( &w, 9000 ); bw_u( &w, 1, 1 );
            bw_u( &w, 23, 5 ); bw_u( &w, 23, 5 ); bw_u( &w, 23, 5 ); bw_u( &w, 24, 5 );
        }
        bw_u( &w, 0, 1 );           /* vcl hrd */
        if (d->hrd) bw_u( &w, 0, 1 );
        bw_u( &w, 0, 1 );           /* pic_struct_present */
        bw_u( &w, d->restriction, 1 );
        if (d->restriction)
        {
            bw_u( &w, 1, 1 ); bw_ue( &w, 2 ); bw_ue( &w, 1 ); bw_ue( &w, 16 ); bw_ue( &w, 16 );
            bw_ue( &w, d->reorder ); bw_ue( &w, d->dec_buf );
        }
    }
    return bw_nal( &w, 0x67, out );
}

static uint32_t make_pps( uint32_t pps_id, uint32_t sps_id, uint8_t *out )
{
    struct bw w;
    memset( &w, 0, sizeof(w) );
    bw_ue( &w, pps_id );
    bw_ue( &w, sps_id );
    bw_u( &w, 1, 1 );  bw_u( &w, 0, 1 );  bw_ue( &w, 0 );  bw_ue( &w, 0 );  bw_ue( &w, 0 );
    bw_u( &w, 0, 1 );  bw_u( &w, 0, 2 );  bw_se( &w, 0 );  bw_se( &w, 0 );  bw_se( &w, 0 );
    bw_u( &w, 1, 1 );  bw_u( &w, 0, 1 );  bw_u( &w, 0, 1 );
    return bw_nal( &w, 0x68, out );
}

static void test_sps(void)
{
    static const struct { struct sps_desc d; uint32_t w, h, cw, ch, reorder; } cases[] =
    {
        /* 1080p High, cropped 1088 -> 1080, VUI timing 30000/1001 + restriction 2 */
        { { 100, 0, 40, 0, 1, 0, 0, 120, 68, 1, 0, 0, 0, 4, 1, 1, 0, 0, -1, 1, 1001, 60000, 0, 1, 2, 4 }, 1920, 1080, 1920, 1088, 2 },
        /* Baseline, POC type 2, odd width by cropping, no VUI: no reordering */
        { { 66, 0xc0, 30, 1, 0, 0, 2, 21, 12, 1, 0, 3, 0, 3, 0 }, 330, 186, 336, 192, 0 },
        /* Main, no restriction: the level 3.1 DPB at 1280x720 is 18000 / 3600 = 5 */
        { { 77, 0x40, 31, 0, 0, 0, 0, 80, 45, 1, 0, 0, 0, 0, 1, 255, 4, 3, 0, 0, 0, 0, 1, 0 }, 1280, 720, 1280, 720, 5 },
        /* High with scaling lists and an interlaced (field) layout, POC type 1, HRD present;
         * the vertical crop unit of a field picture is 4 rows */
        { { 100, 0, 41, 2, 1, 1, 1, 120, 34, 0, 0, 0, 0, 2, 1, 1, 0, 0, 1, 1, 1, 50, 1, 1, 3, 4 }, 1920, 1080, 1920, 1088, 3 },
        /* High 4:2:2 (chroma 2): vertical crop unit is 1 */
        { { 122, 0, 40, 3, 2, 0, 0, 40, 23, 1, 0, 0, 0, 8, 0 }, 640, 360, 640, 368, 16 },
    };
    unsigned int i;

    for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++)
    {
        uint8_t nal[256];
        struct mtv_sps s;
        uint32_t n = make_sps( &cases[i].d, nal ), depth;
        int ok = mtv_parse_sps( nal, n, &s );
        CHECK( ok, "sps %u: not parsed", i );
        if (!ok) continue;
        CHECK( s.width == cases[i].w && s.height == cases[i].h, "sps %u: picture %ux%u, expected %ux%u", i, s.width, s.height, cases[i].w, cases[i].h );
        CHECK( s.coded_width == cases[i].cw && s.coded_height == cases[i].ch, "sps %u: coded %ux%u", i, s.coded_width, s.coded_height );
        CHECK( s.id == cases[i].d.id && s.profile_idc == cases[i].d.profile && s.level_idc == cases[i].d.level, "sps %u: id/profile/level", i );
        depth = mtv_reorder_depth( &s );
        if (i != 4) CHECK( depth == cases[i].reorder, "sps %u: reorder depth %u, expected %u", i, depth, cases[i].reorder );
        else CHECK( depth >= 1 && depth <= 16, "sps %u: reorder depth %u", i, depth );
        if (cases[i].d.timing)
            CHECK( s.fps_n * (uint64_t)cases[i].d.units * 2 == (uint64_t)cases[i].d.scale * s.fps_d, "sps %u: fps %u/%u", i, s.fps_n, s.fps_d );
        if (cases[i].d.sar_idc == 255) CHECK( s.par_n == 4 && s.par_d == 3, "sps %u: sar %u:%u", i, s.par_n, s.par_d );
        if (cases[i].d.sar_idc == 1) CHECK( s.par_n == 1 && s.par_d == 1, "sps %u: sar %u:%u", i, s.par_n, s.par_d );
        if (cases[i].d.vui && cases[i].d.full_range >= 0) CHECK( s.full_range == cases[i].d.full_range, "sps %u: range %d", i, s.full_range );
        CHECK( s.has_reorder == cases[i].d.restriction, "sps %u: restriction flag", i );
        if (cases[i].d.restriction) CHECK( s.max_num_reorder_frames == (uint32_t)cases[i].d.reorder, "sps %u: reorder field", i );
        /* truncated: never a crash, never a wrong size */
        {
            uint32_t cut;
            for (cut = 1; cut < n; cut++)
            {
                struct mtv_sps t;
                if (mtv_parse_sps( nal, cut, &t )) CHECK( t.width == s.width && t.height == s.height, "sps %u cut %u: %ux%u", i, cut, t.width, t.height );
            }
        }
    }
    printf( "sps parser: %u cases\n", (unsigned)(sizeof(cases) / sizeof(cases[0])) );
}

/* ============================================================== stub video backend */
static uint32_t stub_tag( int64_t pts ) { return (uint32_t)((pts / 1000) & 0xffff); }
static uint8_t pat_y( uint32_t tag, uint32_t x, uint32_t y ) { return (uint8_t)(tag * 13 + x * 3 + y * 7); }
static uint8_t pat_u( uint32_t tag, uint32_t x, uint32_t y ) { return (uint8_t)(tag * 5 + x + 64 + y); }
static uint8_t pat_v( uint32_t tag, uint32_t x, uint32_t y ) { return (uint8_t)(tag * 11 + y + 128 + 2 * x); }

struct stub_vdec { uint32_t w, h; };
struct stub_frame { uint32_t w, h, ys, uvs, tag; uint8_t *y, *uv; };

static unsigned int vs_opens, vs_closes, vs_flushes, vs_live, vs_bad_nals, vs_packets, vs_refuse, vs_maps;
static uint32_t vs_last_w, vs_last_h, vs_last_nsps, vs_last_npps;

static int vs_supports( int codec ) { return codec == MAV_BACKEND_H264; }

static void *vs_open( int codec, const uint8_t *ed, uint32_t n, uint32_t w, uint32_t h, char *why, size_t why_size )
{
    struct stub_vdec *d;
    uint32_t pos = 6, i, nsps, npps;

    if (codec != MAV_BACKEND_H264 || vs_refuse)
    {
        snprintf( why, why_size, "refused by the test" );
        return NULL;
    }
    CHECK( n >= 7 && ed[0] == 1 && ed[4] == 0xff && (ed[5] & 0xe0) == 0xe0, "avcC header" );
    nsps = ed[5] & 0x1f;
    for (i = 0; i < nsps && pos + 2 <= n; i++)
    {
        uint32_t len = (ed[pos] << 8) | ed[pos + 1];
        CHECK( pos + 2 + len <= n && (ed[pos + 2] & 0x1f) == 7, "avcC SPS %u", i );
        pos += 2 + len;
    }
    CHECK( pos < n, "avcC: no PPS count" );
    npps = pos < n ? ed[pos++] : 0;
    for (i = 0; i < npps && pos + 2 <= n; i++)
    {
        uint32_t len = (ed[pos] << 8) | ed[pos + 1];
        CHECK( pos + 2 + len <= n && (ed[pos + 2] & 0x1f) == 8, "avcC PPS %u", i );
        pos += 2 + len;
    }
    CHECK( pos == n, "avcC length %u != %u", pos, n );
    CHECK( nsps >= 1 && npps >= 1, "avcC with %u SPS / %u PPS", nsps, npps );
    vs_last_nsps = nsps;
    vs_last_npps = npps;
    d = calloc( 1, sizeof(*d) );
    d->w = w ? w : 64;
    d->h = h ? h : 64;
    vs_last_w = d->w;
    vs_last_h = d->h;
    vs_opens++;
    return d;
}

static int vs_decode( void *handle, const uint8_t *data, uint32_t size, int64_t pts, int64_t duration, int keyframe,
                      mav_vframe_emit emit, void *ctx )
{
    struct stub_vdec *d = handle;
    struct stub_frame *f;
    uint32_t pos = 0, x, y, slices = 0, fail = 0;

    vs_packets++;
    while (pos + 4 <= size)
    {
        uint32_t len = ((uint32_t)data[pos] << 24) | (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3];
        unsigned int type;
        pos += 4;
        if (!len || len > size - pos) { vs_bad_nals++; return -1; }
        type = data[pos] & 0x1f;
        if (type == 7 || type == 8 || type == 9 || type == 12 || type == 10 || type == 11) vs_bad_nals++;
        if (type >= 1 && type <= 5)
        {
            slices++;
            if (len >= 5 && data[pos + 1] == 0xab && data[pos + 4] == 0xee) fail = 1;
        }
        pos += len;
    }
    if (pos != size) vs_bad_nals++;
    if (fail) return -12909;          /* kVTVideoDecoderBadDataErr */
    if (!slices) return 0;
    f = calloc( 1, sizeof(*f) );
    f->w = d->w;
    f->h = d->h;
    f->ys = (d->w + 63) & ~63u;      /* VideoToolbox pads its rows */
    f->uvs = f->ys;
    f->tag = stub_tag( pts );
    f->y = malloc( (size_t)f->ys * f->h );
    f->uv = malloc( (size_t)f->uvs * ((f->h + 1) / 2) );
    for (y = 0; y < f->h; y++)
        for (x = 0; x < f->ys; x++) f->y[(size_t)y * f->ys + x] = x < f->w ? pat_y( f->tag, x, y ) : 0xee;
    for (y = 0; y < (f->h + 1) / 2; y++)
        for (x = 0; x < f->uvs / 2; x++)
        {
            f->uv[(size_t)y * f->uvs + 2 * x] = x < (f->w + 1) / 2 ? pat_u( f->tag, x, y ) : 0xee;
            f->uv[(size_t)y * f->uvs + 2 * x + 1] = x < (f->w + 1) / 2 ? pat_v( f->tag, x, y ) : 0xee;
        }
    vs_live++;
    (void)keyframe;
    emit( ctx, f, pts, duration );
    return 0;
}

static int vs_map( void *frame, struct mav_vplanes *p )
{
    struct stub_frame *f = frame;
    p->y = f->y; p->uv = f->uv; p->y_stride = f->ys; p->uv_stride = f->uvs; p->width = f->w; p->height = f->h;
    p->full_range = 0;
    vs_maps++;
    return 0;
}
static void vs_unmap( void *frame ) { (void)frame; }
static void vs_release( void *frame )
{
    struct stub_frame *f = frame;
    free( f->y ); free( f->uv ); free( f );
    vs_live--;
}
static void vs_flush( void *handle ) { (void)handle; vs_flushes++; }
static void vs_close( void *handle ) { free( handle ); vs_closes++; }

static const struct mav_video_backend stub_video = { "stubvt", vs_supports, vs_open, vs_decode, vs_map, vs_unmap, vs_release, vs_flush, vs_close };

/* ============================================================== the expected MF layout, from the MF rules */
struct exp_layout { uint32_t s0, s1, rows; size_t off1, off2, size; };

static void expect_layout( enum mtx_pix pix, uint32_t w, uint32_t h, uint32_t align, uint32_t stride, struct exp_layout *e )
{
    uint32_t a = align + 1, pw = (w + align) / a * a, ph = (h + align) / a * a;
    memset( e, 0, sizeof(*e) );
    if (stride && stride / (pix == MTX_PIX_YUY2 ? 2 : 1) > pw) pw = stride / (pix == MTX_PIX_YUY2 ? 2 : 1);
    e->rows = ph;
    switch (pix)
    {
    case MTX_PIX_NV12:
        e->s0 = e->s1 = pw;
        e->off1 = (size_t)pw * ph;
        e->size = e->off1 + (size_t)pw * ph / 2;
        break;
    case MTX_PIX_I420:
    case MTX_PIX_YV12:
        e->s0 = pw; e->s1 = pw / 2;
        e->off1 = (size_t)pw * ph;                    /* the first chroma plane in memory */
        e->off2 = e->off1 + (size_t)e->s1 * ph / 2;
        e->size = e->off2 + (size_t)e->s1 * ph / 2;
        break;
    case MTX_PIX_YUY2:
        e->s0 = pw * 2;
        e->size = (size_t)e->s0 * ph;
        break;
    default: break;
    }
}

/* every byte of a frame: the picture where it is, black around it */
static int check_frame( const char *name, enum mtx_pix pix, const uint8_t *buf, uint32_t size, uint32_t w, uint32_t h,
                        uint32_t align, uint32_t stride, uint32_t tag )
{
    struct exp_layout e;
    uint32_t x, y, bad = 0;

    expect_layout( pix, w, h, align, stride, &e );
    if (size != e.size)
    {
        CHECK( 0, "%s: frame size %u, expected %zu (%ux%u)", name, size, e.size, w, h );
        return 0;
    }
    for (y = 0; y < e.rows && bad < 4; y++)
    {
        if (pix == MTX_PIX_YUY2)
        {
            for (x = 0; x < e.s0 / 2 && bad < 4; x++)
            {
                uint8_t yy = buf[(size_t)y * e.s0 + 2 * x], c = buf[(size_t)y * e.s0 + 2 * x + 1];
                uint8_t ey = (x < w && y < h) ? pat_y( tag, x, y ) : 16;
                uint8_t ec = (x < ((w + 1) & ~1u) && y < h) ? ((x & 1) ? pat_v( tag, x / 2, y / 2 ) : pat_u( tag, x / 2, y / 2 )) : 128;
                if (yy != ey || c != ec) { bad++; CHECK( 0, "%s: yuy2 (%u,%u) = %u/%u, expected %u/%u", name, x, y, yy, c, ey, ec ); }
            }
            continue;
        }
        for (x = 0; x < e.s0 && bad < 4; x++)
        {
            uint8_t v = buf[(size_t)y * e.s0 + x], ev = (x < w && y < h) ? pat_y( tag, x, y ) : 16;
            if (v != ev) { bad++; CHECK( 0, "%s: Y (%u,%u) = %u, expected %u", name, x, y, v, ev ); }
        }
    }
    if (pix == MTX_PIX_NV12)
        for (y = 0; y < e.rows / 2 && bad < 4; y++)
            for (x = 0; x < e.s1 / 2 && bad < 4; x++)
            {
                const uint8_t *p = buf + e.off1 + (size_t)y * e.s1 + 2 * x;
                int in = x < (w + 1) / 2 && y < (h + 1) / 2;
                uint8_t eu = in ? pat_u( tag, x, y ) : 128, ev = in ? pat_v( tag, x, y ) : 128;
                if (p[0] != eu || p[1] != ev) { bad++; CHECK( 0, "%s: UV (%u,%u) = %u/%u, expected %u/%u", name, x, y, p[0], p[1], eu, ev ); }
            }
    if (pix == MTX_PIX_I420 || pix == MTX_PIX_YV12)
    {
        size_t uoff = pix == MTX_PIX_I420 ? e.off1 : e.off2, voff = pix == MTX_PIX_I420 ? e.off2 : e.off1;
        for (y = 0; y < e.rows / 2 && bad < 4; y++)
            for (x = 0; x < e.s1 && bad < 4; x++)
            {
                int in = x < (w + 1) / 2 && y < (h + 1) / 2;
                uint8_t u = buf[uoff + (size_t)y * e.s1 + x], v = buf[voff + (size_t)y * e.s1 + x];
                uint8_t eu = in ? pat_u( tag, x, y ) : 128, ev = in ? pat_v( tag, x, y ) : 128;
                if (u != eu || v != ev) { bad++; CHECK( 0, "%s: U/V (%u,%u) = %u/%u, expected %u/%u", name, x, y, u, v, eu, ev ); }
            }
    }
    return !bad;
}

/* ============================================================== driving the transform like video_decoder.c */
struct unit { uint8_t *data; uint32_t size, key; int64_t pts, duration; };

struct run_opts
{
    enum mtx_pix pix;           /* what the caller picks after the stream change */
    uint32_t stride;            /* 2D buffer stride, 0 for 1D */
    uint32_t queue;             /* attrs.input_queue_length */
    int format_change;
    uint32_t in_w, in_h;        /* the input type's frame size */
};

struct run_result { unsigned int frames, changes, bad_order, missing; int64_t pts[4096]; uint32_t widths[4096], heights[4096]; };

static int read_all( struct mtv *v, const char *name, const struct run_opts *o, struct run_result *r, uint8_t *buf, uint32_t cap )
{
    for (;;)
    {
        struct mtx_out out;
        int ret;
        memset( &out, 0, sizeof(out) );
        out.data = buf;
        out.max_size = cap;
        out.stride = o->stride;
        ret = mtv_read( v, &out );
        if (ret == MTX_NEED_MORE_INPUT) return 0;
        if (ret == MTX_STREAM_CHANGE)
        {
            struct mtv_output_info info;
            mtv_get_output( v, &info );
            r->changes++;
            CHECK( info.frame_width == ((info.width + 15) & ~15u) && info.frame_height == ((info.height + 15) & ~15u),
                   "%s: output type %ux%u for a %ux%u picture", name, info.frame_width, info.frame_height, info.width, info.height );
            CHECK( info.par_n && info.par_d, "%s: no pixel aspect ratio", name );
            /* GetOutputAvailableType + SetOutputType, as the caller renegotiates */
            CHECK( !mtv_set_output( v, o->pix, info.width, info.height, info.frame_width, info.frame_height,
                                    info.frame_width != info.width || info.frame_height != info.height ? info.width : 0,
                                    info.frame_width != info.width || info.frame_height != info.height ? info.height : 0 ),
                   "%s: set_output", name );
            continue;
        }
        if (ret != MTX_OK)
        {
            CHECK( 0, "%s: read returned %d", name, ret );
            return -1;
        }
        CHECK( (out.flags & (MTX_FLAG_HAS_PTS | MTX_FLAG_PRESERVE_TIMESTAMPS | MTX_FLAG_SYNC_POINT))
               == (MTX_FLAG_HAS_PTS | MTX_FLAG_PRESERVE_TIMESTAMPS | MTX_FLAG_SYNC_POINT), "%s: flags %#x", name, out.flags );
        if (r->frames && out.pts <= r->pts[r->frames - 1]) r->bad_order++;
        {
            struct mtv_output_info info;
            char what[96];
            mtv_get_output( v, &info );
            snprintf( what, sizeof(what), "%s frame %u", name, r->frames );
            check_frame( what, o->pix, buf, out.size, info.width, info.height, 15, o->stride, stub_tag( out.pts ) );
            if (r->frames < 4096) { r->widths[r->frames] = info.width; r->heights[r->frames] = info.height; }
        }
        if (r->frames < 4096) r->pts[r->frames] = out.pts;
        r->frames++;
    }
}

static int run_units( const char *name, struct unit *units, unsigned int count, const struct run_opts *o, struct run_result *r )
{
    struct mtv_config cfg;
    struct mtv *v;
    static uint8_t buf[8 << 20];
    unsigned int i;

    memset( r, 0, sizeof(*r) );
    memset( &cfg, 0, sizeof(cfg) );
    cfg.in_width = o->in_w; cfg.in_height = o->in_h;
    cfg.pix = MTX_PIX_NV12;
    cfg.out_width = cfg.dw_width = o->in_w ? o->in_w : 1920;
    cfg.out_height = cfg.dw_height = o->in_h ? o->in_h : 1080;
    cfg.plane_align = 15;
    cfg.input_queue_length = o->queue;
    cfg.allow_format_change = o->format_change;
    cfg.preserve_timestamps = 1;
    if (mtv_create( &cfg, &v ))
    {
        CHECK( 0, "%s: create", name );
        return -1;
    }
    for (i = 0; i < count; i++)
    {
        struct mtx_in in = { units[i].data, units[i].size, MTX_FLAG_HAS_PTS | MTX_FLAG_HAS_DURATION | (units[i].key ? MTX_FLAG_SYNC_POINT : 0),
                             units[i].pts, (uint64_t)units[i].duration };
        int ret = mtv_push( v, &in );
        if (ret == MTX_NOT_ACCEPTING)
        {
            CHECK( !mtv_accepts_input( v ), "%s: refused input while accepting", name );
            read_all( v, name, o, r, buf, sizeof(buf) );
            ret = mtv_push( v, &in );
        }
        CHECK( ret == MTX_OK, "%s: push %u returned %d", name, i, ret );
    }
    mtv_drain( v );
    read_all( v, name, o, r, buf, sizeof(buf) );
    mtv_destroy( v );
    return 0;
}

static void check_order( const char *name, struct unit *units, unsigned int count, const struct run_result *r )
{
    unsigned int i, j, found = 0;
    CHECK( r->frames == count, "%s: %u pictures out of %u access units", name, r->frames, count );
    CHECK( !r->bad_order, "%s: %u pictures out of presentation order", name, r->bad_order );
    for (i = 0; i < count; i++)
        for (j = 0; j < r->frames && j < 4096; j++)
            if (r->pts[j] == units[i].pts) { found++; break; }
    CHECK( found == count, "%s: %u of %u timestamps came back", name, found, count );
}

/* --------------------------------------------------- synthetic streams */
static uint32_t make_slice( int idr, uint32_t index, int fail, uint8_t *out )
{
    out[0] = idr ? 0x65 : 0x41;
    out[1] = 0xab;
    out[2] = index >> 8;
    out[3] = index & 0xff;
    out[4] = fail ? 0xee : 0x00;
    out[5] = 0x80;
    return 6;
}

static uint32_t put_nal( uint8_t *dst, const uint8_t *nal, uint32_t n, int four )
{
    uint32_t o = 0;
    if (four) dst[o++] = 0;
    dst[o++] = 0; dst[o++] = 0; dst[o++] = 1;
    memcpy( dst + o, nal, n );
    return o + n;
}

/* An IBBP-like stream: decode order I0 P3 B1 B2 P6 B4 B5 ... with SPS/PPS/AUD/SEI in front
 * of each IDR (every `gop` frames), and the timestamps of the presentation order. */
static unsigned int make_synthetic( const struct sps_desc *sd, unsigned int frames, unsigned int gop, int64_t base,
                                    struct unit *units, int fail_at )
{
    int reorder = sd->poc_type != 2 && sd->profile != 66;
    static const int order[3] = { 2, 0, 1 };   /* P then the two B's before it */
    uint8_t sps[256], pps[64], slice[16];
    uint32_t sps_n = make_sps( sd, sps ), pps_n = make_pps( 0, sd->id, pps );
    unsigned int n = 0, i, g;

    for (g = 0; g < frames; g += gop)
    {
        unsigned int in_gop = frames - g < gop ? frames - g : gop;
        unsigned int dec[64], k = 0;
        dec[k++] = 0;
        for (i = 1; reorder && i + 2 < in_gop; i += 3)
        {
            dec[k++] = i + order[0];
            dec[k++] = i + order[1];
            dec[k++] = i + order[2];
        }
        for (; i < in_gop; i++) dec[k++] = i;
        for (i = 0; i < k; i++)
        {
            uint8_t *p = malloc( 512 );
            uint32_t o = 0;
            uint8_t aud[2] = { 0x09, 0xf0 }, sei[6] = { 0x06, 0x05, 0x01, 0x42, 0x80, 0x00 };
            unsigned int index = g + dec[i];
            o += put_nal( p + o, aud, 2, 1 );
            if (!i)
            {
                o += put_nal( p + o, sps, sps_n, 1 );
                o += put_nal( p + o, pps, pps_n, 0 );
                o += put_nal( p + o, sei, 5, 0 );
            }
            o += put_nal( p + o, slice, make_slice( !i, index, (int)n == fail_at, slice ), 1 );
            p[o++] = 0;                    /* trailing_zero_8bits */
            units[n].data = p;
            units[n].size = o;
            units[n].key = !i;
            units[n].pts = base + (int64_t)index * 333667;
            units[n].duration = 333667;
            n++;
        }
    }
    return n;
}

static void free_units( struct unit *u, unsigned int n )
{
    unsigned int i;
    for (i = 0; i < n; i++) free( u[i].data );
}

static void test_synthetic(void)
{
    static struct unit units[600], more[300];
    struct sps_desc a = { 100, 0, 40, 0, 1, 0, 0, 20, 12, 1, 0, 0, 0, 6, 1, 1, 0, 0, -1, 1, 1001, 60000, 0, 1, 2, 3 };
    struct sps_desc b = { 66, 0xc0, 30, 0, 0, 0, 2, 21, 12, 1, 0, 3, 0, 3, 0 };
    static const enum mtx_pix pixes[4] = { MTX_PIX_NV12, MTX_PIX_I420, MTX_PIX_YV12, MTX_PIX_YUY2 };
    static struct run_result r;
    struct run_opts o;
    unsigned int n, m, i, p;
    char name[96];

    n = make_synthetic( &a, 40, 10, 0, units, -1 );
    for (p = 0; p < 4; p++)
    {
        memset( &o, 0, sizeof(o) );
        o.pix = pixes[p]; o.queue = 15; o.format_change = 1; o.in_w = 320; o.in_h = 180;
        snprintf( name, sizeof(name), "synthetic 320x180 high, format %d", pixes[p] );
        vs_bad_nals = 0;
        run_units( name, units, n, &o, &r );
        check_order( name, units, n, &r );
        CHECK( r.changes == 1, "%s: %u stream changes", name, r.changes );
        CHECK( !vs_bad_nals, "%s: %u parameter set / delimiter NAL units reached the decoder", name, vs_bad_nals );
        CHECK( vs_last_w == 320 && vs_last_h == 180, "%s: decoder opened at %ux%u", name, vs_last_w, vs_last_h );
    }
    /* a 2D buffer with a wider stride, and a tiny input queue */
    memset( &o, 0, sizeof(o) );
    o.pix = MTX_PIX_NV12; o.queue = 2; o.format_change = 1; o.stride = 512;
    run_units( "synthetic 2D stride 512, queue 2", units, n, &o, &r );
    check_order( "synthetic 2D stride 512, queue 2", units, n, &r );

    /* resolution change mid-stream: a second SPS, the decoder is reopened, a second stream change */
    m = make_synthetic( &b, 20, 10, (int64_t)40 * 333667, more, -1 );
    for (i = 0; i < m; i++) units[n + i] = more[i];
    vs_opens = 0;
    memset( &o, 0, sizeof(o) );
    o.pix = MTX_PIX_I420; o.queue = 15; o.format_change = 1;
    run_units( "synthetic resolution change", units, n + m, &o, &r );
    check_order( "synthetic resolution change", units, n + m, &r );
    CHECK( r.changes == 2, "resolution change: %u stream changes", r.changes );
    CHECK( vs_opens == 2, "resolution change: %u decoder opens", vs_opens );
    CHECK( r.widths[0] == 320 && r.heights[0] == 180 && r.widths[n] == 330 && r.heights[n] == 186,
           "resolution change: %ux%u then %ux%u", r.widths[0], r.heights[0], r.widths[n], r.heights[n] );
    free_units( units, n + m );

    /* a decode error drops one picture and nothing else */
    n = make_synthetic( &a, 20, 10, 0, units, 7 );
    memset( &o, 0, sizeof(o) );
    o.pix = MTX_PIX_NV12; o.queue = 15; o.format_change = 1;
    run_units( "synthetic decode error", units, n, &o, &r );
    CHECK( r.frames == n - 1 && !r.bad_order, "decode error: %u pictures of %u, %u out of order", r.frames, n, r.bad_order );
    free_units( units, n );
    printf( "synthetic h264: done\n" );
}

/* queue limit, buffer too small, flush, drain, no format change */
static void test_protocol(void)
{
    static struct unit units[64];
    struct sps_desc a = { 77, 0x40, 30, 0, 0, 0, 0, 20, 12, 1, 0, 0, 0, 6, 1, 1, 0, 0, -1, 0, 0, 0, 0, 1, 1, 2 };
    static uint8_t buf[1 << 20];
    struct mtv_config cfg;
    struct mtx_out out;
    struct mtv *v;
    unsigned int n = make_synthetic( &a, 30, 10, 0, units, -1 ), i, got = 0;
    int ret;

    memset( &cfg, 0, sizeof(cfg) );
    cfg.pix = MTX_PIX_NV12; cfg.out_width = cfg.dw_width = 320; cfg.out_height = cfg.dw_height = 180;
    cfg.plane_align = 15; cfg.input_queue_length = 3; cfg.allow_format_change = 1; cfg.preserve_timestamps = 1;
    CHECK( !mtv_create( &cfg, &v ), "create" );
    for (i = 0; i < 4; i++)
    {
        struct mtx_in in = { units[i].data, units[i].size, MTX_FLAG_HAS_PTS | (units[i].key ? MTX_FLAG_SYNC_POINT : 0), units[i].pts, 0 };
        CHECK( mtv_push( v, &in ) == MTX_OK, "push %u", i );
    }
    CHECK( !mtv_accepts_input( v ), "accepting with input_queue_length + 1 queued" );
    {
        struct mtx_in in = { units[4].data, units[4].size, MTX_FLAG_HAS_PTS, units[4].pts, 0 };
        CHECK( mtv_push( v, &in ) == MTX_NOT_ACCEPTING, "a fifth sample was accepted" );
    }
    memset( &out, 0, sizeof(out) );
    out.data = buf; out.max_size = sizeof(buf);
    CHECK( mtv_read( v, &out ) == MTX_STREAM_CHANGE, "first read is not a stream change" );
    CHECK( !mtv_set_output( v, MTX_PIX_NV12, 320, 180, 320, 192, 320, 180 ), "set_output" );
    out.max_size = 320 * 192 * 3 / 2 - 1;
    CHECK( mtv_read( v, &out ) == MTX_BUFFER_TOO_SMALL, "a short buffer was filled" );
    out.max_size = sizeof(buf);
    ret = mtv_read( v, &out );
    CHECK( ret == MTX_OK && out.size == 320 * 192 * 3 / 2 && out.pts == 0, "read after too small: %d, %u bytes, pts %lld", ret, out.size, (long long)out.pts );
    CHECK( !(out.flags & MTX_FLAG_DISCONTINUITY), "discontinuity on the first picture" );
    CHECK( !(out.flags & MTX_FLAG_HAS_DURATION), "a duration out of nowhere" );
    /* flush: everything held is dropped, decoding waits for the next IDR */
    CHECK( !mtv_flush( v ), "flush" );
    CHECK( vs_live == 0, "%u pictures still alive after flush", vs_live );
    for (i = 5; i < n; i++)
    {
        struct mtx_in in = { units[i].data, units[i].size, MTX_FLAG_HAS_PTS | (units[i].key ? MTX_FLAG_SYNC_POINT : 0), units[i].pts, 0 };
        while ((ret = mtv_push( v, &in )) == MTX_NOT_ACCEPTING)
        {
            out.max_size = sizeof(buf);
            ret = mtv_read( v, &out );
            CHECK( ret == MTX_OK || ret == MTX_NEED_MORE_INPUT, "read %d", ret );
            if (ret == MTX_OK)
            {
                if (!got) CHECK( (out.flags & MTX_FLAG_DISCONTINUITY) && out.pts == 10 * 333667,
                                 "first picture after flush: pts %lld flags %#x", (long long)out.pts, out.flags );
                got++;
            }
        }
        CHECK( ret == MTX_OK, "push %u: %d", i, ret );
    }
    CHECK( !mtv_drain( v ), "drain" );
    for (;;)
    {
        out.max_size = sizeof(buf);
        ret = mtv_read( v, &out );
        if (ret != MTX_OK) break;
        got++;
    }
    CHECK( ret == MTX_NEED_MORE_INPUT, "end of drain: %d", ret );
    CHECK( got == 20, "after the flush: %u pictures, expected the 20 from the next IDR on", got );
    mtv_destroy( v );
    CHECK( vs_live == 0, "%u pictures leaked", vs_live );

    /* without allow_format_change: no stream change, the caller's size is produced */
    memset( &cfg, 0, sizeof(cfg) );
    cfg.pix = MTX_PIX_I420; cfg.out_width = cfg.dw_width = 352; cfg.out_height = cfg.dw_height = 160;
    cfg.plane_align = 0; cfg.input_queue_length = 15; cfg.preserve_timestamps = 0;
    CHECK( !mtv_create( &cfg, &v ), "create" );
    for (i = 0; i < 10; i++)
    {
        struct mtx_in in = { units[i].data, units[i].size, MTX_FLAG_HAS_PTS | (units[i].key ? MTX_FLAG_SYNC_POINT : 0), units[i].pts, 0 };
        CHECK( mtv_push( v, &in ) == MTX_OK, "push %u", i );
    }
    mtv_drain( v );
    memset( &out, 0, sizeof(out) );
    out.data = buf; out.max_size = sizeof(buf);
    ret = mtv_read( v, &out );
    CHECK( ret == MTX_OK, "no-format-change read: %d", ret );
    CHECK( out.size == 352 * 160 * 3 / 2, "no-format-change size %u", out.size );
    CHECK( !(out.flags & MTX_FLAG_PRESERVE_TIMESTAMPS) && (out.flags & MTX_FLAG_HAS_PTS), "flags %#x", out.flags );
    /* the 320x180 picture in a 352x160 frame: cropped at the bottom, black on the right */
    CHECK( buf[0] == pat_y( stub_tag( out.pts ), 0, 0 ) && buf[340] == 16 && buf[352 * 159 + 319] == pat_y( stub_tag( out.pts ), 319, 159 ),
           "no-format-change content" );
    mtv_destroy( v );

    /* no parameter sets at all: nothing decodes, nothing crashes */
    memset( &cfg, 0, sizeof(cfg) );
    cfg.pix = MTX_PIX_NV12; cfg.plane_align = 15; cfg.input_queue_length = 15; cfg.allow_format_change = 1;
    CHECK( !mtv_create( &cfg, &v ), "create" );
    {
        uint8_t slice[16], au[32];
        uint32_t o = put_nal( au, slice, make_slice( 1, 0, 0, slice ), 1 );
        struct mtx_in in = { au, o, MTX_FLAG_SYNC_POINT, 0, 0 };
        CHECK( mtv_push( v, &in ) == MTX_OK, "push without sets" );
        out.max_size = sizeof(buf);
        CHECK( mtv_read( v, &out ) == MTX_NEED_MORE_INPUT, "a picture without parameter sets" );
    }
    mtv_destroy( v );

    /* the codec data of the media type carries the parameter sets (Annex B and avcC) */
    for (i = 0; i < 2; i++)
    {
        uint8_t sps[256], pps[64], header[512], slice[16], au[32];
        uint32_t sn = make_sps( &a, sps ), pn = make_pps( 0, 0, pps ), hn = 0, o;
        struct mtx_in in;
        if (!i)
        {
            hn += put_nal( header + hn, sps, sn, 1 );
            hn += put_nal( header + hn, pps, pn, 1 );
        }
        else
        {
            header[hn++] = 1; header[hn++] = sps[1]; header[hn++] = sps[2]; header[hn++] = sps[3]; header[hn++] = 0xff; header[hn++] = 0xe1;
            header[hn++] = sn >> 8; header[hn++] = sn & 0xff; memcpy( header + hn, sps, sn ); hn += sn;
            header[hn++] = 1; header[hn++] = pn >> 8; header[hn++] = pn & 0xff; memcpy( header + hn, pps, pn ); hn += pn;
        }
        memset( &cfg, 0, sizeof(cfg) );
        cfg.pix = MTX_PIX_NV12; cfg.plane_align = 15; cfg.input_queue_length = 15; cfg.allow_format_change = 1;
        cfg.header = header; cfg.header_size = hn;
        CHECK( !mtv_create( &cfg, &v ), "create" );
        o = put_nal( au, slice, make_slice( 1, 0, 0, slice ), 1 );
        in.data = au; in.size = o; in.flags = MTX_FLAG_HAS_PTS; in.pts = 0; in.duration = 0;
        CHECK( mtv_push( v, &in ) == MTX_OK, "push" );
        mtv_drain( v );
        out.max_size = sizeof(buf);
        CHECK( mtv_read( v, &out ) == MTX_STREAM_CHANGE, "codec data %s: no picture", i ? "avcC" : "Annex B" );
        mtv_destroy( v );
    }
    CHECK( vs_live == 0, "%u pictures leaked", vs_live );
    printf( "protocol: done\n" );
}

/* real x264 streams written by the Python side */
static void test_file( const char *path, const char *name, enum mtx_pix pix, uint32_t w, uint32_t h )
{
    static struct unit units[4096];
    static struct run_result r;
    struct run_opts o;
    FILE *f = fopen( path, "rb" );
    unsigned int n = 0;

    if (!f) { CHECK( 0, "%s: cannot open %s", name, path ); return; }
    for (;;)
    {
        uint8_t head[24];
        uint32_t size, key;
        int64_t pts, dur;
        if (fread( head, 1, 24, f ) != 24) break;
        memcpy( &size, head, 4 ); memcpy( &pts, head + 4, 8 ); memcpy( &dur, head + 12, 8 ); memcpy( &key, head + 20, 4 );
        units[n].data = malloc( size );
        if (fread( units[n].data, 1, size, f ) != size) { free( units[n].data ); break; }
        units[n].size = size; units[n].pts = pts; units[n].duration = dur; units[n].key = key;
        n++;
    }
    fclose( f );
    memset( &o, 0, sizeof(o) );
    o.pix = pix; o.queue = 15; o.format_change = 1; o.in_w = w; o.in_h = h;
    vs_bad_nals = 0;
    run_units( name, units, n, &o, &r );
    check_order( name, units, n, &r );
    CHECK( r.changes == 1, "%s: %u stream changes", name, r.changes );
    CHECK( !vs_bad_nals, "%s: %u parameter set / delimiter NAL units reached the decoder", name, vs_bad_nals );
    CHECK( vs_last_w == w && vs_last_h == h, "%s: decoder opened at %ux%u, expected %ux%u", name, vs_last_w, vs_last_h, w, h );
    CHECK( r.widths[0] == ((w + 15) & ~15u) || 1, "unused" );
    printf( "%s: %u access units, %u pictures in order\n", name, n, r.frames );
    free_units( units, n );
}

/* ============================================================== stub audio backend */
static unsigned int as_opens, as_units, as_last_size, as_fail_next;
static uint8_t as_last_asc[8];
static uint32_t as_last_asc_size, as_double_rate;
static const uint32_t rates_tab[13] = { 96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350 };
struct stub_adec { uint32_t channels; };

static int as_supports( int codec ) { return codec == MAV_BACKEND_AAC; }
static void *as_open( int codec, const uint8_t *asc, uint32_t n, uint32_t *rate, uint32_t *channels, char *why, size_t ws )
{
    struct stub_adec *d;
    uint32_t fi, ch;
    if (n < 2) { snprintf( why, ws, "short config" ); return NULL; }
    memcpy( as_last_asc, asc, n < 8 ? n : 8 );
    as_last_asc_size = n;
    fi = ((asc[0] & 7) << 1) | (asc[1] >> 7);
    ch = (asc[1] >> 3) & 0xf;
    if (fi == 15) { *rate = ((asc[1] & 0x7f) << 17) | (asc[2] << 9) | (asc[3] << 1) | (asc[4] >> 7); ch = (asc[4] >> 3) & 0xf; }
    else if (fi < 13) *rate = rates_tab[fi];
    else { snprintf( why, ws, "bad index" ); return NULL; }
    if (as_double_rate) *rate *= 2;
    *channels = ch == 7 ? 8 : ch;
    d = calloc( 1, sizeof(*d) );
    d->channels = *channels;
    as_opens++;
    (void)codec;
    return d;
}
static int as_decode( void *handle, const uint8_t *data, uint32_t size, float *out, uint32_t max )
{
    struct stub_adec *d = handle;
    uint32_t i, c;
    as_units++;
    as_last_size = size;
    if (as_fail_next) { as_fail_next = 0; return -1; }
    if (max < 1024) return -2;
    for (i = 0; i < 1024; i++)
        for (c = 0; c < d->channels; c++)
            out[i * d->channels + c] = (float)(0.25 * sin( 2 * M_PI * 1000.0 * i / 48000.0 ) * (c + 1) / d->channels) + (data[0] - 128) / 4096.0f;
    return 1024;
}
static void as_flush( void *handle ) { (void)handle; }
static void as_close( void *handle ) { free( handle ); }
static const struct mav_audio_backend stub_audio = { "stubat", as_supports, as_open, as_decode, as_flush, as_close };

static uint32_t read_pcm( struct mta *a, uint8_t *buf, uint32_t cap, int64_t *first_pts, unsigned int *flags_or )
{
    uint32_t total = 0;
    for (;;)
    {
        struct mtx_out out;
        int ret;
        memset( &out, 0, sizeof(out) );
        out.data = buf + total;
        out.max_size = cap - total;
        ret = mta_read( a, &out );
        if (ret != MTX_OK) break;
        if (!total && first_pts) *first_pts = (out.flags & MTX_FLAG_HAS_PTS) ? out.pts : -1;
        if (flags_or) *flags_or |= out.flags;
        total += out.size;
    }
    return total;
}

static void test_aac(void)
{
    static uint8_t pcm[1 << 22];
    uint8_t asc_lc44[2] = { 0x12, 0x10 }, asc_51[2] = { 0x11, 0xb0 }, asc_24m[2] = { 0x13, 0x08 };
    struct mta_config cfg;
    struct mta *a;
    uint8_t unit[300];
    uint32_t got, i;
    int64_t pts;
    unsigned int flags;

    /* raw, 44.1 kHz stereo -> s16 stereo */
    memset( &cfg, 0, sizeof(cfg) );
    cfg.asc = asc_lc44; cfg.asc_size = 2; cfg.in_rate = 44100; cfg.in_channels = 2;
    cfg.out_float = 0; cfg.out_rate = 44100; cfg.out_channels = 2;
    CHECK( !mta_create( &cfg, &a ), "create" );
    memset( unit, 160, sizeof(unit) );
    {
        struct mtx_in in = { unit, 200, MTX_FLAG_HAS_PTS, 5000000, 0 };
        CHECK( mta_push( a, &in ) == MTX_OK, "push" );
        CHECK( !mta_accepts_input( a ), "accepting while PCM is pending" );
        CHECK( mta_push( a, &in ) == MTX_NOT_ACCEPTING, "a second unit was accepted while PCM is pending" );
    }
    flags = 0;
    got = read_pcm( a, pcm, sizeof(pcm), &pts, &flags );
    CHECK( got == 1024 * 4, "s16 stereo: %u bytes", got );
    CHECK( pts == 5000000 && (flags & MTX_FLAG_HAS_DURATION), "pts %lld flags %#x", (long long)pts, flags );
    CHECK( as_last_asc_size == 2 && as_last_asc[0] == 0x12 && as_last_asc[1] == 0x10 && as_last_size == 200, "decoder input" );
    {
        int16_t *s = (int16_t *)pcm;
        float expect = (160 - 128) / 4096.0f;   /* sample 0 of the stub's channel 0 */
        CHECK( fabsf( s[0] / 32768.0f - expect ) < 0.002f, "s16 value %d", s[0] );
    }
    /* the next unit continues the timeline without a timestamp of its own */
    {
        struct mtx_in in = { unit, 200, 0, 0, 0 };
        CHECK( mta_push( a, &in ) == MTX_OK, "push 2" );
        got = read_pcm( a, pcm, sizeof(pcm), &pts, NULL );
        CHECK( got == 4096 && pts == 5000000 + 1024 * 10000000LL / 44100, "continued pts %lld", (long long)pts );
    }
    /* a decode error is concealed with silence of a unit's length */
    as_fail_next = 1;
    {
        struct mtx_in in = { unit, 200, 0, 0, 0 };
        CHECK( mta_push( a, &in ) == MTX_OK, "push 3" );
        got = read_pcm( a, pcm, sizeof(pcm), NULL, NULL );
        CHECK( got == 4096 && !pcm[0] && !pcm[4095], "concealment %u bytes", got );
    }
    /* small reads: whole frames, INCOMPLETE until the end */
    {
        struct mtx_in in = { unit, 200, MTX_FLAG_HAS_PTS, 0, 0 };
        struct mtx_out out;
        CHECK( mta_push( a, &in ) == MTX_OK, "push 4" );
        memset( &out, 0, sizeof(out) );
        out.data = pcm; out.max_size = 1001;
        CHECK( mta_read( a, &out ) == MTX_OK && out.size == 1000 && (out.flags & MTX_FLAG_INCOMPLETE), "partial read %u %#x", out.size, out.flags );
        read_pcm( a, pcm, sizeof(pcm), NULL, NULL );
    }
    /* flush: discontinuity, next timestamp taken from the input */
    mta_flush( a );
    {
        struct mtx_in in = { unit, 200, MTX_FLAG_HAS_PTS, 90000000, 0 };
        CHECK( mta_push( a, &in ) == MTX_OK, "push 5" );
        flags = 0;
        read_pcm( a, pcm, sizeof(pcm), &pts, &flags );
        CHECK( pts == 90000000 && (flags & MTX_FLAG_DISCONTINUITY), "after flush pts %lld flags %#x", (long long)pts, flags );
    }
    /* switch to float mid-stream */
    CHECK( !mta_set_output( a, 1, 44100, 2, 3 ), "set_output float" );
    {
        struct mtx_in in = { unit, 200, 0, 0, 0 };
        CHECK( mta_push( a, &in ) == MTX_OK, "push 6" );
        got = read_pcm( a, pcm, sizeof(pcm), NULL, NULL );
        CHECK( got == 1024 * 8 && fabsf( ((float *)pcm)[0] - (160 - 128) / 4096.0f ) < 1e-4f, "float %u bytes %f", got, ((float *)pcm)[0] );
    }
    mta_destroy( a );

    /* 5.1 -> stereo float */
    memset( &cfg, 0, sizeof(cfg) );
    cfg.asc = asc_51; cfg.asc_size = 2; cfg.in_rate = 48000; cfg.in_channels = 6;
    cfg.out_float = 1; cfg.out_rate = 48000; cfg.out_channels = 2;
    CHECK( !mta_create( &cfg, &a ), "create 5.1" );
    {
        struct mtx_in in = { unit, 100, MTX_FLAG_HAS_PTS, 0, 0 };
        float peak = 0;
        CHECK( mta_push( a, &in ) == MTX_OK, "push 5.1" );
        got = read_pcm( a, pcm, sizeof(pcm), NULL, NULL );
        CHECK( got == 1024 * 8, "5.1 -> stereo: %u bytes", got );
        for (i = 0; i < got / 4; i++) if (fabsf( ((float *)pcm)[i] ) > peak) peak = fabsf( ((float *)pcm)[i] );
        CHECK( peak > 0.05f && peak <= 1.5f, "5.1 downmix peak %f", peak );
    }
    mta_destroy( a );

    /* 24 kHz mono -> 48 kHz stereo s16: resampled */
    memset( &cfg, 0, sizeof(cfg) );
    cfg.asc = asc_24m; cfg.asc_size = 2; cfg.in_rate = 24000; cfg.in_channels = 1;
    cfg.out_float = 0; cfg.out_rate = 48000; cfg.out_channels = 2;
    CHECK( !mta_create( &cfg, &a ), "create 24k" );
    {
        uint32_t total = 0;
        for (i = 0; i < 20; i++)
        {
            struct mtx_in in = { unit, 100, i ? 0 : MTX_FLAG_HAS_PTS, 0, 0 };
            CHECK( mta_push( a, &in ) == MTX_OK, "push 24k %u", i );
            total += read_pcm( a, pcm, sizeof(pcm), NULL, NULL );
        }
        mta_drain( a );
        total += read_pcm( a, pcm, sizeof(pcm), NULL, NULL );
        CHECK( total / 4 > 20 * 2048 - 64 && total / 4 <= 20 * 2048 + 64, "24k -> 48k: %u frames for %u", total / 4, 20 * 2048 );
    }
    mta_destroy( a );

    /* ADTS: three frames in one sample, the configuration from the header */
    memset( &cfg, 0, sizeof(cfg) );
    cfg.adts = 1; cfg.in_rate = 44100; cfg.in_channels = 2; cfg.out_rate = 44100; cfg.out_channels = 2;
    CHECK( !mta_create( &cfg, &a ), "create adts" );
    {
        uint8_t adts[3 * (7 + 50)];
        uint32_t o = 0, k, before = as_units;
        for (k = 0; k < 3; k++)
        {
            uint32_t len = 7 + 50;
            adts[o + 0] = 0xff; adts[o + 1] = 0xf1;                   /* MPEG-4, no CRC */
            adts[o + 2] = (1 << 6) | (4 << 2) | 0;                     /* LC, 44100, channel config high bit */
            adts[o + 3] = (2 << 6) | (len >> 11);
            adts[o + 4] = (len >> 3) & 0xff;
            adts[o + 5] = ((len & 7) << 5) | 0x1f;
            adts[o + 6] = 0xfc;
            memset( adts + o + 7, 140, 50 );
            o += len;
        }
        {
            struct mtx_in in = { adts, o, MTX_FLAG_HAS_PTS, 0, 0 };
            CHECK( mta_push( a, &in ) == MTX_OK, "push adts" );
        }
        CHECK( as_units - before == 3 && as_last_size == 50, "adts: %u units, last %u bytes", as_units - before, as_last_size );
        CHECK( as_last_asc_size == 2 && as_last_asc[0] == 0x12 && as_last_asc[1] == 0x10, "adts config %02x %02x", as_last_asc[0], as_last_asc[1] );
        got = read_pcm( a, pcm, sizeof(pcm), NULL, NULL );
        CHECK( got == 3 * 1024 * 4, "adts pcm %u", got );
    }
    mta_destroy( a );

    /* the AAC decoder's support check: 6 ch in, no config, float stereo out */
    memset( &cfg, 0, sizeof(cfg) );
    cfg.in_rate = 48000; cfg.in_channels = 6; cfg.out_float = 1; cfg.out_rate = 48000; cfg.out_channels = 2;
    CHECK( !mta_create( &cfg, &a ), "support check create" );
    mta_destroy( a );
    /* no configuration given: one built from the rate and channel count (AAC-LC) */
    memset( &cfg, 0, sizeof(cfg) );
    cfg.in_rate = 44100; cfg.in_channels = 1; cfg.out_rate = 44100; cfg.out_channels = 1;
    CHECK( !mta_create( &cfg, &a ), "no-config create" );
    {
        struct mtx_in in = { unit, 100, 0, 0, 0 };
        CHECK( mta_push( a, &in ) == MTX_OK, "push no-config" );
        CHECK( as_last_asc_size == 2 && as_last_asc[0] == 0x12 && as_last_asc[1] == 0x08, "built config %02x %02x", as_last_asc[0], as_last_asc[1] );
        read_pcm( a, pcm, sizeof(pcm), NULL, NULL );
    }
    mta_destroy( a );
    /* an odd rate: the explicit 24-bit frequency */
    {
        uint8_t asc[8];
        uint32_t n = mta_default_asc( 37800, 2, asc ), r = ((asc[1] & 0x7f) << 17) | (asc[2] << 9) | (asc[3] << 1) | (asc[4] >> 7);
        CHECK( n == 5 && (asc[0] >> 3) == 2 && (((asc[0] & 7) << 1) | (asc[1] >> 7)) == 15 && r == 37800 && ((asc[4] >> 3) & 0xf) == 2,
               "explicit-rate config" );
    }
    printf( "aac: done\n" );
}

int main( int argc, char **argv )
{
    int i;
    mtx_configure( &stub_video, &stub_audio );
    test_sps();
    test_synthetic();
    test_protocol();
    for (i = 1; i + 4 < argc; i += 5)
    {
        static const enum mtx_pix pixes[4] = { MTX_PIX_NV12, MTX_PIX_I420, MTX_PIX_YV12, MTX_PIX_YUY2 };
        test_file( argv[i], argv[i + 1], pixes[atoi( argv[i + 2] ) & 3], atoi( argv[i + 3] ), atoi( argv[i + 4] ) );
    }
    test_aac();
    CHECK( vs_live == 0, "%u pictures leaked", vs_live );
    printf( "%s: %d failure(s)\n", failures ? "FAIL" : "PASS", failures );
    return failures ? 1 : 0;
}
'''

def compile_and_run(src_text, exe_name, extra_flags, args, tmp, env=None):
    src = tmp / (exe_name + ".c")
    src.write_text(src_text)
    exe = tmp / exe_name
    cc = os.environ.get("CC", "cc")
    cmd = [cc, "-std=gnu11", "-O1", "-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=undefined",
           "-fno-strict-aliasing"] + extra_flags + [str(src), "-o", str(exe)] + ff_libs
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout[-6000:], r.stderr[-6000:])
        sys.exit("FAIL: %s did not compile" % exe_name)
    run_env = dict(os.environ, ASAN_OPTIONS=os.environ.get("ASAN_OPTIONS", "detect_leaks=1"), UBSAN_OPTIONS="print_stacktrace=1")
    if env:
        run_env.update(env)
    out = subprocess.run([str(exe)] + args, capture_output=True, text=True, env=run_env)
    lines = [l for l in out.stdout.splitlines()]
    for l in lines:
        if l.startswith("FAIL") or l.startswith("PASS") or ": done" in l or "access units" in l or "cases" in l:
            print("  " + l)
    if out.returncode:
        err = [l for l in out.stderr.splitlines() if not l.startswith("[wg-")]
        print("\n".join(err[-80:]))
        sys.exit("FAIL: %s exited %d" % (exe_name, out.returncode))
    return out

with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(os.environ.get("MADEIRA_TEST_TMP") or tmpdir)
    tmp.mkdir(parents=True, exist_ok=True)
    args = []
    if shutil.which("ffmpeg") and shutil.which("ffprobe") and \
       "libx264" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout:
        high, n1 = make_stream(tmp, "high", "320x180", "high", ["-bf", "3", "-g", "12", "-x264-params", "aud=1:scenecut=0"])
        base, n2 = make_stream(tmp, "baseline", "330x186", "baseline", ["-g", "15", "-x264-params", "aud=1:scenecut=0"])
        args = [str(high), "x264 high 320x180 B-frames nv12", "0", "320", "180",
                str(high), "x264 high 320x180 B-frames yuy2", "3", "320", "180",
                str(base), "x264 baseline 330x186 i420", "1", "330", "186",
                str(base), "x264 baseline 330x186 yv12", "2", "330", "186"]
    else:
        print("SKIP: no ffmpeg with libx264; real-stream cases not run")
    print("core harness:")
    compile_and_run(harness, "wgt_core", ["-Wall", "-Wno-unused-function", "-I" + str(ntdll_unix), "-I" + str(inc)], args, tmp)

    # ------------------------------------------------------------------ 3. the production glue
    wine_build = Path(os.environ.get("MADEIRA_WINE_BUILD") or wine / "build-macos")
    if not (wine_build / "include/config.h").exists() or not (wine_build / "include/mfobjects.h").exists():
        print("SKIP: no configured Wine tree (MADEIRA_WINE_BUILD); the glue cases are not run")
    else:
        stubs = harness[harness.index("/* ============================================================== bit writer for SPS / PPS */"):
                        harness.index("static void test_sps(void)")]
        stubs += harness[harness.index("/* ============================================================== stub video backend */"):
                         harness.index("/* ============================================================== the expected MF layout")]
        astubs = harness[harness.index("/* ============================================================== stub audio backend */"):
                         harness.index("static uint32_t read_pcm(")]
        glue_harness = r'''
#include "winegstreamer_unixlib_ios.c"
#include <math.h>

const struct mav_video_backend mav_apple_video_backend;
const struct mav_audio_backend mav_apple_audio_backend;

static int failures;
#define CHECK(c, ...) do { if (!(c)) { printf( "FAIL %s:%d: ", __func__, __LINE__ ); printf( __VA_ARGS__ ); printf( "\n" ); fflush( stdout ); failures++; } } while (0)

#include <sys/mman.h>
/* A 32-bit "guest" window for the wow64 entry: off iOS, unixlib.h's ios_wow_host_ptr() is the
 * identity, so the 32-bit pointers in the argument block must be real low addresses. */
static unsigned char *guest;
''' + stubs + astubs + r'''
static const GUID sub_h264 = { 0x34363248, 0x0000, 0x0010, { 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };
static const GUID sub_nv12 = { 0x3231564e, 0x0000, 0x0010, { 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };
static const GUID sub_iyuv = { 0x56555949, 0x0000, 0x0010, { 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };
static const GUID mt_video = { 0x73646976, 0x0000, 0x0010, { 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };
static const GUID mt_audio = { 0x73647561, 0x0000, 0x0010, { 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };

/* SPS 320x180 (cropped from 192), PPS, IDR slice */
static void make_video_types( MFVIDEOFORMAT *in, MFVIDEOFORMAT *out, const GUID *out_sub )
{
    memset( in, 0, sizeof(*in) );
    in->dwSize = sizeof(*in);
    in->guidFormat = sub_h264;
    in->videoInfo.dwWidth = 320;
    in->videoInfo.dwHeight = 180;
    in->videoInfo.FramesPerSecond.Numerator = 30;
    in->videoInfo.FramesPerSecond.Denominator = 1;
    memset( out, 0, sizeof(*out) );
    out->dwSize = sizeof(*out);
    out->guidFormat = *out_sub;
    out->videoInfo.dwWidth = 320;
    out->videoInfo.dwHeight = 180;
}

static UINT32 build_au( BYTE *au, int idr, UINT32 index )
{
    /* SPS for 320x180 Baseline (20x12 macroblocks, 6 rows cropped), PPS, then a slice the stub
     * recognises; the core parses the SPS, the stub only needs the slice */
    static const struct sps_desc d = { 66, 0xc0, 30, 0, 0, 0, 2, 20, 12, 1, 0, 0, 0, 6, 0 };
    BYTE sps[64], pps[16];
    UINT32 o = 0, sn = make_sps( &d, sps ), pn = make_pps( 0, 0, pps );
    if (idr)
    {
        au[o++] = 0; au[o++] = 0; au[o++] = 0; au[o++] = 1; memcpy( au + o, sps, sn ); o += sn;
        au[o++] = 0; au[o++] = 0; au[o++] = 0; au[o++] = 1; memcpy( au + o, pps, pn ); o += pn;
    }
    au[o++] = 0; au[o++] = 0; au[o++] = 1;
    au[o++] = idr ? 0x65 : 0x41; au[o++] = 0xab; au[o++] = index >> 8; au[o++] = index & 0xff; au[o++] = 0; au[o++] = 0x80;
    return o;
}

static void test_glue_video(void)
{
    MFVIDEOFORMAT in, out, got;
    struct wg_transform_create_params create = {0};
    struct wg_transform_get_output_type_params gtype = {0};
    struct wg_transform_set_output_type_params stype = {0};
    struct wg_transform_get_status_params status = {0};
    static BYTE frame[1 << 20];
    BYTE au[128];
    UINT32 i, frames = 0;
    NTSTATUS st;

    make_video_types( &in, &out, &sub_nv12 );
    /* Electra may negotiate the elementary-stream subtype instead of H264. */
    in.guidFormat = (GUID){0x3f40f4f0,0x5622,0x4ff8,{0xb6,0xd8,0xa1,0x7a,0x58,0x4b,0xee,0x5e}};
    create.input_type.major = mt_video; create.input_type.format_size = sizeof(in); create.input_type.u.video = &in;
    create.output_type.major = mt_video; create.output_type.format_size = sizeof(out); create.output_type.u.video = &out;
    create.attrs.output_plane_align = 15; create.attrs.input_queue_length = 15;
    create.attrs.allow_format_change = TRUE; create.attrs.preserve_timestamps = TRUE;
    st = wma_transform_create( &create );
    CHECK( !st && create.transform, "create: %#x", (UINT)st );
    if (st) return;
    for (i = 0; i < 6; i++)
    {
        struct wg_sample sample = {0};
        struct wg_transform_push_data_params push = {0};
        UINT32 n = build_au( au, !i, i );
        sample.data = (UINT64)(UINT_PTR)au; sample.size = n; sample.max_size = n;
        sample.flags = WG_SAMPLE_FLAG_HAS_PTS | (i ? 0 : WG_SAMPLE_FLAG_SYNC_POINT); sample.pts = i * 333333;
        push.transform = create.transform; push.sample = &sample;
        CHECK( !wma_transform_push_data( &push ) && push.result == S_OK, "push %u: %#x", i, (UINT)push.result );
    }
    status.transform = create.transform;
    CHECK( !wma_transform_get_status( &status ) && status.accepts_input, "get_status" );
    CHECK( !wma_transform_drain( &create.transform ), "drain" );
    for (;;)
    {
        struct wg_sample sample = {0};
        struct wg_transform_read_data_params read = {0};
        sample.data = (UINT64)(UINT_PTR)frame; sample.max_size = sizeof(frame);
        read.transform = create.transform; read.sample = &sample;
        st = wma_transform_read_data( &read );
        CHECK( !st, "read status %#x", (UINT)st );
        if (st || read.result == MF_E_TRANSFORM_NEED_MORE_INPUT) break;
        if (read.result == MF_E_TRANSFORM_STREAM_CHANGE)
        {
            /* two-phase, as main.c's wg_transform_get_output_type() */
            gtype.transform = create.transform;
            gtype.media_type.u.format = NULL; gtype.media_type.format_size = 0;
            CHECK( wma_transform_get_output_type( &gtype ) == STATUS_BUFFER_TOO_SMALL && gtype.media_type.format_size == sizeof(MFVIDEOFORMAT),
                   "get_output_type size phase" );
            gtype.media_type.u.video = &got;
            CHECK( !wma_transform_get_output_type( &gtype ), "get_output_type" );
            CHECK( IsEqualGUID( &gtype.media_type.major, &mt_video ) && got.dwSize == sizeof(got) && IsEqualGUID( &got.guidFormat, &sub_nv12 ),
                   "output type header" );
            CHECK( got.videoInfo.dwWidth == 320 && got.videoInfo.dwHeight == 192, "output frame %ux%u", (UINT)got.videoInfo.dwWidth, (UINT)got.videoInfo.dwHeight );
            CHECK( got.videoInfo.MinimumDisplayAperture.Area.cx == 320 && got.videoInfo.MinimumDisplayAperture.Area.cy == 180,
                   "aperture %dx%d", (int)got.videoInfo.MinimumDisplayAperture.Area.cx, (int)got.videoInfo.MinimumDisplayAperture.Area.cy );
            CHECK( got.videoInfo.FramesPerSecond.Numerator == 30 && got.videoInfo.FramesPerSecond.Denominator == 1, "fps" );
            /* the caller sets IYUV with what it was offered */
            got.guidFormat = sub_iyuv;
            stype.transform = create.transform;
            stype.media_type.major = mt_video; stype.media_type.format_size = sizeof(got); stype.media_type.u.video = &got;
            CHECK( !wma_transform_set_output_type( &stype ), "set_output_type" );
            continue;
        }
        CHECK( read.result == S_OK, "read result %#x", (UINT)read.result );
        CHECK( sample.size == 320 * 192 * 3 / 2, "frame size %u", sample.size );
        CHECK( (sample.flags & (WG_SAMPLE_FLAG_HAS_PTS | WG_SAMPLE_FLAG_PRESERVE_TIMESTAMPS)) == (WG_SAMPLE_FLAG_HAS_PTS | WG_SAMPLE_FLAG_PRESERVE_TIMESTAMPS)
               && sample.pts == (INT64)frames * 333333, "flags %#x pts %lld", sample.flags, (long long)sample.pts );
        frames++;
    }
    CHECK( frames == 6, "%u frames", frames );
    gtype.media_type.u.video = &got;
    gtype.media_type.format_size = sizeof(got);
    CHECK( !wma_transform_get_output_type( &gtype ) && IsEqualGUID( &got.guidFormat, &sub_iyuv ), "subtype kept as the caller named it" );
    CHECK( !wma_transform_flush( &create.transform ), "flush" );
    CHECK( !wma_transform_destroy( &create.transform ), "destroy" );
    CHECK( vs_live == 0, "%u pictures leaked", vs_live );

    /* the 32-bit entry: refused unless MADEIRA_WG_H264_AAC=1 */
    {
        struct wg_transform_create_params32 *c32 = (void *)guest;
        MFVIDEOFORMAT *in32 = (void *)(guest + 0x100), *out32 = (void *)(guest + 0x400);
        int pass;
        for (pass = 0; pass < 2; pass++)
        {
            if (pass) setenv( "MADEIRA_WG_H264_AAC", "1", 1 );
            make_video_types( in32, out32, &sub_nv12 );
            memset( c32, 0, sizeof(*c32) );
            c32->input_type.major = mt_video; c32->input_type.format_size = sizeof(*in32); c32->input_type.format = 0x20000100;
            c32->output_type.major = mt_video; c32->output_type.format_size = sizeof(*out32); c32->output_type.format = 0x20000400;
            c32->attrs.output_plane_align = 15;
            st = wow64_wma_transform_create( c32 );
            if (!pass) CHECK( st == STATUS_NOT_SUPPORTED && !c32->transform, "32-bit H.264 created by default (%#x)", (UINT)st );
            else
            {
                CHECK( !st && c32->transform, "32-bit H.264 with MADEIRA_WG_H264_AAC=1: %#x", (UINT)st );
                if (c32->transform) wma_transform_destroy( &c32->transform );
            }
        }
        setenv( "MADEIRA_WG_H264_AAC", "0", 1 );
        create.transform = 0;
        CHECK( wma_transform_create( &create ) == STATUS_NOT_SUPPORTED, "64-bit H.264 with MADEIRA_WG_H264_AAC=0" );
        unsetenv( "MADEIRA_WG_H264_AAC" );
    }
    printf( "glue video: done\n" );
}

static void test_glue_audio(void)
{
    struct { WAVEFORMATEX wfx; BYTE extra[64]; } in;
    WAVEFORMATEXTENSIBLE out, got;
    struct wg_transform_create_params create = {0};
    struct wg_transform_get_output_type_params gtype = {0};
    static BYTE pcm[1 << 16];
    BYTE unit[64];
    NTSTATUS st;

    memset( &in, 0, sizeof(in) );
    in.wfx.wFormatTag = WAVE_FORMAT_MPEG_HEAAC; in.wfx.nChannels = 2; in.wfx.nSamplesPerSec = 44100;
    in.wfx.cbSize = 12 + 2;
    in.extra[12] = 0x12; in.extra[13] = 0x10;       /* HEAACWAVEINFO tail (payload 0), then the config */
    memset( &out, 0, sizeof(out) );
    out.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE; out.Format.nChannels = 2; out.Format.nSamplesPerSec = 44100;
    out.Format.wBitsPerSample = 16; out.Format.nBlockAlign = 4; out.Format.nAvgBytesPerSec = 44100 * 4;
    out.Format.cbSize = sizeof(out) - sizeof(WAVEFORMATEX);
    out.Samples.wValidBitsPerSample = 16; out.dwChannelMask = 3;
    out.SubFormat = (GUID){ WAVE_FORMAT_PCM, 0x0000, 0x0010, { 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };
    create.input_type.major = mt_audio; create.input_type.format_size = sizeof(WAVEFORMATEX) + in.wfx.cbSize; create.input_type.u.audio = &in.wfx;
    create.output_type.major = mt_audio; create.output_type.format_size = sizeof(out); create.output_type.u.audio = &out.Format;
    st = wma_transform_create( &create );
    CHECK( !st && create.transform, "aac create %#x", (UINT)st );
    if (st) return;
    memset( unit, 150, sizeof(unit) );
    {
        struct wg_sample sample = {0};
        struct wg_transform_push_data_params push = {0};
        sample.data = (UINT64)(UINT_PTR)unit; sample.size = 40; sample.flags = WG_SAMPLE_FLAG_HAS_PTS; sample.pts = 777;
        push.transform = create.transform; push.sample = &sample;
        CHECK( !wma_transform_push_data( &push ) && push.result == S_OK, "aac push" );
        CHECK( as_last_asc_size == 2 && as_last_asc[0] == 0x12 && as_last_asc[1] == 0x10, "aac config from HEAACWAVEINFO" );
    }
    {
        struct wg_sample sample = {0};
        struct wg_transform_read_data_params read = {0};
        sample.data = (UINT64)(UINT_PTR)pcm; sample.max_size = sizeof(pcm);
        read.transform = create.transform; read.sample = &sample;
        CHECK( !wma_transform_read_data( &read ) && read.result == S_OK && sample.size == 4096 && sample.pts == 777, "aac read %u", sample.size );
    }
    gtype.transform = create.transform;
    CHECK( wma_transform_get_output_type( &gtype ) == STATUS_BUFFER_TOO_SMALL && gtype.media_type.format_size == sizeof(out), "aac type size" );
    gtype.media_type.u.audio = &got.Format;
    CHECK( !wma_transform_get_output_type( &gtype ) && !memcmp( &got, &out, sizeof(out) ), "aac output type verbatim" );
    CHECK( !wma_transform_destroy( &create.transform ), "aac destroy" );

    /* aac_decoder_create()'s support check: HEAAC 6 ch, payload 0, no config -> float stereo 48 kHz */
    memset( &in, 0, sizeof(in) );
    in.wfx.wFormatTag = WAVE_FORMAT_MPEG_HEAAC; in.wfx.nChannels = 6; in.wfx.nSamplesPerSec = 48000;
    in.wfx.nAvgBytesPerSec = 1152000; in.wfx.nBlockAlign = 24; in.wfx.wBitsPerSample = 32; in.wfx.cbSize = 12;
    memset( &out, 0, sizeof(out) );
    out.Format.wFormatTag = WAVE_FORMAT_IEEE_FLOAT; out.Format.wBitsPerSample = 32; out.Format.nSamplesPerSec = 48000;
    out.Format.nChannels = 2; out.Format.cbSize = sizeof(out) - sizeof(WAVEFORMATEX);
    create.transform = 0;
    create.input_type.format_size = sizeof(WAVEFORMATEX) + 12;
    st = wma_transform_create( &create );
    CHECK( !st && create.transform, "support-check create %#x", (UINT)st );
    if (create.transform) wma_transform_destroy( &create.transform );
    /* LOAS is refused */
    in.extra[0] = 3;
    create.transform = 0;
    CHECK( wma_transform_create( &create ) == STATUS_NOT_SUPPORTED, "LOAS accepted" );
    printf( "glue audio: done\n" );
}

int main(void)
{
    guest = mmap( (void *)0x20000000, 1 << 16, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0 );
    if (guest != (void *)0x20000000) { printf( "FAIL: no low mapping for the wow64 case\n" ); return 1; }
    pthread_once( &av_backend_once, av_configure_backends );
    mtx_configure( &stub_video, &stub_audio );
    test_glue_video();
    test_glue_audio();
    printf( "%s: %d failure(s)\n", failures ? "FAIL" : "PASS", failures );
    return failures ? 1 : 0;
}
'''
        print("glue harness:")
        compile_and_run(glue_harness, "wgt_glue",
                        ["-Wno-int-conversion", "-Wno-implicit-function-declaration",
                         "-include", str(wine_build / "include/config.h"),
                         "-I" + str(ntdll_unix), "-I" + str(wine / "dlls/winegstreamer"),
                         "-I" + str(wine_build / "include"), "-I" + str(wine / "include"), "-I" + str(inc),
                         "-D__WINESRC__", "-D_NTSYSTEM_", "-D_ACRTIMP=", "-DWINBASEAPI=", "-DWINE_UNIX_LIB"],
                        [], tmp)
print("PASS")
