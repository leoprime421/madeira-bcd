/* H.264 and AAC decoder transforms for winegstreamer's wg_transform on iOS,
 * decoded by VideoToolbox / AudioToolbox through wg_parser_backend_ios.h.
 *                                                        (madeira-bcd, 2026-10-08)
 *
 * WHY THIS EXISTS
 * ---------------
 * Mortal Shell 2 (Unreal Engine 5, 64-bit) finishes compiling its shaders and
 * then shows a black screen while its music plays (device logs 2026-10-08
 * 12:55 / 13:12 / 13:30, IPA build 100).  The engine renders the whole time;
 * A media texture without decoded frames is a candidate cause. Device
 * validation is needed to confirm whether this fixes the black screen.
 * Unreal's Electra player checks for Windows' decoders at startup:
 *
 *   err:module:load_dll [dll-missing] #6 L"msmpeg2vdec.dll" status=c0000135
 *
 * and on Windows it decodes with the H.264 decoder MFT
 * (CLSID_CMSH264DecoderMFT, msmpeg2vdec.dll) and the AAC decoder MFT
 * (CLSID_CMSAACDecMFT, msauddecmft.dll).  In Wine both forward to
 * winegstreamer.dll (CLSID_wg_h264_decoder / CLSID_wg_aac_decoder), whose
 * decoders are dlls/winegstreamer/video_decoder.c and aac_decoder.c on top of
 * a wg_transform -- a GStreamer pipeline upstream.  This port's wg_transform
 * (winegstreamer_unixlib_ios.c) handled the WMA family only and refused H.264
 * and AAC at create, so the decoders could not even be instantiated.
 *
 * This file is the H.264 and AAC wg_transform: the platform decoders the
 * MP4 parser already uses (wg_parser_apple_ios.c) behind the transform
 * protocol that video_decoder.c / aac_decoder.c drive.  Like
 * wg_parser_av_ios.c it includes no Wine header: winegstreamer_unixlib_ios.c
 * #includes it and maps the Wine structures, and tests/host/check-wg-transform.py
 * compiles it on its own with stub backends.
 *
 * THE PROTOCOL (dlls/winegstreamer/wg_transform.c, mirrored)
 * ----------------------------------------------------------
 *  - push_data queues one compressed sample.  MF_E_NOTACCEPTING once
 *    input_queue_length + 1 samples are queued, or while a drain is pending.
 *  - read_data decodes queued input until a picture is ready.
 *    MF_E_TRANSFORM_NEED_MORE_INPUT when none can be.  The first picture after
 *    create, and the first one whose size differs from the current output
 *    caps, is NOT consumed: the call returns MF_E_TRANSFORM_STREAM_CHANGE
 *    (only with attrs.allow_format_change, which the H.264 decoder sets) and
 *    the caller renegotiates through get_output_type / set_output_type.  That
 *    is what GStreamer does on every caps event, and what Windows' decoder
 *    does on the first output.
 *  - pictures leave in PRESENTATION order.  VideoToolbox returns them in
 *    decode order; they are held in a reorder window sized from the SPS
 *    (VUI max_num_reorder_frames, else the level's DPB size, 0 for streams
 *    that cannot reorder) and sorted by the timestamps the caller gave.
 *  - the output frame layout is GStreamer's for the negotiated format, with
 *    the planes padded to attrs.output_plane_align + 1 (16 for H.264) as
 *    align_video_info_planes() does, and the 2D-buffer stride honoured.  The
 *    output type reports the padded size with the picture as its minimum
 *    display aperture, so 1920x1080 is a 1920x1088 frame cropped to 1080.
 *  - drain emits everything held; flush drops everything and waits for the
 *    next IDR (a seek).
 *
 * The input is an H.264 Annex B byte stream (start codes), as the Windows
 * decoder takes it; parameter sets arrive in-band (Electra puts them in front
 * of every keyframe) or as codec data after the MFVIDEOFORMAT.  They are
 * turned into the avcC + length-prefixed packets VideoToolbox wants here, and
 * a changed SPS/PPS reopens the decoder.
 *
 * AAC: raw access units (with the AudioSpecificConfig from the media type,
 * or one built from its rate and channel count) or ADTS, decoded by
 * AudioToolbox to float and converted with libswresample to the negotiated
 * output (16-bit or float, its rate and channel count -- the AAC decoder's
 * support check asks for 6 channels in and 2 out).
 *
 * LOGGING: "[wg-h264]" / "[wg-aac]" on stderr, a bounded number of lines per
 * process (MADEIRA_DIAG=1 lifts the bound), and a summary line per transform
 * when it is destroyed.
 */

#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <libavutil/channel_layout.h>
#include <libavutil/mem.h>
#include <libavutil/opt.h>
#include <libavutil/samplefmt.h>
#include <libswresample/swresample.h>

#include "wg_parser_backend_ios.h"

/***********************************************************************
 *           API (used by winegstreamer_unixlib_ios.c and the host test)
 */

enum mtx_result
{
    MTX_OK = 0,
    MTX_NEED_MORE_INPUT,    /* MF_E_TRANSFORM_NEED_MORE_INPUT */
    MTX_NOT_ACCEPTING,      /* MF_E_NOTACCEPTING */
    MTX_STREAM_CHANGE,      /* MF_E_TRANSFORM_STREAM_CHANGE */
    MTX_BUFFER_TOO_SMALL,   /* STATUS_BUFFER_TOO_SMALL */
    MTX_INVALID,            /* STATUS_INVALID_PARAMETER */
    MTX_NO_MEMORY,          /* STATUS_NO_MEMORY */
    MTX_UNSUPPORTED,        /* STATUS_NOT_SUPPORTED */
};

/* The raw formats the H.264 transform produces (video_decoder.c's list). */
enum mtx_pix
{
    MTX_PIX_NONE = 0,
    MTX_PIX_NV12,
    MTX_PIX_I420,           /* also IYUV */
    MTX_PIX_YV12,
    MTX_PIX_YUY2,
};

/* The same bits as unixlib.h `enum wg_sample_flag`; the glue asserts it. */
#define MTX_FLAG_INCOMPLETE           0x01
#define MTX_FLAG_HAS_PTS              0x02
#define MTX_FLAG_HAS_DURATION         0x04
#define MTX_FLAG_SYNC_POINT           0x08
#define MTX_FLAG_DISCONTINUITY        0x10
#define MTX_FLAG_PRESERVE_TIMESTAMPS  0x20

/* One compressed input sample.  Times are in 100 ns. */
struct mtx_in
{
    const uint8_t *data;
    uint32_t size;
    uint32_t flags;         /* MTX_FLAG_HAS_PTS / HAS_DURATION / SYNC_POINT / DISCONTINUITY */
    int64_t pts;
    uint64_t duration;
};

/* One output buffer: in, the caller's memory; out, what was written. */
struct mtx_out
{
    uint8_t *data;
    uint32_t max_size;
    uint32_t stride;        /* bytes per row of plane 0 for an MF 2D buffer, else 0 */
    uint32_t size;
    uint32_t flags;
    int64_t pts;
    uint64_t duration;
};

struct mtv_config
{
    /* the input type */
    uint32_t in_width, in_height;       /* 0: unknown */
    uint32_t fps_n, fps_d;              /* fps_d 0: unknown */
    uint32_t par_n, par_d;              /* par_d 0: unknown */
    const uint8_t *header;              /* bytes after the MFVIDEOFORMAT: Annex B or avcC */
    uint32_t header_size;
    /* the output type */
    enum mtx_pix pix;
    uint32_t out_width, out_height;     /* the picture size it asks for (aperture, else frame size) */
    uint32_t dw_width, dw_height;       /* its MFVideoInfo dwWidth / dwHeight */
    uint32_t ap_width, ap_height;       /* its MinimumDisplayAperture, 0 x 0 if empty */
    /* wg_transform_attrs */
    uint32_t plane_align;
    uint32_t input_queue_length;
    int allow_format_change;
    int preserve_timestamps;
    int low_latency;
};

/* What get_output_type reports. */
struct mtv_output_info
{
    enum mtx_pix pix;
    uint32_t width, height;             /* the picture */
    uint32_t frame_width, frame_height; /* the padded frame (dwWidth / dwHeight) */
    uint32_t fps_n, fps_d;              /* fps_d 0: unknown */
    uint32_t par_n, par_d;
};

struct mta_config
{
    int adts;                           /* 1: ADTS frames, 0: raw access units */
    const uint8_t *asc;                 /* AudioSpecificConfig, may be empty */
    uint32_t asc_size;
    uint32_t in_rate, in_channels;
    int out_float;                      /* 1: float32, 0: s16 */
    uint32_t out_rate, out_channels, out_mask;
};

struct mtv;
struct mta;

static void mtx_configure( const struct mav_video_backend *video, const struct mav_audio_backend *audio );

static int mtv_create( const struct mtv_config *cfg, struct mtv **out );
static void mtv_destroy( struct mtv *v );
static int mtv_push( struct mtv *v, const struct mtx_in *in );
static int mtv_read( struct mtv *v, struct mtx_out *out );
static void mtv_get_output( struct mtv *v, struct mtv_output_info *info );
static int mtv_set_output( struct mtv *v, enum mtx_pix pix, uint32_t width, uint32_t height,
                           uint32_t dw_width, uint32_t dw_height, uint32_t ap_width, uint32_t ap_height );
static int mtv_accepts_input( struct mtv *v );
static int mtv_drain( struct mtv *v );
static int mtv_flush( struct mtv *v );

static int mta_create( const struct mta_config *cfg, struct mta **out );
static void mta_destroy( struct mta *a );
static int mta_push( struct mta *a, const struct mtx_in *in );
static int mta_read( struct mta *a, struct mtx_out *out );
static int mta_set_output( struct mta *a, int out_float, uint32_t rate, uint32_t channels, uint32_t mask );
static int mta_accepts_input( struct mta *a );
static int mta_drain( struct mta *a );
static int mta_flush( struct mta *a );

/***********************************************************************
 *           common
 */
#define MTX_MAX_LOGS 160
#define MTX_REORDER_MAX 16
#define MTX_MAX_DIMENSION 8192
#define MTX_MAX_AU_SIZE (64u << 20)

