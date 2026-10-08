/* D3D12 video capability discovery for devices without a D3D12 video backend.
 * This is a separate COM interface, not the graphics device's vtable.
 * Hardware decoding/processing remains unavailable; MF platform decoders
 * are independent of this API. */
#ifndef MADEIRA_VIDEO_CAPS_H
#define MADEIRA_VIDEO_CAPS_H
#include <d3d12video.h>
#include <string.h>
#include <stddef.h>

struct mad_video_caps {
    ID3D12VideoDevice iface;
    IUnknown *parent;
};
/* These layouts are specified by d3d12video.h; older Wine headers expose
 * the feature enums but omit the corresponding count/list structs. */
struct mad_video_profile_count { UINT NodeIndex, ProfileCount; };
struct mad_video_profiles { UINT NodeIndex, ProfileCount; GUID *pProfiles; };
struct mad_video_format_count { UINT NodeIndex; D3D12_VIDEO_DECODE_CONFIGURATION Configuration; UINT FormatCount; };
struct mad_video_formats { UINT NodeIndex; D3D12_VIDEO_DECODE_CONFIGURATION Configuration; UINT FormatCount; DXGI_FORMAT *pOutputFormats; };

/* Check the wire layout directly: both Wine and the CI MinGW headers can
 * omit the SDK typedefs above. These assertions run on the host too. */
_Static_assert(sizeof(struct mad_video_profile_count) == 8, "video profile count ABI");
_Static_assert(offsetof(struct mad_video_profiles, pProfiles) == 8 &&
               sizeof(struct mad_video_profiles) == 8 + sizeof(void *), "video profiles ABI");
_Static_assert(offsetof(struct mad_video_format_count, Configuration) == 4 &&
               offsetof(struct mad_video_format_count, FormatCount) == 28 &&
               sizeof(struct mad_video_format_count) == 32, "video format count ABI");
_Static_assert(offsetof(struct mad_video_formats, pOutputFormats) == 32 &&
               sizeof(struct mad_video_formats) == 32 + sizeof(void *), "video formats ABI");

static struct mad_video_caps *mad_video_impl(ID3D12VideoDevice *iface) {
    return CONTAINING_RECORD(iface, struct mad_video_caps, iface);
}
static HRESULT STDMETHODCALLTYPE mad_video_qi(ID3D12VideoDevice *iface, REFIID iid, void **out) {
    if (!out) return E_POINTER;
    *out = NULL;
    if (!iid) return E_INVALIDARG;
    if (IsEqualGUID(iid, &IID_ID3D12VideoDevice)) {
        *out = iface;
        IUnknown_AddRef(mad_video_impl(iface)->parent);
        return S_OK;
    }
    /* The parent's IUnknown preserves COM identity and lifetime. */
    return IUnknown_QueryInterface(mad_video_impl(iface)->parent, iid, out);
}
static ULONG STDMETHODCALLTYPE mad_video_addref(ID3D12VideoDevice *iface) {
    return IUnknown_AddRef(mad_video_impl(iface)->parent);
}
static ULONG STDMETHODCALLTYPE mad_video_release(ID3D12VideoDevice *iface) {
    return IUnknown_Release(mad_video_impl(iface)->parent);
}
static HRESULT STDMETHODCALLTYPE mad_video_check(ID3D12VideoDevice *iface, D3D12_FEATURE_VIDEO feature, void *data, UINT size) {
    (void)iface;
    if (!data) return E_INVALIDARG;
    /* Single-node device. Preserve all caller inputs and zero only outputs. */
    switch (feature) {
    case D3D12_FEATURE_VIDEO_DECODE_PROFILE_COUNT: {
        struct mad_video_profile_count *d = data;
        if (size != sizeof(*d) || d->NodeIndex) return E_INVALIDARG;
        d->ProfileCount = 0; return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODE_PROFILES: {
        struct mad_video_profiles *d = data;
        if (size != sizeof(*d) || d->NodeIndex || d->ProfileCount) return E_INVALIDARG;
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODE_FORMAT_COUNT: {
        struct mad_video_format_count *d = data;
        if (size != sizeof(*d) || d->NodeIndex) return E_INVALIDARG;
        d->FormatCount = 0; return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODE_FORMATS: {
        struct mad_video_formats *d = data;
        if (size != sizeof(*d) || d->NodeIndex || d->FormatCount) return E_INVALIDARG;
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_DECODE_SUPPORT: {
        D3D12_FEATURE_DATA_VIDEO_DECODE_SUPPORT *d = data;
        if (size != sizeof(*d) || d->NodeIndex) return E_INVALIDARG;
        d->SupportFlags = D3D12_VIDEO_DECODE_SUPPORT_FLAG_NONE;
        d->ConfigurationFlags = D3D12_VIDEO_DECODE_CONFIGURATION_FLAG_NONE;
        d->DecodeTier = D3D12_VIDEO_DECODE_TIER_NOT_SUPPORTED;
        return S_OK;
    }
    case D3D12_FEATURE_VIDEO_FEATURE_AREA_SUPPORT: {
        D3D12_FEATURE_DATA_VIDEO_FEATURE_AREA_SUPPORT *d = data;
        if (size != sizeof(*d) || d->NodeIndex) return E_INVALIDARG;
        d->VideoDecodeSupport = d->VideoProcessSupport = d->VideoEncodeSupport = FALSE;
        return S_OK;
    }
    default: return E_INVALIDARG;
    }
}
static HRESULT STDMETHODCALLTYPE mad_video_create_decoder(ID3D12VideoDevice *iface,
    const D3D12_VIDEO_DECODER_DESC *desc, REFIID iid, void **out) {
    (void)iface; (void)desc; (void)iid;
    if (!out) return E_POINTER;
    *out = NULL; return E_NOTIMPL;
}
static HRESULT STDMETHODCALLTYPE mad_video_create_heap(ID3D12VideoDevice *iface,
    const D3D12_VIDEO_DECODER_HEAP_DESC *desc, REFIID iid, void **out) {
    (void)iface; (void)desc; (void)iid;
    if (!out) return E_POINTER;
    *out = NULL; return E_NOTIMPL;
}
static HRESULT STDMETHODCALLTYPE mad_video_create_processor(ID3D12VideoDevice *iface, UINT node,
    const D3D12_VIDEO_PROCESS_OUTPUT_STREAM_DESC *output, UINT count,
    const D3D12_VIDEO_PROCESS_INPUT_STREAM_DESC *inputs, REFIID iid, void **out) {
    (void)iface; (void)node; (void)output; (void)count; (void)inputs; (void)iid;
    if (!out) return E_POINTER;
    *out = NULL; return E_NOTIMPL;
}
static const ID3D12VideoDeviceVtbl mad_video_vtbl = {
    mad_video_qi, mad_video_addref, mad_video_release, mad_video_check,
    mad_video_create_decoder, mad_video_create_heap, mad_video_create_processor
};
static void mad_video_init(struct mad_video_caps *caps, IUnknown *parent) {
    caps->iface.lpVtbl = &mad_video_vtbl;
    caps->parent = parent;
}
#endif
