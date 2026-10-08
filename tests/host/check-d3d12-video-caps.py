#!/usr/bin/env python3
"""Exercise the real no-hardware D3D12 video COM interface on a Wine-header host.
No Metal, Wine process or Windows binary is needed. Requires generated Wine headers.
"""
from pathlib import Path
import os, subprocess, tempfile
root = Path(__file__).resolve().parents[2]
wine = Path(os.environ.get('MADEIRA_WINE_SRC', root / 'wine'))
build = Path(os.environ.get('MADEIRA_WINE_BUILD', wine / 'build-host'))
source = r'''
#define INITGUID
#define COBJMACROS
#include <initguid.h>
#include <windows.h>
#include <assert.h>
#include "mad_video_caps.h"
struct parent { IUnknown iface; unsigned refs; struct mad_video_caps video; };
static ULONG STDMETHODCALLTYPE add(IUnknown *i) { return ++((struct parent *)i)->refs; }
static ULONG STDMETHODCALLTYPE release(IUnknown *i) { return --((struct parent *)i)->refs; }
static HRESULT STDMETHODCALLTYPE query(IUnknown *i, REFIID iid, void **out) {
    struct parent *p = (struct parent *)i;
    *out = NULL;
    if (IsEqualGUID(iid, &IID_IUnknown)) *out = i;
    else if (IsEqualGUID(iid, &IID_ID3D12VideoDevice)) *out = &p->video.iface;
    else return E_NOINTERFACE;
    add(i); return S_OK;
}
static const IUnknownVtbl parent_vtbl = {query, add, release};
int main(void) {
    struct parent p = {{&parent_vtbl}, 1, {0}};
    ID3D12VideoDevice *v = NULL, *v2 = NULL;
    IUnknown *identity = NULL;
    struct mad_video_profile_count count = {0, 99};
    struct mad_video_profiles profiles = {0, 0, (GUID *)0x1234};
    struct mad_video_format_count formats = {0};
    D3D12_FEATURE_DATA_VIDEO_FEATURE_AREA_SUPPORT areas = {0, TRUE, TRUE, TRUE};
    D3D12_FEATURE_DATA_VIDEO_DECODE_SUPPORT support = {0};
    GUID unknown = {0xdeadbeef}; void *out = (void *)1;
    mad_video_init(&p.video, &p.iface);
    assert(IUnknown_QueryInterface(&p.iface, &IID_ID3D12VideoDevice, (void **)&v) == S_OK && v);
    assert(ID3D12VideoDevice_QueryInterface(v, &IID_IUnknown, (void **)&identity) == S_OK && identity == &p.iface);
    assert(IUnknown_QueryInterface(identity, &IID_ID3D12VideoDevice, (void **)&v2) == S_OK && v2 == v);
    assert(ID3D12VideoDevice_QueryInterface(v, &unknown, &out) == E_NOINTERFACE && !out);
    assert(ID3D12VideoDevice_CheckFeatureSupport(v, D3D12_FEATURE_VIDEO_DECODE_PROFILE_COUNT, &count, sizeof(count)) == S_OK && count.ProfileCount == 0);
    assert(ID3D12VideoDevice_CheckFeatureSupport(v, D3D12_FEATURE_VIDEO_DECODE_PROFILES, &profiles, sizeof(profiles)) == S_OK && profiles.pProfiles == (GUID *)0x1234);
    formats.FormatCount = 99;
    assert(ID3D12VideoDevice_CheckFeatureSupport(v, D3D12_FEATURE_VIDEO_DECODE_FORMAT_COUNT, &formats, sizeof(formats)) == S_OK && formats.FormatCount == 0);
    assert(ID3D12VideoDevice_CheckFeatureSupport(v, D3D12_FEATURE_VIDEO_FEATURE_AREA_SUPPORT, &areas, sizeof(areas)) == S_OK && !areas.VideoDecodeSupport && !areas.VideoProcessSupport && !areas.VideoEncodeSupport);
    support.Width = 1920; support.Height = 1080; support.SupportFlags = 1;
    assert(ID3D12VideoDevice_CheckFeatureSupport(v, D3D12_FEATURE_VIDEO_DECODE_SUPPORT, &support, sizeof(support)) == S_OK && !support.SupportFlags && support.Width == 1920 && support.Height == 1080);
    count.ProfileCount = 123;
    assert(ID3D12VideoDevice_CheckFeatureSupport(v, D3D12_FEATURE_VIDEO_DECODE_PROFILE_COUNT, &count, sizeof(count)-1) == E_INVALIDARG && count.ProfileCount == 123);
    count.NodeIndex = 1;
    assert(ID3D12VideoDevice_CheckFeatureSupport(v, D3D12_FEATURE_VIDEO_DECODE_PROFILE_COUNT, &count, sizeof(count)) == E_INVALIDARG && count.ProfileCount == 123);
    assert(ID3D12VideoDevice_CheckFeatureSupport(v, D3D12_FEATURE_VIDEO_DECODE_PROFILE_COUNT, NULL, sizeof(count)) == E_INVALIDARG);
    out = (void *)1;
    assert(ID3D12VideoDevice_CreateVideoDecoder(v, NULL, &IID_IUnknown, &out) == E_NOTIMPL && !out);
    out = (void *)1;
    assert(ID3D12VideoDevice_CreateVideoDecoderHeap(v, NULL, &IID_IUnknown, &out) == E_NOTIMPL && !out);
    out = (void *)1;
    assert(ID3D12VideoDevice_CreateVideoProcessor(v, 0, NULL, 0, NULL, &IID_IUnknown, &out) == E_NOTIMPL && !out);
    ID3D12VideoDevice_Release(v2); IUnknown_Release(identity); ID3D12VideoDevice_Release(v);
    assert(p.refs == 1);
    return 0;
}
'''
with tempfile.TemporaryDirectory() as tmp:
    c = Path(tmp) / 'caps.c'; exe = Path(tmp) / 'caps'
    c.write_text(source)
    subprocess.run([os.environ.get('CC', 'cc'), '-O1', '-g', '-fsanitize=address,undefined',
                    '-fno-sanitize-recover=undefined', '-D__WINESRC__', '-include', str(build / 'include/config.h'),
                    '-I' + str(build / 'include'), '-I' + str(wine / 'include'),
                    '-I' + str(root / 'madeira-d3d12/src/pe'), str(c), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
print('PASS: D3D12 video COM identity, lifetime, capability queries and unsupported creation')