static const struct mav_video_backend *mtx_vbackend;
static const struct mav_audio_backend *mtx_abackend;

static void mtx_configure( const struct mav_video_backend *video, const struct mav_audio_backend *audio )
{
    mtx_vbackend = video;
    mtx_abackend = audio;
}

static pthread_mutex_t mtx_log_lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned int mtx_log_count;
static int mtx_log_diag = -1;

static void mtx_log( const char *tag, const char *fmt, ... ) __attribute__((format(printf, 2, 3)));
static void mtx_log( const char *tag, const char *fmt, ... )
{
    char line[512];
    unsigned int n;
    va_list args;

    pthread_mutex_lock( &mtx_log_lock );
    if (mtx_log_diag < 0)
    {
        const char *e = getenv( "MADEIRA_DIAG" );
        mtx_log_diag = e && e[0] == '1';
    }
    n = ++mtx_log_count;
    pthread_mutex_unlock( &mtx_log_lock );
    if (n > MTX_MAX_LOGS && !mtx_log_diag)
    {
        if (n == MTX_MAX_LOGS + 1)
            dprintf( 2, "%s further lines suppressed (MADEIRA_DIAG=1 shows them)\n", tag );
        return;
    }
    va_start( args, fmt );
    vsnprintf( line, sizeof(line), fmt, args );
    va_end( args );
    dprintf( 2, "%s %s\n", tag, line );
}

#define MTV_TAG "[wg-h264]"
#define MTA_TAG "[wg-aac]"

static int mtx_reserve( void **buf, size_t *cap, size_t need, size_t elem )
{
    size_t n;
    void *grown;

    if (need <= *cap) return 1;
    n = *cap ? *cap : 8;
    while (n < need) n *= 2;
    if (!(grown = realloc( *buf, n * elem ))) return 0;
    *buf = grown;
    *cap = n;
    return 1;
}

/***********************************************************************
 *           H.264 bitstream: Annex B, RBSP, SPS
 */

/* Finds the NAL units of an Annex B buffer: calls `fn` with each payload
 * (start code and trailing_zero_8bits removed).  Returns the NAL count. */
typedef void (*mtv_nal_fn)( void *ctx, const uint8_t *nal, uint32_t size );

static unsigned int mtv_annexb_walk( const uint8_t *p, uint32_t n, mtv_nal_fn fn, void *ctx )
{
    uint32_t i = 0, start = 0, count = 0;
    int in_nal = 0;

    while (i + 3 <= n)
    {
        if (p[i] == 0 && p[i + 1] == 0 && p[i + 2] == 1)
        {
            if (in_nal)
            {
                uint32_t end = i;
                while (end > start && !p[end - 1]) end--;
                if (end > start) { fn( ctx, p + start, end - start ); count++; }
            }
            i += 3;
            start = i;
            in_nal = 1;
            continue;
        }
        i++;
    }
    if (in_nal)
    {
        uint32_t end = n;
        while (end > start && !p[end - 1]) end--;
        if (end > start) { fn( ctx, p + start, end - start ); count++; }
    }
    return count;
}

static int mtv_is_annexb( const uint8_t *p, uint32_t n )
{
    return (n >= 3 && !p[0] && !p[1] && p[2] == 1) || (n >= 4 && !p[0] && !p[1] && !p[2] && p[3] == 1);
}

/* A buffer of 4-byte big-endian length-prefixed NAL units (AVCC packets), as
 * a fallback for a caller that does not send Annex B: every length must fit
 * exactly. */
static int mtv_is_avcc_packet( const uint8_t *p, uint32_t n )
{
    uint32_t pos = 0;

    if (n < 5) return 0;
    while (pos + 4 <= n)
    {
        uint32_t len = ((uint32_t)p[pos] << 24) | ((uint32_t)p[pos + 1] << 16) | ((uint32_t)p[pos + 2] << 8) | p[pos + 3];
        pos += 4;
        if (!len || len > n - pos || (p[pos] & 0x80)) return 0;
        pos += len;
    }
    return pos == n;
}

static unsigned int mtv_avcc_walk( const uint8_t *p, uint32_t n, mtv_nal_fn fn, void *ctx )
{
    uint32_t pos = 0, count = 0;

    while (pos + 4 <= n)
    {
        uint32_t len = ((uint32_t)p[pos] << 24) | ((uint32_t)p[pos + 1] << 16) | ((uint32_t)p[pos + 2] << 8) | p[pos + 3];
        pos += 4;
        if (!len || len > n - pos) break;
        fn( ctx, p + pos, len );
        count++;
        pos += len;
    }
    return count;
}

/* Bit reader over an RBSP (emulation prevention bytes already removed). */
struct mtv_bits
{
    const uint8_t *p;
    uint32_t size;          /* bytes */
    uint32_t pos;           /* bits */
    int overrun;
};

static uint32_t mtv_u( struct mtv_bits *b, unsigned int count )
{
    uint32_t v = 0;

    while (count--)
    {
        if (b->pos >= b->size * 8)
        {
            b->overrun = 1;
            return 0;
        }
        v = (v << 1) | ((b->p[b->pos >> 3] >> (7 - (b->pos & 7))) & 1);
        b->pos++;
    }
    return v;
}

static uint32_t mtv_ue( struct mtv_bits *b )
{
    unsigned int zeros = 0;
    uint32_t v;

    while (!mtv_u( b, 1 ))
    {
        if (b->overrun || ++zeros > 31)
        {
            b->overrun = 1;
            return 0;
        }
    }
    if (!zeros) return 0;
    v = mtv_u( b, zeros );
    return (uint32_t)(((uint64_t)1 << zeros) - 1 + v);
}

static int32_t mtv_se( struct mtv_bits *b )
{
    uint32_t v = mtv_ue( b );
    return (v & 1) ? (int32_t)((v + 1) / 2) : -(int32_t)(v / 2);
}

/* NAL payload (after the header byte) to RBSP. */
static uint32_t mtv_unescape( const uint8_t *src, uint32_t n, uint8_t *dst, uint32_t cap )
{
    uint32_t i, o = 0, zeros = 0;

    for (i = 0; i < n && o < cap; i++)
    {
        if (zeros >= 2 && src[i] == 3)
        {
            zeros = 0;
            continue;
        }
        dst[o++] = src[i];
        zeros = src[i] ? 0 : zeros + 1;
    }
    return o;
}

struct mtv_sps
{
    uint32_t id;
    uint32_t profile_idc, constraints, level_idc;
    uint32_t chroma_format_idc;
    uint32_t poc_type;
    uint32_t max_num_ref_frames;
    uint32_t width_mbs, height_map_units, frame_mbs_only;
    uint32_t coded_width, coded_height;     /* luma samples */
    uint32_t width, height;                 /* after the frame cropping */
    uint32_t par_n, par_d;                  /* 0/0: not given */
    uint32_t fps_n, fps_d;                  /* 0/0: not given */
    int full_range;                         /* -1: not given */
    int has_reorder;
    uint32_t max_num_reorder_frames, max_dec_frame_buffering;
};

static void mtv_skip_scaling_list( struct mtv_bits *b, unsigned int size )
{
    int32_t last = 8, next = 8;
    unsigned int j;

    for (j = 0; j < size && !b->overrun; j++)
    {
        if (next)
        {
            int32_t delta = mtv_se( b );
            next = (last + delta + 256) % 256;
        }
        last = next ? next : last;
    }
}

static void mtv_skip_hrd( struct mtv_bits *b )
{
    uint32_t cpb_cnt = mtv_ue( b ) + 1, i;

    mtv_u( b, 4 );
    mtv_u( b, 4 );
    for (i = 0; i < cpb_cnt && i < 32 && !b->overrun; i++)
    {
        mtv_ue( b );
        mtv_ue( b );
        mtv_u( b, 1 );
    }
    mtv_u( b, 5 );
    mtv_u( b, 5 );
    mtv_u( b, 5 );
    mtv_u( b, 5 );
}

static const uint32_t mtv_sar_table[17][2] =
{
    { 0, 0 }, { 1, 1 }, { 12, 11 }, { 10, 11 }, { 16, 11 }, { 40, 33 }, { 24, 11 }, { 20, 11 }, { 32, 11 },
    { 80, 33 }, { 18, 11 }, { 15, 11 }, { 64, 33 }, { 160, 99 }, { 4, 3 }, { 3, 2 }, { 2, 1 },
};

/* nal: the whole NAL unit, header byte included.  Returns 1 on success. */
static int mtv_parse_sps( const uint8_t *nal, uint32_t size, struct mtv_sps *s )
{
    uint8_t rbsp[512];
    struct mtv_bits b;
    uint32_t crop_l = 0, crop_r = 0, crop_t = 0, crop_b = 0, sub_w = 2, sub_h = 2, unit_x, unit_y;
    uint32_t sep_colour = 0;

    if (size < 4 || (nal[0] & 0x1f) != 7) return 0;
    memset( s, 0, sizeof(*s) );
    s->full_range = -1;
    b.p = rbsp;
    b.size = mtv_unescape( nal + 1, size - 1, rbsp, sizeof(rbsp) );
    b.pos = 0;
    b.overrun = 0;

    s->profile_idc = mtv_u( &b, 8 );
    s->constraints = mtv_u( &b, 8 );
    s->level_idc = mtv_u( &b, 8 );
    s->id = mtv_ue( &b );
    if (s->id > 31) return 0;
    s->chroma_format_idc = 1;
    switch (s->profile_idc)
    {
    case 100: case 110: case 122: case 244: case 44: case 83: case 86: case 118: case 128:
    case 138: case 139: case 134: case 135:
        s->chroma_format_idc = mtv_ue( &b );
        if (s->chroma_format_idc > 3) return 0;
        if (s->chroma_format_idc == 3) sep_colour = mtv_u( &b, 1 );
        mtv_ue( &b );               /* bit_depth_luma_minus8 */
        mtv_ue( &b );               /* bit_depth_chroma_minus8 */
        mtv_u( &b, 1 );             /* qpprime_y_zero_transform_bypass_flag */
        if (mtv_u( &b, 1 ))         /* seq_scaling_matrix_present_flag */
        {
            unsigned int i, lists = s->chroma_format_idc != 3 ? 8 : 12;
            for (i = 0; i < lists && !b.overrun; i++)
                if (mtv_u( &b, 1 )) mtv_skip_scaling_list( &b, i < 6 ? 16 : 64 );
        }
        break;
    }
    mtv_ue( &b );                   /* log2_max_frame_num_minus4 */
    s->poc_type = mtv_ue( &b );
    if (s->poc_type == 0)
        mtv_ue( &b );               /* log2_max_pic_order_cnt_lsb_minus4 */
    else if (s->poc_type == 1)
    {
        uint32_t i, cycle;
        mtv_u( &b, 1 );
        mtv_se( &b );
        mtv_se( &b );
        cycle = mtv_ue( &b );
        if (cycle > 255) return 0;
        for (i = 0; i < cycle && !b.overrun; i++) mtv_se( &b );
    }
    else if (s->poc_type != 2) return 0;
    s->max_num_ref_frames = mtv_ue( &b );
    mtv_u( &b, 1 );                 /* gaps_in_frame_num_value_allowed_flag */
    s->width_mbs = mtv_ue( &b ) + 1;
    s->height_map_units = mtv_ue( &b ) + 1;
    s->frame_mbs_only = mtv_u( &b, 1 );
    if (!s->frame_mbs_only) mtv_u( &b, 1 );   /* mb_adaptive_frame_field_flag */
    mtv_u( &b, 1 );                 /* direct_8x8_inference_flag */
    if (mtv_u( &b, 1 ))             /* frame_cropping_flag */
    {
        crop_l = mtv_ue( &b );
        crop_r = mtv_ue( &b );
        crop_t = mtv_ue( &b );
        crop_b = mtv_ue( &b );
    }
    if (b.overrun) return 0;
    if (s->width_mbs > MTX_MAX_DIMENSION / 16 || s->height_map_units > MTX_MAX_DIMENSION / 16) return 0;
    s->coded_width = s->width_mbs * 16;
    s->coded_height = (2 - s->frame_mbs_only) * s->height_map_units * 16;

    if (sep_colour || s->chroma_format_idc == 0) sub_w = sub_h = 1;   /* ChromaArrayType 0 */
    else if (s->chroma_format_idc == 2) sub_h = 1;
    else if (s->chroma_format_idc == 3) sub_w = sub_h = 1;
    unit_x = sub_w;
    unit_y = sub_h * (2 - s->frame_mbs_only);
    if ((uint64_t)unit_x * (crop_l + crop_r) >= s->coded_width
        || (uint64_t)unit_y * (crop_t + crop_b) >= s->coded_height)
        return 0;
    s->width = s->coded_width - unit_x * (crop_l + crop_r);
    s->height = s->coded_height - unit_y * (crop_t + crop_b);

    if (mtv_u( &b, 1 ))             /* vui_parameters_present_flag */
    {
        int nal_hrd, vcl_hrd;

        if (mtv_u( &b, 1 ))         /* aspect_ratio_info_present_flag */
        {
            uint32_t idc = mtv_u( &b, 8 );
            if (idc == 255)
            {
                s->par_n = mtv_u( &b, 16 );
                s->par_d = mtv_u( &b, 16 );
            }
            else if (idc < 17)
            {
                s->par_n = mtv_sar_table[idc][0];
                s->par_d = mtv_sar_table[idc][1];
            }
            if (!s->par_n || !s->par_d) s->par_n = s->par_d = 0;
        }
        if (mtv_u( &b, 1 )) mtv_u( &b, 1 );   /* overscan */
        if (mtv_u( &b, 1 ))         /* video_signal_type_present_flag */
        {
            mtv_u( &b, 3 );
            s->full_range = mtv_u( &b, 1 );
            if (mtv_u( &b, 1 ))
            {
                mtv_u( &b, 8 );
                mtv_u( &b, 8 );
                mtv_u( &b, 8 );
            }
        }
        if (mtv_u( &b, 1 ))         /* chroma_loc_info_present_flag */
        {
            mtv_ue( &b );
            mtv_ue( &b );
        }
        if (mtv_u( &b, 1 ))         /* timing_info_present_flag */
        {
            uint32_t units = mtv_u( &b, 32 ), scale = mtv_u( &b, 32 );
            mtv_u( &b, 1 );
            if (units && scale && !b.overrun)
            {
                s->fps_n = scale;
                s->fps_d = units * 2;
                if (!(s->fps_n % 2) && !(s->fps_d % 2)) { s->fps_n /= 2; s->fps_d /= 2; }
            }
        }
        nal_hrd = mtv_u( &b, 1 );
        if (nal_hrd) mtv_skip_hrd( &b );
        vcl_hrd = mtv_u( &b, 1 );
        if (vcl_hrd) mtv_skip_hrd( &b );
        if (nal_hrd || vcl_hrd) mtv_u( &b, 1 );   /* low_delay_hrd_flag */
        mtv_u( &b, 1 );             /* pic_struct_present_flag */
        if (mtv_u( &b, 1 ) && !b.overrun)   /* bitstream_restriction_flag */
        {
            mtv_u( &b, 1 );
            mtv_ue( &b );
            mtv_ue( &b );
            mtv_ue( &b );
            mtv_ue( &b );
            s->max_num_reorder_frames = mtv_ue( &b );
            s->max_dec_frame_buffering = mtv_ue( &b );
            if (!b.overrun) s->has_reorder = 1;
        }
        if (b.overrun)
        {
            /* A truncated or exotic VUI: keep the size, drop what it said. */
            s->par_n = s->par_d = s->fps_n = s->fps_d = 0;
            s->full_range = -1;
            s->has_reorder = 0;
        }
    }
    return 1;
}

/* Table A-1 MaxDpbMbs. */
static uint32_t mtv_max_dpb_mbs( const struct mtv_sps *s )
{
    switch (s->level_idc)
    {
    case 9: return 396;
    case 10: return 396;
    case 11: return (s->constraints & 0x10) && s->profile_idc != 100 && s->profile_idc != 110
                    && s->profile_idc != 122 && s->profile_idc != 244 ? 396 : 900;
    case 12: case 13: case 20: return 2376;
    case 21: return 4752;
    case 22: case 30: return 8100;
    case 31: return 18000;
    case 32: return 20480;
    case 40: case 41: return 32768;
    case 42: return 34816;
    case 50: return 110400;
    case 51: case 52: return 184320;
    default: return 696320;   /* level 6.x, or unknown: the largest */
    }
}

/* How many decoded pictures may precede, in decode order, a picture that is
 * shown before them. */
static uint32_t mtv_reorder_depth( const struct mtv_sps *s )
{
    uint32_t frames, mbs;

    if (s->has_reorder) return s->max_num_reorder_frames < MTX_REORDER_MAX ? s->max_num_reorder_frames : MTX_REORDER_MAX;
    /* POC type 2: output order is decode order.  Baseline (66) has no B
     * slices; constraint_set3 on the intra profiles means intra only. */
    if (s->poc_type == 2 || s->profile_idc == 66) return 0;
    if ((s->constraints & 0x10) && (s->profile_idc == 44 || s->profile_idc == 110 || s->profile_idc == 122
                                    || s->profile_idc == 244))
        return 0;
    mbs = s->width_mbs * ((2 - s->frame_mbs_only) * s->height_map_units);
    frames = mbs ? mtv_max_dpb_mbs( s ) / mbs : MTX_REORDER_MAX;
    if (frames > MTX_REORDER_MAX) frames = MTX_REORDER_MAX;
    if (!frames) frames = 1;
    return frames;
}

/***********************************************************************
 *           output layout (GStreamer's, see the header comment)
 */
#define MTX_ROUND_UP_2(x) (((x) + 1u) & ~1u)
#define MTX_ROUND_UP_4(x) (((x) + 3u) & ~3u)

struct mtx_layout
{
    uint32_t width, height;     /* the picture */
    uint32_t stride[3];
    size_t offset[3];
    uint32_t rows[3];           /* rows of each plane */
    uint32_t planes;
    size_t size;
};

static int mtv_layout( enum mtx_pix pix, uint32_t w, uint32_t h, uint32_t plane_align, uint32_t sample_stride,
                       uint32_t dw_width, uint32_t dw_height, uint32_t ap_width, uint32_t ap_height,
                       struct mtx_layout *l )
{
    uint32_t pad_r = ((plane_align + 1) - (w & plane_align)) & plane_align;
    uint32_t pad_b = ((plane_align + 1) - (h & plane_align)) & plane_align;
    uint32_t pw, ph, s0, mask = plane_align;
    uint64_t size;

    memset( l, 0, sizeof(*l) );
    if (!w || !h || w > MTX_MAX_DIMENSION || h > MTX_MAX_DIMENSION) return 0;
    l->width = w;
    l->height = h;
    if (ap_width && ap_height)
    {
        /* align_video_info_planes(): the output type's frame size beyond its
         * aperture is padding the caller expects. */
        if (dw_width > ap_width && dw_width - ap_width > pad_r) pad_r = dw_width - ap_width;
        if (dw_height > ap_height && dw_height - ap_height > pad_b) pad_b = dw_height - ap_height;
    }
    pw = w + pad_r;
    ph = h + pad_b;
    if (sample_stride)
    {
        uint32_t px = pix == MTX_PIX_YUY2 ? 2 : 1;
        if (sample_stride / px > pw) pw = sample_stride / px;
    }
    if (pw > MTX_MAX_DIMENSION * 2 || ph > MTX_MAX_DIMENSION * 2) return 0;

    switch (pix)
    {
    case MTX_PIX_NV12:
        if (!plane_align && (w & 3) && (w & 3) != 3 && pw == w && ph == h)
        {
            /* NV12's minimum stride alignment is 2, which Windows expects
             * (align_video_info_planes' fix_nv12). */
            s0 = MTX_ROUND_UP_2( w );
        }
        else
        {
            s0 = MTX_ROUND_UP_4( pw );
            if (mask) s0 = (s0 + mask) & ~mask;
        }
        l->planes = 2;
        l->stride[0] = l->stride[1] = s0;
        l->rows[0] = MTX_ROUND_UP_2( ph );
        l->rows[1] = l->rows[0] / 2;
        l->offset[1] = (size_t)s0 * l->rows[0];
        size = (uint64_t)l->offset[1] + (uint64_t)s0 * l->rows[1];
        break;
    case MTX_PIX_I420:
    case MTX_PIX_YV12:
    {
        uint32_t s1, rows;
        s0 = MTX_ROUND_UP_4( pw );
        if (mask) s0 = (s0 + mask) & ~mask;
        s1 = MTX_ROUND_UP_4( MTX_ROUND_UP_2( s0 ) / 2 );
        rows = MTX_ROUND_UP_2( ph );
        l->planes = 3;
        l->stride[0] = s0;
        l->stride[1] = l->stride[2] = s1;
        l->rows[0] = rows;
        l->rows[1] = l->rows[2] = rows / 2;
        /* offset[1] is U and offset[2] is V; YV12 stores V first */
        if (pix == MTX_PIX_I420)
        {
            l->offset[1] = (size_t)s0 * rows;
            l->offset[2] = l->offset[1] + (size_t)s1 * (rows / 2);
            size = (uint64_t)l->offset[2] + (uint64_t)s1 * (rows / 2);
        }
        else
        {
            l->offset[2] = (size_t)s0 * rows;
            l->offset[1] = l->offset[2] + (size_t)s1 * (rows / 2);
            size = (uint64_t)l->offset[1] + (uint64_t)s1 * (rows / 2);
        }
        break;
    }
    case MTX_PIX_YUY2:
        s0 = MTX_ROUND_UP_4( pw * 2 );
        if (mask) s0 = (s0 + mask) & ~mask;
        l->planes = 1;
        l->stride[0] = s0;
        l->rows[0] = ph;
        size = (uint64_t)s0 * ph;
        break;
    default:
        return 0;
    }
    if (size > UINT32_MAX) return 0;
    l->size = (size_t)size;
    return 1;
}

static void mtx_fill( uint8_t *dst, size_t n, const uint8_t *pattern, size_t plen )
{
    size_t i;
    if (plen == 1) memset( dst, pattern[0], n );
    else for (i = 0; i < n; i++) dst[i] = pattern[i % plen];
}

/* One decoded 4:2:0 bi-planar picture into `dst` with layout `l`: the visible
 * part copied, everything else black. */
static void mtv_write_picture( enum mtx_pix pix, const struct mav_vplanes *src, uint8_t *dst,
                               const struct mtx_layout *l )
{
    static const uint8_t black_y = 16, black_c = 128, black_yuy2[4] = { 16, 128, 16, 128 };
    uint32_t cw = src->width < l->width ? src->width : l->width;
    uint32_t ch = src->height < l->height ? src->height : l->height;
    uint32_t ccw = (cw + 1) / 2, cch = (ch + 1) / 2, x, y;

    switch (pix)
    {
    case MTX_PIX_NV12:
        for (y = 0; y < l->rows[0]; y++)
        {
            uint8_t *row = dst + (size_t)l->stride[0] * y;
            if (y < ch)
            {
                memcpy( row, src->y + (size_t)src->y_stride * y, cw );
                memset( row + cw, black_y, l->stride[0] - cw );
            }
            else memset( row, black_y, l->stride[0] );
        }
        for (y = 0; y < l->rows[1]; y++)
        {
            uint8_t *row = dst + l->offset[1] + (size_t)l->stride[1] * y;
            if (y < cch)
            {
                memcpy( row, src->uv + (size_t)src->uv_stride * y, ccw * 2 );
                memset( row + ccw * 2, black_c, l->stride[1] - ccw * 2 );
            }
            else memset( row, black_c, l->stride[1] );
        }
        break;
    case MTX_PIX_I420:
    case MTX_PIX_YV12:
        for (y = 0; y < l->rows[0]; y++)
        {
            uint8_t *row = dst + (size_t)l->stride[0] * y;
            if (y < ch)
            {
                memcpy( row, src->y + (size_t)src->y_stride * y, cw );
                memset( row + cw, black_y, l->stride[0] - cw );
            }
            else memset( row, black_y, l->stride[0] );
        }
        for (y = 0; y < l->rows[1]; y++)
        {
            uint8_t *u = dst + l->offset[1] + (size_t)l->stride[1] * y;
            uint8_t *v = dst + l->offset[2] + (size_t)l->stride[2] * y;
            if (y < cch)
            {
                const uint8_t *uv = src->uv + (size_t)src->uv_stride * y;
                for (x = 0; x < ccw; x++)
                {
                    u[x] = uv[2 * x];
                    v[x] = uv[2 * x + 1];
                }
                memset( u + ccw, black_c, l->stride[1] - ccw );
                memset( v + ccw, black_c, l->stride[2] - ccw );
            }
            else
            {
                memset( u, black_c, l->stride[1] );
                memset( v, black_c, l->stride[2] );
            }
        }
        break;
    case MTX_PIX_YUY2:
        for (y = 0; y < l->rows[0]; y++)
        {
            uint8_t *o = dst + (size_t)l->stride[0] * y;
            if (y < ch)
            {
                const uint8_t *yr = src->y + (size_t)src->y_stride * y;
                const uint8_t *uv = src->uv + (size_t)src->uv_stride * (y / 2);
                for (x = 0; x < cw; x += 2)
                {
                    o[2 * x + 0] = yr[x];
                    o[2 * x + 1] = uv[x];
                    o[2 * x + 2] = x + 1 < cw ? yr[x + 1] : yr[x];
                    o[2 * x + 3] = uv[x + 1];
                }
                x = ((cw + 1) & ~1u) * 2;
                mtx_fill( o + x, l->stride[0] - x, black_yuy2, 4 );
            }
            else mtx_fill( o, l->stride[0], black_yuy2, 4 );
        }
        break;
    default:
        break;
    }
}

/***********************************************************************
 *           the H.264 transform
 */
struct mtv_au
{
    uint8_t *data;
    uint32_t size;
    uint32_t flags;
    int64_t pts;
    uint64_t duration;
};

struct mtv_frame
{
    void *handle;           /* the backend's picture */
    int64_t pts;            /* 100 ns; valid with has_pts */
    int64_t duration;       /* 100 ns; <= 0 unknown */
    uint32_t width, height; /* the picture; 0 x 0 until known */
    int has_pts;
    int discontinuity;
};

struct mtv
{
    pthread_mutex_t lock;
    struct mtv_config cfg;
    uint8_t *header;

    /* parameter sets, as received (header byte included) */
    uint8_t *sps[32], *pps[256];
    uint32_t sps_size[32], pps_size[256];
    int sets_dirty;         /* changed since the decoder was opened */
    struct mtv_sps active;  /* the latest SPS, parsed */
    int have_sps;

    void *dec;              /* the backend decoder */
    int dec_failed;         /* the last open failed with these parameter sets */
    unsigned int open_failures;
    int need_keyframe;
    unsigned int waited;    /* access units dropped while waiting for a keyframe */
    uint32_t reorder_depth;

    /* compressed input, oldest first */
    struct mtv_au *in;
    size_t in_count, in_cap;

    /* decoded pictures in decode order, sorted by pts on insert */
    struct mtv_frame *reorder;
    size_t reorder_count, reorder_cap;
    /* pictures ready to read, in presentation order */
    struct mtv_frame *out;
    size_t out_count, out_cap;

    /* filled by the emit callback during decode */
    uint32_t cur_width, cur_height;
    int pending_discontinuity;

    /* the output caps */
    enum mtx_pix pix;
    uint32_t out_width, out_height;
    uint32_t dw_width, dw_height, ap_width, ap_height;
    int caps_announced;     /* the stream change for the current caps has been reported */

    int64_t last_pts;       /* of the last picture emitted, for pictures without one */
    int has_last_pts;
    uint8_t *packet;        /* the length-prefixed packet being decoded */
    size_t packet_cap, packet_len;

    /* accounting */
    unsigned int pushed, decoded, emitted, dropped_no_key, dropped_no_sets, errors, read_count;
    unsigned int reopens, logged_errors;
};

static void mtv_store_set( uint8_t **slot, uint32_t *slot_size, const uint8_t *nal, uint32_t size, int *dirty )
{
    uint8_t *copy;

    if (*slot && *slot_size == size && !memcmp( *slot, nal, size )) return;
    if (!(copy = malloc( size ))) return;
    memcpy( copy, nal, size );
    free( *slot );
    *slot = copy;
    *slot_size = size;
    *dirty = 1;
}

static uint32_t mtv_pps_sps_id( const uint8_t *nal, uint32_t size, uint32_t *pps_id )
{
    uint8_t rbsp[16];
    struct mtv_bits b;

    b.p = rbsp;
    b.size = mtv_unescape( nal + 1, size - 1, rbsp, sizeof(rbsp) );
    b.pos = 0;
    b.overrun = 0;
    *pps_id = mtv_ue( &b );
    return b.overrun ? UINT32_MAX : mtv_ue( &b );
}

/* SPS and PPS NAL units are kept; returns 1 for one of them. */
static int mtv_take_param_set( struct mtv *v, const uint8_t *nal, uint32_t size )
{
    unsigned int type = nal[0] & 0x1f;

    if (type == 7)
    {
        struct mtv_sps sps;
        if (!mtv_parse_sps( nal, size, &sps ))
        {
            mtx_log( MTV_TAG, "transform %p: an SPS (%u bytes, profile %u) could not be parsed; kept for the decoder",
                     v, size, size > 1 ? nal[1] : 0 );
            if (size >= 4)
            {
                uint8_t rbsp[8];
                struct mtv_bits b = { rbsp, mtv_unescape( nal + 1, size - 1, rbsp, sizeof(rbsp) ), 24, 0 };
                uint32_t id = mtv_ue( &b );
                if (!b.overrun && id < 32) mtv_store_set( &v->sps[id], &v->sps_size[id], nal, size, &v->sets_dirty );
            }
            return 1;
        }
        mtv_store_set( &v->sps[sps.id], &v->sps_size[sps.id], nal, size, &v->sets_dirty );
        if (!v->have_sps || memcmp( &sps, &v->active, sizeof(sps) ))
        {
            v->active = sps;
            v->have_sps = 1;
            v->reorder_depth = mtv_reorder_depth( &sps );
            mtx_log( MTV_TAG, "transform %p: SPS %u profile %u level %u, %ux%u coded, %ux%u picture, poc %u, "
                     "reorder %u%s, %u/%u fps, sar %u:%u, range %s",
                     v, sps.id, sps.profile_idc, sps.level_idc, sps.coded_width, sps.coded_height,
                     sps.width, sps.height, sps.poc_type, v->reorder_depth,
                     sps.has_reorder ? " (vui)" : " (dpb)", sps.fps_n, sps.fps_d, sps.par_n, sps.par_d,
                     sps.full_range < 0 ? "?" : sps.full_range ? "full" : "video" );
        }
        return 1;
    }
    if (type == 8)
    {
        uint32_t pps_id, sps_id = size >= 2 ? mtv_pps_sps_id( nal, size, &pps_id ) : UINT32_MAX;
        if (sps_id < 32 && pps_id < 256)
            mtv_store_set( &v->pps[pps_id], &v->pps_size[pps_id], nal, size, &v->sets_dirty );
        return 1;
    }
    return 0;
}

static void mtv_header_nal( void *ctx, const uint8_t *nal, uint32_t size )
{
    mtv_take_param_set( ctx, nal, size );
}

/* avcC from the current parameter sets; NULL if there is no SPS or no PPS. */
static uint8_t *mtv_build_avcc( struct mtv *v, uint32_t *out_size )
{
    unsigned int nsps = 0, npps = 0, i;
    size_t size = 7;
    const uint8_t *first = NULL;
    uint8_t *avcc, *p;

    for (i = 0; i < 32; i++)
        if (v->sps[i] && v->sps_size[i] >= 4)
        {
            nsps++;
            size += 2 + v->sps_size[i];
            if (!first) first = v->sps[i];
        }
    for (i = 0; i < 256; i++)
        if (v->pps[i]) { npps++; size += 2 + v->pps_size[i]; }
    if (!nsps || !npps || nsps > 31 || size > 65536) return NULL;
    if (!(avcc = malloc( size ))) return NULL;
    p = avcc;
    *p++ = 1;
    *p++ = first[1];                /* profile, compatibility, level of the first SPS */
    *p++ = first[2];
    *p++ = first[3];
    *p++ = 0xff;                    /* 4-byte NAL lengths */
    *p++ = 0xe0 | nsps;
    for (i = 0; i < 32; i++)
        if (v->sps[i] && v->sps_size[i] >= 4)
        {
            *p++ = v->sps_size[i] >> 8;
            *p++ = v->sps_size[i] & 0xff;
            memcpy( p, v->sps[i], v->sps_size[i] );
            p += v->sps_size[i];
        }
    *p++ = npps;
    for (i = 0; i < 256; i++)
        if (v->pps[i])
        {
            *p++ = v->pps_size[i] >> 8;
            *p++ = v->pps_size[i] & 0xff;
            memcpy( p, v->pps[i], v->pps_size[i] );
            p += v->pps_size[i];
        }
    *out_size = (uint32_t)(p - avcc);
    return avcc;
}

static void mtv_release_frames( struct mtv_frame *frames, size_t *count )
{
    size_t i;
    for (i = 0; i < *count; i++)
        if (frames[i].handle && mtx_vbackend) mtx_vbackend->release( frames[i].handle );
    *count = 0;
}

static int mtv_open_decoder( struct mtv *v )
{
    char why[256] = "";
    uint8_t *avcc;
    uint32_t avcc_size = 0;

    if (v->dec)
    {
        mtx_vbackend->close( v->dec );
        v->dec = NULL;
        v->reopens++;
    }
    v->sets_dirty = 0;
    if (!mtx_vbackend)
    {
        v->dec_failed = 1;
        mtx_log( MTV_TAG, "transform %p: no video decoder backend on this platform", v );
        return 0;
    }
    if (!(avcc = mtv_build_avcc( v, &avcc_size )))
    {
        v->dec_failed = 1;
        return 0;
    }
    v->dec = mtx_vbackend->open( MAV_BACKEND_H264, avcc, avcc_size,
                                 v->have_sps ? v->active.width : 0, v->have_sps ? v->active.height : 0,
                                 why, sizeof(why) );
    free( avcc );
    if (!v->dec)
    {
        v->dec_failed = 1;
        if (++v->open_failures <= 3)
            mtx_log( MTV_TAG, "transform %p: %s refused the stream: %s", v, mtx_vbackend->name, why );
        return 0;
    }
    v->dec_failed = 0;
    mtx_log( MTV_TAG, "transform %p: %s decoder %s (%ux%u, reorder %u)", v, mtx_vbackend->name,
             v->reopens ? "reopened for new parameter sets" : "opened",
             v->have_sps ? v->active.width : 0, v->have_sps ? v->active.height : 0, v->reorder_depth );
    return 1;
}

/* the emit callback of the backend: a picture in decode order */
static void mtv_emit( void *ctx, void *handle, int64_t pts, int64_t duration )
{
    struct mtv *v = ctx;
    struct mtv_frame f;
    size_t i;

    if (!mtx_reserve( (void **)&v->reorder, &v->reorder_cap, v->reorder_count + 1, sizeof(*v->reorder) ))
    {
        mtx_vbackend->release( handle );
        return;
    }
    memset( &f, 0, sizeof(f) );
    f.handle = handle;
    f.duration = duration != INT64_MIN ? duration : 0;
    f.width = v->cur_width;
    f.height = v->cur_height;
    f.discontinuity = v->pending_discontinuity;
    v->pending_discontinuity = 0;
    if (pts != INT64_MIN)
    {
        f.pts = pts;
        f.has_pts = 1;
    }
    else
    {
        /* no timestamp: right after the latest one known */
        int64_t step = f.duration > 0 ? f.duration
                       : v->cfg.fps_n && v->cfg.fps_d ? (int64_t)10000000 * v->cfg.fps_d / v->cfg.fps_n : 333667;
        int64_t last = v->reorder_count && v->reorder[v->reorder_count - 1].has_pts ? v->reorder[v->reorder_count - 1].pts
                       : v->has_last_pts ? v->last_pts : -step;
        f.pts = last + step;
        f.has_pts = 0;
    }
    for (i = v->reorder_count; i > 0 && v->reorder[i - 1].pts > f.pts; i--)
        v->reorder[i] = v->reorder[i - 1];
    v->reorder[i] = f;
    v->reorder_count++;
    v->decoded++;
}

/* Moves pictures from the reorder window to the output queue: all of them
 * when `all`, else as many as the window does not need. */
static void mtv_release_ready( struct mtv *v, int all )
{
    while (v->reorder_count && (all || v->reorder_count > v->reorder_depth))
    {
        if (!mtx_reserve( (void **)&v->out, &v->out_cap, v->out_count + 1, sizeof(*v->out) )) return;
        v->out[v->out_count++] = v->reorder[0];
        v->last_pts = v->reorder[0].pts;
        v->has_last_pts = 1;
        memmove( v->reorder, v->reorder + 1, (v->reorder_count - 1) * sizeof(*v->reorder) );
        v->reorder_count--;
    }
}

struct mtv_packet_ctx
{
    struct mtv *v;
    int idr, slices, sets;
};

static void mtv_packet_nal( void *ctx, const uint8_t *nal, uint32_t size )
{
    struct mtv_packet_ctx *pc = ctx;
    struct mtv *v = pc->v;
    unsigned int type = nal[0] & 0x1f;

    if (nal[0] & 0x80) return;      /* forbidden_zero_bit */
    if (mtv_take_param_set( v, nal, size ))
    {
        pc->sets++;
        return;
    }
    switch (type)
    {
    case 1: case 2: case 3: case 4: case 5:
        if (type == 5) pc->idr = 1;
        pc->slices++;
        break;
    case 6:                         /* SEI */
        break;
    default:
        /* AUD, end of sequence / stream, filler, SVC / MVC extensions: not
         * for a VideoToolbox sample buffer. */
        return;
    }
    if (!mtx_reserve( (void **)&v->packet, &v->packet_cap, v->packet_len + 4 + size, 1 )) return;
    v->packet[v->packet_len++] = size >> 24;
    v->packet[v->packet_len++] = (size >> 16) & 0xff;
    v->packet[v->packet_len++] = (size >> 8) & 0xff;
    v->packet[v->packet_len++] = size & 0xff;
    memcpy( v->packet + v->packet_len, nal, size );
    v->packet_len += size;
}

/* Decodes the oldest queued access unit. */
static void mtv_decode_one( struct mtv *v )
{
    struct mtv_au au = v->in[0];
    struct mtv_packet_ctx pc = { v, 0, 0, 0 };
    int err, keyframe;

    memmove( v->in, v->in + 1, (v->in_count - 1) * sizeof(*v->in) );
    v->in_count--;

    v->packet_len = 0;
    if (mtv_is_annexb( au.data, au.size )) mtv_annexb_walk( au.data, au.size, mtv_packet_nal, &pc );
    else if (mtv_is_avcc_packet( au.data, au.size )) mtv_avcc_walk( au.data, au.size, mtv_packet_nal, &pc );
    else
    {
        mtv_annexb_walk( au.data, au.size, mtv_packet_nal, &pc );
        if (!pc.slices && v->logged_errors < 4)
        {
            v->logged_errors++;
            mtx_log( MTV_TAG, "transform %p: a %u-byte sample is neither Annex B nor length-prefixed "
                     "(starts %02x %02x %02x %02x); dropped", v, au.size, au.size > 0 ? au.data[0] : 0,
                     au.size > 1 ? au.data[1] : 0, au.size > 2 ? au.data[2] : 0, au.size > 3 ? au.data[3] : 0 );
        }
    }
    free( au.data );
    if (au.flags & MTX_FLAG_DISCONTINUITY) v->pending_discontinuity = 1;
    if (!pc.slices) return;

    /* Decoding starts at an IDR.  A stream whose random access points are
     * not IDRs (open GOP) is started at a sample the caller marked as a
     * sync point once a second's worth of samples has gone by without one. */
    keyframe = pc.idr || ((au.flags & MTX_FLAG_SYNC_POINT) && v->waited >= 30);
    if (v->need_keyframe)
    {
        if (!keyframe)
        {
            if (!v->waited++)
                mtx_log( MTV_TAG, "transform %p: waiting for a keyframe; dropping access units until one comes", v );
            v->dropped_no_key++;
            return;
        }
        v->need_keyframe = 0;
        v->waited = 0;
    }
    if (!v->dec || v->sets_dirty)
    {
        /* The same parameter sets fail the same way, so after a failed open
         * only a keyframe (which comes with its sets) tries again. */
        if (!v->sets_dirty && v->dec_failed && !keyframe)
        {
            v->dropped_no_sets++;
            return;
        }
        if (!mtv_open_decoder( v ))
        {
            if (!v->dropped_no_sets++)
                mtx_log( MTV_TAG, "transform %p: no usable SPS/PPS yet; dropping access units", v );
            v->need_keyframe = 1;
            return;
        }
    }

    v->cur_width = v->have_sps ? v->active.width : 0;
    v->cur_height = v->have_sps ? v->active.height : 0;
    err = mtx_vbackend->decode( v->dec, v->packet, (uint32_t)v->packet_len,
                                (au.flags & MTX_FLAG_HAS_PTS) ? au.pts : INT64_MIN,
                                (au.flags & MTX_FLAG_HAS_DURATION) ? (int64_t)au.duration : INT64_MIN,
                                keyframe, mtv_emit, v );
    if (err < 0)
    {
        v->errors++;
        if (v->logged_errors < 8)
        {
            v->logged_errors++;
            mtx_log( MTV_TAG, "transform %p: decode failed (%d) on a %zu-byte access unit%s", v, err,
                     v->packet_len, keyframe ? " (keyframe)" : "" );
        }
    }
    mtv_release_ready( v, 0 );
}

static void mtv_clear_input( struct mtv *v )
{
    size_t i;
    for (i = 0; i < v->in_count; i++) free( v->in[i].data );
    v->in_count = 0;
}

static int mtv_create( const struct mtv_config *cfg, struct mtv **out )
{
    struct mtv *v;

    *out = NULL;
    if (cfg->pix == MTX_PIX_NONE) return MTX_UNSUPPORTED;
    if (!(v = calloc( 1, sizeof(*v) ))) return MTX_NO_MEMORY;
    pthread_mutex_init( &v->lock, NULL );
    v->cfg = *cfg;
    v->cfg.header = NULL;
    v->cfg.header_size = 0;
    v->pix = cfg->pix;
    v->out_width = cfg->out_width;
    v->out_height = cfg->out_height;
    v->dw_width = cfg->dw_width;
    v->dw_height = cfg->dw_height;
    v->ap_width = cfg->ap_width;
    v->ap_height = cfg->ap_height;
    v->need_keyframe = 1;
    v->reorder_depth = 4;
    if (cfg->header && cfg->header_size && cfg->header_size < 65536)
    {
        if (cfg->header[0] == 1 && cfg->header_size >= 7)
        {
            /* avcC: the parameter sets are in it */
            const uint8_t *p = cfg->header;
            uint32_t n = cfg->header_size, pos = 6, i, num = p[5] & 0x1f, group;
            for (group = 0; group < 2; group++)
            {
                if (group)
                {
                    if (pos >= n) break;
                    num = p[pos++];
                }
                for (i = 0; i < num && pos + 2 <= n; i++)
                {
                    uint32_t len = (p[pos] << 8) | p[pos + 1];
                    pos += 2;
                    if (!len || pos + len > n) break;
                    mtv_take_param_set( v, p + pos, len );
                    pos += len;
                }
            }
        }
        else mtv_annexb_walk( cfg->header, cfg->header_size, mtv_header_nal, v );
    }
    mtx_log( MTV_TAG, "transform %p created: input %ux%u %u/%u fps, %u codec bytes; output %d %ux%u "
             "(frame %ux%u, aperture %ux%u); align %u, queue %u, format change %d, low latency %d",
             v, cfg->in_width, cfg->in_height, cfg->fps_n, cfg->fps_d, cfg->header_size, cfg->pix,
             cfg->out_width, cfg->out_height, cfg->dw_width, cfg->dw_height, cfg->ap_width, cfg->ap_height,
             cfg->plane_align, cfg->input_queue_length, cfg->allow_format_change, cfg->low_latency );
    *out = v;
    return MTX_OK;
}

static void mtv_destroy( struct mtv *v )
{
    unsigned int i;

    if (!v) return;
    pthread_mutex_lock( &v->lock );
    mtx_log( MTV_TAG, "transform %p destroyed: %u pushed, %u decoded, %u read, %u decode errors, "
             "%u dropped before a keyframe, %u without parameter sets, %u reopens",
             v, v->pushed, v->decoded, v->read_count, v->errors, v->dropped_no_key, v->dropped_no_sets, v->reopens );
    mtv_clear_input( v );
    mtv_release_frames( v->reorder, &v->reorder_count );
    mtv_release_frames( v->out, &v->out_count );
    if (v->dec) mtx_vbackend->close( v->dec );
    for (i = 0; i < 32; i++) free( v->sps[i] );
    for (i = 0; i < 256; i++) free( v->pps[i] );
    free( v->in );
    free( v->reorder );
    free( v->out );
    free( v->packet );
    pthread_mutex_unlock( &v->lock );
    pthread_mutex_destroy( &v->lock );
    free( v );
}

static int mtv_push( struct mtv *v, const struct mtx_in *in )
{
    struct mtv_au au;

    if (in->size && !in->data) return MTX_INVALID;
    if (in->size > MTX_MAX_AU_SIZE) return MTX_INVALID;
    pthread_mutex_lock( &v->lock );
    if (v->in_count >= (size_t)v->cfg.input_queue_length + 1)
    {
        pthread_mutex_unlock( &v->lock );
        return MTX_NOT_ACCEPTING;
    }
    if (!mtx_reserve( (void **)&v->in, &v->in_cap, v->in_count + 1, sizeof(*v->in) ))
    {
        pthread_mutex_unlock( &v->lock );
        return MTX_NO_MEMORY;
    }
    au.size = in->size;
    au.flags = in->flags;
    au.pts = in->pts;
    au.duration = in->duration;
    if (!(au.data = malloc( in->size ? in->size : 1 )))
    {
        pthread_mutex_unlock( &v->lock );
        return MTX_NO_MEMORY;
    }
    if (in->size) memcpy( au.data, in->data, in->size );
    v->in[v->in_count++] = au;
    v->pushed++;
    pthread_mutex_unlock( &v->lock );
    return MTX_OK;
}

static int mtv_accepts_input( struct mtv *v )
{
    int ret;
    pthread_mutex_lock( &v->lock );
    ret = v->in_count < (size_t)v->cfg.input_queue_length + 1;
    pthread_mutex_unlock( &v->lock );
    return ret;
}

/* The size of a picture whose SPS was not understood: the decoder's own. */
static void mtv_resolve_size( struct mtv_frame *f )
{
    struct mav_vplanes planes;

    if (f->width && f->height) return;
    if (mtx_vbackend->map( f->handle, &planes )) return;
    f->width = planes.width;
    f->height = planes.height;
    mtx_vbackend->unmap( f->handle );
}

static void mtv_drop_head( struct mtv *v )
{
    mtx_vbackend->release( v->out[0].handle );
    memmove( v->out, v->out + 1, (v->out_count - 1) * sizeof(*v->out) );
    v->out_count--;
    v->errors++;
}

static int mtv_read( struct mtv *v, struct mtx_out *out )
{
    struct mtx_layout l;
    struct mav_vplanes planes;
    struct mtv_frame f;
    uint32_t width, height;

    out->size = 0;
    out->flags = 0;
    pthread_mutex_lock( &v->lock );
    for (;;)
    {
        if (!v->out_count)
        {
            if (v->in_count)
            {
                mtv_decode_one( v );
                continue;
            }
            pthread_mutex_unlock( &v->lock );
            return MTX_NEED_MORE_INPUT;
        }
        f = v->out[0];
        mtv_resolve_size( &f );
        if (f.width && f.height) break;
        /* an SPS this file could not read and a picture that cannot be
         * mapped: nothing to show */
        mtv_drop_head( v );
    }
    v->out[0].width = f.width;
    v->out[0].height = f.height;

    if (v->cfg.allow_format_change
        && (!v->caps_announced || f.width != v->out_width || f.height != v->out_height))
    {
        mtx_log( MTV_TAG, "transform %p: stream format %ux%u (was %ux%u); reporting a stream change",
                 v, f.width, f.height, v->out_width, v->out_height );
        v->out_width = f.width;
        v->out_height = f.height;
        v->caps_announced = 1;
        pthread_mutex_unlock( &v->lock );
        return MTX_STREAM_CHANGE;
    }

    width = v->cfg.allow_format_change || !v->out_width || !v->out_height ? f.width : v->out_width;
    height = v->cfg.allow_format_change || !v->out_width || !v->out_height ? f.height : v->out_height;
    if (!mtv_layout( v->pix, width, height, v->cfg.plane_align, out->stride,
                     v->dw_width, v->dw_height, v->ap_width, v->ap_height, &l ))
    {
        pthread_mutex_unlock( &v->lock );
        return MTX_INVALID;
    }
    if (out->max_size < l.size || !out->data)
    {
        if (v->logged_errors < 8)
        {
            v->logged_errors++;
            mtx_log( MTV_TAG, "transform %p: output buffer of %u bytes is too small for a %zu-byte %ux%u frame",
                     v, out->max_size, l.size, width, height );
        }
        pthread_mutex_unlock( &v->lock );
        return MTX_BUFFER_TOO_SMALL;
    }
    if (mtx_vbackend->map( f.handle, &planes ))
    {
        mtv_drop_head( v );
        pthread_mutex_unlock( &v->lock );
        return MTX_NEED_MORE_INPUT;
    }
    mtv_write_picture( v->pix, &planes, out->data, &l );
    mtx_vbackend->unmap( f.handle );
    mtx_vbackend->release( f.handle );
    memmove( v->out, v->out + 1, (v->out_count - 1) * sizeof(*v->out) );
    v->out_count--;

    out->size = (uint32_t)l.size;
    out->flags = MTX_FLAG_SYNC_POINT;
    if (f.has_pts)
    {
        out->flags |= MTX_FLAG_HAS_PTS;
        out->pts = f.pts;
        if (v->cfg.preserve_timestamps) out->flags |= MTX_FLAG_PRESERVE_TIMESTAMPS;
    }
    if (f.duration > 0)
    {
        out->flags |= MTX_FLAG_HAS_DURATION;
        out->duration = (uint64_t)f.duration;
    }
    if (f.discontinuity) out->flags |= MTX_FLAG_DISCONTINUITY;
    if (!v->read_count)
        mtx_log( MTV_TAG, "transform %p: first picture %ux%u -> format %d, stride %u x %u rows, %zu bytes "
                 "(buffer %u, 2D stride %u)", v, width, height, v->pix, l.stride[0], l.rows[0],
                 l.size, out->max_size, out->stride );
    v->read_count++;
    pthread_mutex_unlock( &v->lock );
    return MTX_OK;
}

static void mtv_get_output( struct mtv *v, struct mtv_output_info *info )
{
    uint32_t align;

    pthread_mutex_lock( &v->lock );
    memset( info, 0, sizeof(*info) );
    align = v->cfg.plane_align;
    info->pix = v->pix;
    info->width = v->out_width;
    info->height = v->out_height;
    /* init_mf_video_format_from_gst_caps(): the frame size is the caps size
     * rounded up to the plane alignment */
    info->frame_width = (v->out_width + align) & ~align;
    info->frame_height = (v->out_height + align) & ~align;
    if (v->cfg.fps_n && v->cfg.fps_d)
    {
        info->fps_n = v->cfg.fps_n;
        info->fps_d = v->cfg.fps_d;
    }
    else if (v->have_sps && v->active.fps_n && v->active.fps_d)
    {
        info->fps_n = v->active.fps_n;
        info->fps_d = v->active.fps_d;
    }
    if (v->have_sps && v->active.par_n && v->active.par_d)
    {
        info->par_n = v->active.par_n;
        info->par_d = v->active.par_d;
    }
    else if (v->cfg.par_n && v->cfg.par_d)
    {
        info->par_n = v->cfg.par_n;
        info->par_d = v->cfg.par_d;
    }
    else info->par_n = info->par_d = 1;
    pthread_mutex_unlock( &v->lock );
}

static int mtv_set_output( struct mtv *v, enum mtx_pix pix, uint32_t width, uint32_t height,
                           uint32_t dw_width, uint32_t dw_height, uint32_t ap_width, uint32_t ap_height )
{
    if (pix == MTX_PIX_NONE) return MTX_UNSUPPORTED;
    pthread_mutex_lock( &v->lock );
    if (pix != v->pix)
        mtx_log( MTV_TAG, "transform %p: output format %d -> %d", v, v->pix, pix );
    /* Pictures already decoded are converted at read time, so nothing queued
     * is lost by a format change (wg_transform.c has to drop them). */
    v->pix = pix;
    v->dw_width = dw_width;
    v->dw_height = dw_height;
    v->ap_width = ap_width;
    v->ap_height = ap_height;
    /* With allow_format_change the stream decides the size; otherwise the
     * caller's size is what gets produced (cropped or padded). */
    if (!v->cfg.allow_format_change && width && height)
    {
        v->out_width = width;
        v->out_height = height;
    }
    pthread_mutex_unlock( &v->lock );
    return MTX_OK;
}

static int mtv_drain( struct mtv *v )
{
    pthread_mutex_lock( &v->lock );
    /* Decode everything queued and give up the reorder window now, so the
     * caller can read it all out and push again right away. */
    while (v->in_count) mtv_decode_one( v );
    mtv_release_ready( v, 1 );
    pthread_mutex_unlock( &v->lock );
    return MTX_OK;
}

static int mtv_flush( struct mtv *v )
{
    pthread_mutex_lock( &v->lock );
    mtv_clear_input( v );
    mtv_release_frames( v->reorder, &v->reorder_count );
    mtv_release_frames( v->out, &v->out_count );
    if (v->dec) mtx_vbackend->flush( v->dec );
    v->need_keyframe = 1;
    v->waited = 0;
    v->pending_discontinuity = 1;
    v->has_last_pts = 0;
    pthread_mutex_unlock( &v->lock );
    return MTX_OK;
}

/***********************************************************************
 *           the AAC transform
 */
struct mta
{
    pthread_mutex_t lock;
    struct mta_config cfg;
    uint8_t asc[64];
    uint32_t asc_size;
    int adts;

    void *dec;
    int dec_failed;
    uint32_t dec_rate, dec_channels;
    float *dec_buf;

    SwrContext *swr;
    uint32_t swr_rate, swr_channels;    /* what it was set up for */

    uint8_t *pcm;
    size_t pcm_cap, pcm_pos, pcm_len;
    uint32_t frame_size;                /* output bytes per frame */
    int64_t pts;                        /* of the first frame in pcm, 100 ns */
    int have_pts;
    int discontinuity;
    uint32_t last_frames;

    unsigned int pushed, units, decoded_frames, errors, logged;
};

static const uint32_t mta_rates[13] = { 96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350 };

/* AAC-LC AudioSpecificConfig for a rate and channel count. */
static uint32_t mta_default_asc( uint32_t rate, uint32_t channels, uint8_t *asc )
{
    unsigned int i;

    for (i = 0; i < 13; i++) if (mta_rates[i] == rate) break;
    if (channels > 7) channels = channels == 8 ? 7 : 2;    /* 7: 7.1 */
    if (!channels) channels = 2;
    if (i < 13)
    {
        asc[0] = (2 << 3) | (i >> 1);
        asc[1] = ((i & 1) << 7) | (channels << 3);
        return 2;
    }
    /* frequency index 15: the rate in 24 bits */
    asc[0] = (2 << 3) | (15 >> 1);
    asc[1] = (1u << 7) | ((rate >> 17) & 0x7f);
    asc[2] = (rate >> 9) & 0xff;
    asc[3] = (rate >> 1) & 0xff;
    asc[4] = ((rate & 1) << 7) | (channels << 3);
    return 5;
}

/* ADTS header: returns its length (7 or 9) and the frame length, 0 if `p` is
 * not one. */
static uint32_t mta_adts_header( const uint8_t *p, uint32_t n, uint32_t *frame_len, uint8_t *asc, uint32_t *asc_size,
                                 uint32_t *blocks )
{
    uint32_t profile, index, channels, header;

    if (n < 7 || p[0] != 0xff || (p[1] & 0xf6) != 0xf0) return 0;
    header = (p[1] & 1) ? 7 : 9;
    profile = (p[2] >> 6) & 3;
    index = (p[2] >> 2) & 0xf;
    channels = ((p[2] & 1) << 2) | (p[3] >> 6);
    *frame_len = ((uint32_t)(p[3] & 3) << 11) | ((uint32_t)p[4] << 3) | (p[5] >> 5);
    *blocks = (p[6] & 3) + 1;
    if (index > 12 || *frame_len < header) return 0;
    asc[0] = ((profile + 1) << 3) | (index >> 1);
    asc[1] = ((index & 1) << 7) | (channels << 3);
    *asc_size = 2;
    return header;
}

static void mta_close_decoder( struct mta *a )
{
    if (a->dec) mtx_abackend->close( a->dec );
    a->dec = NULL;
    swr_free( &a->swr );
    av_freep( &a->dec_buf );
}

static int mta_open_decoder( struct mta *a )
{
    char why[256] = "";
    uint32_t rate = a->cfg.in_rate ? a->cfg.in_rate : 48000, channels = a->cfg.in_channels ? a->cfg.in_channels : 2;

    if (!mtx_abackend)
    {
        a->dec_failed = 1;
        mtx_log( MTA_TAG, "transform %p: no audio decoder backend on this platform", a );
        return 0;
    }
    if (!a->asc_size) a->asc_size = mta_default_asc( rate, channels, a->asc );
    a->dec = mtx_abackend->open( MAV_BACKEND_AAC, a->asc, a->asc_size, &rate, &channels, why, sizeof(why) );
    if (!a->dec)
    {
        a->dec_failed = 1;
        mtx_log( MTA_TAG, "transform %p: %s refused the stream: %s", a, mtx_abackend->name, why );
        return 0;
    }
    if (!rate || !channels || channels > 8 || !(a->dec_buf = av_malloc( (size_t)MAV_AUDIO_BACKEND_MAX_FRAMES * channels * sizeof(float) )))
    {
        mtx_abackend->close( a->dec );
        a->dec = NULL;
        a->dec_failed = 1;
        return 0;
    }
    a->dec_rate = rate;
    a->dec_channels = channels;
    mtx_log( MTA_TAG, "transform %p: %s decoder opened, %u Hz %u ch (config %02x %02x, %u bytes) -> %s %u Hz %u ch",
             a, mtx_abackend->name, rate, channels, a->asc[0], a->asc_size > 1 ? a->asc[1] : 0, a->asc_size,
             a->cfg.out_float ? "float32" : "s16", a->cfg.out_rate, a->cfg.out_channels );
    return 1;
}

static void mta_layout( AVChannelLayout *layout, uint32_t mask, uint32_t channels )
{
    if (mask && (uint32_t)__builtin_popcount( mask ) == channels)
        av_channel_layout_from_mask( layout, mask );
    else
        av_channel_layout_default( layout, channels );
}

static int mta_setup_swr( struct mta *a )
{
    AVChannelLayout in_layout = {0}, out_layout = {0};
    int err;

    if (a->swr && a->swr_rate == a->dec_rate && a->swr_channels == a->dec_channels) return 1;
    swr_free( &a->swr );
    mta_layout( &in_layout, 0, a->dec_channels );
    mta_layout( &out_layout, a->cfg.out_mask, a->cfg.out_channels );
    err = swr_alloc_set_opts2( &a->swr, &out_layout, a->cfg.out_float ? AV_SAMPLE_FMT_FLT : AV_SAMPLE_FMT_S16,
                               a->cfg.out_rate, &in_layout, AV_SAMPLE_FMT_FLT, a->dec_rate, 0, NULL );
    av_channel_layout_uninit( &in_layout );
    av_channel_layout_uninit( &out_layout );
    if (err < 0 || (err = swr_init( a->swr )) < 0)
    {
        mtx_log( MTA_TAG, "transform %p: no conversion %u Hz %u ch -> %u Hz %u ch (%d)", a, a->dec_rate,
                 a->dec_channels, a->cfg.out_rate, a->cfg.out_channels, err );
        swr_free( &a->swr );
        return 0;
    }
    a->swr_rate = a->dec_rate;
    a->swr_channels = a->dec_channels;
    return 1;
}

/* Appends `frames` decoded frames (NULL: flush the converter). */
static int mta_convert( struct mta *a, const float *in, uint32_t frames )
{
    const uint8_t *src[1] = { (const uint8_t *)in };
    int64_t max_out;
    uint8_t *dst[1];
    int got;

    if (!a->swr) return MTX_OK;
    max_out = swr_get_out_samples( a->swr, (int)frames );
    if (max_out < 0) return MTX_OK;
    if (!max_out) max_out = 1;
    if (!mtx_reserve( (void **)&a->pcm, &a->pcm_cap, a->pcm_len + (size_t)max_out * a->frame_size, 1 ))
        return MTX_NO_MEMORY;
    dst[0] = a->pcm + a->pcm_len;
    got = swr_convert( a->swr, dst, (int)max_out, in ? src : NULL, in ? (int)frames : 0 );
    if (got > 0) a->pcm_len += (size_t)got * a->frame_size;
    return MTX_OK;
}

static void mta_silence( struct mta *a, uint32_t frames )
{
    size_t bytes;

    if (!frames || !a->cfg.out_rate) return;
    /* in output frames */
    frames = (uint32_t)((uint64_t)frames * a->cfg.out_rate / (a->dec_rate ? a->dec_rate : a->cfg.out_rate));
    bytes = (size_t)frames * a->frame_size;
    if (!mtx_reserve( (void **)&a->pcm, &a->pcm_cap, a->pcm_len + bytes, 1 )) return;
    memset( a->pcm + a->pcm_len, 0, bytes );
    a->pcm_len += bytes;
}

static void mta_decode_unit( struct mta *a, const uint8_t *data, uint32_t size )
{
    int frames;

    if (!a->dec)
    {
        if (a->dec_failed || !mta_open_decoder( a )) return;
    }
    if (!mta_setup_swr( a )) return;
    a->units++;
    frames = mtx_abackend->decode( a->dec, data, size, a->dec_buf, MAV_AUDIO_BACKEND_MAX_FRAMES );
    if (frames < 0)
    {
        a->errors++;
        if (a->logged < 6)
        {
            a->logged++;
            mtx_log( MTA_TAG, "transform %p: decode failed (%d) on a %u-byte unit; %u frames of silence",
                     a, frames, size, a->last_frames ? a->last_frames : 1024 );
        }
        mta_silence( a, a->last_frames ? a->last_frames : 1024 );
        return;
    }
    if (frames > MAV_AUDIO_BACKEND_MAX_FRAMES) frames = MAV_AUDIO_BACKEND_MAX_FRAMES;
    if (frames) a->last_frames = (uint32_t)frames;
    a->decoded_frames += (uint32_t)frames;
    mta_convert( a, a->dec_buf, (uint32_t)frames );
}

static int mta_create( const struct mta_config *cfg, struct mta **out )
{
    struct mta *a;

    *out = NULL;
    if (!cfg->out_rate || !cfg->out_channels || cfg->out_channels > 8) return MTX_UNSUPPORTED;
    if (!(a = calloc( 1, sizeof(*a) ))) return MTX_NO_MEMORY;
    pthread_mutex_init( &a->lock, NULL );
    a->cfg = *cfg;
    a->cfg.asc = NULL;
    a->cfg.asc_size = 0;
    a->adts = cfg->adts;
    if (cfg->asc && cfg->asc_size && cfg->asc_size <= sizeof(a->asc))
    {
        memcpy( a->asc, cfg->asc, cfg->asc_size );
        a->asc_size = cfg->asc_size;
    }
    a->frame_size = cfg->out_channels * (cfg->out_float ? 4 : 2);
    mtx_log( MTA_TAG, "transform %p created: %s, %u Hz %u ch, %u config bytes -> %s %u Hz %u ch mask %#x",
             a, cfg->adts ? "ADTS" : "raw", cfg->in_rate, cfg->in_channels, a->asc_size,
             cfg->out_float ? "float32" : "s16", cfg->out_rate, cfg->out_channels, cfg->out_mask );
    *out = a;
    return MTX_OK;
}

static void mta_destroy( struct mta *a )
{
    if (!a) return;
    pthread_mutex_lock( &a->lock );
    mtx_log( MTA_TAG, "transform %p destroyed: %u pushed, %u units, %u frames decoded, %u errors",
             a, a->pushed, a->units, a->decoded_frames, a->errors );
    mta_close_decoder( a );
    free( a->pcm );
    pthread_mutex_unlock( &a->lock );
    pthread_mutex_destroy( &a->lock );
    free( a );
}

static void mta_compact( struct mta *a )
{
    if (a->pcm_pos == a->pcm_len) a->pcm_pos = a->pcm_len = 0;
}

static int mta_push( struct mta *a, const struct mtx_in *in )
{
    if (in->size && !in->data) return MTX_INVALID;
    pthread_mutex_lock( &a->lock );
    /* Like the WMA transform (and attrs.input_queue_length 0): one sample in
     * flight, so ProcessInput and ProcessOutput alternate. */
    if (a->pcm_len > a->pcm_pos)
    {
        pthread_mutex_unlock( &a->lock );
        return MTX_NOT_ACCEPTING;
    }
    mta_compact( a );
    a->pushed++;
    if (in->flags & MTX_FLAG_HAS_PTS)
    {
        a->pts = in->pts;
        a->have_pts = 1;
    }
    if (in->flags & MTX_FLAG_DISCONTINUITY) a->discontinuity = 1;

    if (in->size && (a->adts || (in->size >= 7 && in->data[0] == 0xff && (in->data[1] & 0xf6) == 0xf0 && !a->asc_size)))
    {
        /* ADTS: one or more frames per sample */
        uint32_t pos = 0;
        while (pos + 7 <= in->size)
        {
            uint8_t asc[2];
            uint32_t frame_len, asc_size, blocks, header;
            header = mta_adts_header( in->data + pos, in->size - pos, &frame_len, asc, &asc_size, &blocks );
            if (!header || frame_len > in->size - pos)
            {
                if (a->logged < 6)
                {
                    a->logged++;
                    mtx_log( MTA_TAG, "transform %p: no ADTS frame at byte %u of %u; rest dropped", a, pos, in->size );
                }
                break;
            }
            if (!a->dec && !a->dec_failed && (!a->asc_size || a->adts))
            {
                memcpy( a->asc, asc, asc_size );
                a->asc_size = asc_size;
            }
            a->adts = 1;
            if (blocks == 1) mta_decode_unit( a, in->data + pos + header, frame_len - header );
            else if (a->logged < 6)
            {
                a->logged++;
                mtx_log( MTA_TAG, "transform %p: ADTS frame with %u raw blocks skipped", a, blocks );
            }
            pos += frame_len;
        }
    }
    else if (in->size) mta_decode_unit( a, in->data, in->size );

    pthread_mutex_unlock( &a->lock );
    return MTX_OK;
}

static int mta_read( struct mta *a, struct mtx_out *out )
{
    size_t avail, copy;
    uint64_t frames;

    out->size = 0;
    out->flags = 0;
    pthread_mutex_lock( &a->lock );
    avail = a->pcm_len - a->pcm_pos;
    copy = avail < out->max_size ? avail : out->max_size;
    copy -= copy % a->frame_size;
    if (!copy)
    {
        pthread_mutex_unlock( &a->lock );
        return MTX_NEED_MORE_INPUT;
    }
    if (!out->data)
    {
        pthread_mutex_unlock( &a->lock );
        return MTX_INVALID;
    }
    memcpy( out->data, a->pcm + a->pcm_pos, copy );
    out->size = (uint32_t)copy;
    frames = copy / a->frame_size;
    out->flags = MTX_FLAG_SYNC_POINT | MTX_FLAG_HAS_DURATION;
    out->duration = frames * 10000000 / a->cfg.out_rate;
    if (a->have_pts)
    {
        out->flags |= MTX_FLAG_HAS_PTS;
        out->pts = a->pts;
        a->pts += (int64_t)out->duration;
    }
    if (a->discontinuity)
    {
        out->flags |= MTX_FLAG_DISCONTINUITY;
        a->discontinuity = 0;
    }
    a->pcm_pos += copy;
    if (a->pcm_pos < a->pcm_len) out->flags |= MTX_FLAG_INCOMPLETE;
    else mta_compact( a );
    pthread_mutex_unlock( &a->lock );
    return MTX_OK;
}

static int mta_set_output( struct mta *a, int out_float, uint32_t rate, uint32_t channels, uint32_t mask )
{
    if (!rate || !channels || channels > 8) return MTX_UNSUPPORTED;
    pthread_mutex_lock( &a->lock );
    a->cfg.out_float = out_float;
    a->cfg.out_rate = rate;
    a->cfg.out_channels = channels;
    a->cfg.out_mask = mask;
    a->frame_size = channels * (out_float ? 4 : 2);
    /* what is converted already is in the old format */
    a->pcm_pos = a->pcm_len = 0;
    swr_free( &a->swr );
    pthread_mutex_unlock( &a->lock );
    mtx_log( MTA_TAG, "transform %p: output set to %s %u Hz %u ch", a, out_float ? "float32" : "s16", rate, channels );
    return MTX_OK;
}

static int mta_accepts_input( struct mta *a )
{
    int ret;
    pthread_mutex_lock( &a->lock );
    ret = a->pcm_len == a->pcm_pos;
    pthread_mutex_unlock( &a->lock );
    return ret;
}

static int mta_drain( struct mta *a )
{
    pthread_mutex_lock( &a->lock );
    if (a->swr) mta_convert( a, NULL, 0 );
    pthread_mutex_unlock( &a->lock );
    return MTX_OK;
}

static int mta_flush( struct mta *a )
{
    pthread_mutex_lock( &a->lock );
    a->pcm_pos = a->pcm_len = 0;
    a->have_pts = 0;
    a->discontinuity = 1;
    if (a->dec) mtx_abackend->flush( a->dec );
    if (a->swr) swr_init( a->swr );
    pthread_mutex_unlock( &a->lock );
    return MTX_OK;
}
