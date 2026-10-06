#!/usr/bin/env python3
"""Build 86: fix Spider-Man D3D12 zero-size Metal texture + empty DXGI modes.

Build 85 reaches a real D3D12 swapchain (1280x720 / 1408x648), then Metal
asserts because a later D3D12 TEXTURE2D descriptor reaches winemetal as 0x0.
Madeira also reports no usable display modes even though the current mode is
known.  Keep both fixes iOS/Madeira-local and source-backed.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
mad = root / "madeira-d3d12/src/pe/madeira_d3d12.c"
dxgi = root / "dxmt/src/dxgi/dxgi_output.cpp"

# --- madeira_d3d12: remember the last valid swapchain dimensions and use them
# only when a later 2D texture descriptor has a missing dimension.  Do not turn
# arbitrary invalid 1D/3D resources into 1x1 textures.
s = mad.read_text()
marker = "ml1159 zero-size TEXTURE2D"
if marker not in s:
    global_anchor = "static struct mad_device *g_last_device;   /* ml887: GetDevice fallback for children without a back pointer */\n"
    global_new = global_anchor + "static volatile LONG g_last_swap_width, g_last_swap_height; /* ml1159: last valid drawable size */\n"
    if s.count(global_anchor) != 1:
        raise SystemExit("ml1159 global anchor changed")
    s = s.replace(global_anchor, global_new, 1)

    tex_old = """    ti->pixel_format = *pf;\n    ti->width = (uint32_t)desc->Width;\n    ti->height = desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D ? 1 : desc->Height;\n    ti->depth = 1;"""
    tex_new = """    ti->pixel_format = *pf;\n    ti->width = (uint32_t)desc->Width;\n    ti->height = desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D ? 1 : desc->Height;\n    /* ml1159: Spider-Man build 85 creates the real 1280x720/1408x648\n     * swapchain successfully, then a later TEXTURE2D reaches Metal as 0x0.\n     * Metal asserts instead of returning an HRESULT.  Windows never hands\n     * Metal a zero-size texture; for this drawable-adjacent case recover the\n     * missing dimensions from the last valid swapchain rather than inventing\n     * 1x1.  Other invalid dimensions are rejected below. */\n    if (desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&\n        (!ti->width || !ti->height)) {\n        UINT sw = (UINT)InterlockedCompareExchange(&g_last_swap_width, 0, 0);\n        UINT sh = (UINT)InterlockedCompareExchange(&g_last_swap_height, 0, 0);\n        if (sw && sh) {\n            static LONG said;\n            UINT oldw = ti->width, oldh = ti->height;\n            if (!ti->width) ti->width = sw;\n            if (!ti->height) ti->height = sh;\n            if (InterlockedIncrement(&said) <= 16)\n                d3d12_log(\"[madeira-d3d12] ml1159 zero-size TEXTURE2D %ux%u -> swapchain %ux%u\\n\",\n                          oldw, oldh, ti->width, ti->height);\n        }\n    }\n    if (!ti->width || !ti->height) {\n        d3d12_log(\"[madeira-d3d12] ml1159 refusing invalid texture dimensions %ux%u dim=%u\\n\",\n                  ti->width, ti->height, (unsigned)desc->Dimension);\n        return 0;\n    }\n    ti->depth = 1;"""
    if s.count(tex_old) != 1:
        raise SystemExit("ml1159 texinfo anchor changed")
    s = s.replace(tex_old, tex_new, 1)

    swap_anchor = """static HRESULT mad_swap_make_buffers(struct mad_swapchain *s) {\n    D3D12_RESOURCE_DESC rd;\n    UINT i, n = s->desc.BufferCount ? s->desc.BufferCount : 2;\n    int is_depth;"""
    swap_new = swap_anchor + """\n    /* ml1159: publish only a valid drawable size.  The resource builder may\n     * use it to repair a later zero-dimension 2D drawable resource. */\n    if (s->desc.Width && s->desc.Height) {\n        InterlockedExchange(&g_last_swap_width, (LONG)s->desc.Width);\n        InterlockedExchange(&g_last_swap_height, (LONG)s->desc.Height);\n    }"""
    if s.count(swap_anchor) != 1:
        raise SystemExit("ml1159 swapchain anchor changed")
    s = s.replace(swap_anchor, swap_new, 1)
    mad.write_text(s)

# --- DXGI: iOS/headless WSI can enumerate zero modes while current mode works.
# Expose that current mode as a single valid DXGI mode instead of returning 0.
d = dxgi.read_text()
mode_marker = "ml1160 fallback-current"
if mode_marker not in d:
    anchor = """      dstModeId += 1;\n    }\n\n    /* MADEIRA (ml1190): opt-in diagnostics, DXMT_DISPLAY_MODE_STATS=1. */"""
    replacement = """      dstModeId += 1;\n    }\n\n#ifdef DXMT_IOS\n    /* ml1160 fallback-current: the Madeira/iOS WSI backend can report a\n     * current mode while exposing no enumerable mode list. Spider-Man treats\n     * an empty DXGI list as \"Could not find any display mode\". Publish the\n     * real current mode, preserving the requested DXGI format. */\n    if (dstModeId == 0) {\n      wsi::WsiMode currentMode = {};\n      if (wsi::getCurrentDisplayMode(monitor_, &currentMode) &&\n          currentMode.width && currentMode.height) {\n        if (!currentMode.refreshRate.numerator || !currentMode.refreshRate.denominator) {\n          currentMode.refreshRate.numerator = 60;\n          currentMode.refreshRate.denominator = 1;\n        }\n        if (pDesc != nullptr) {\n          DXGI_MODE_DESC1 mode = ConvertDisplayMode(currentMode);\n          mode.Format = EnumFormat;\n          modeList.push_back(mode);\n        }\n        dstModeId = 1;\n        static std::atomic<unsigned> fallbackLogs{0};\n        if (fallbackLogs.fetch_add(1, std::memory_order_relaxed) < 16)\n          Logger::warn(str::format(\"[dxgi-modes] ml1160 fallback-current \",\n              currentMode.width, \"x\", currentMode.height, \" @ \",\n              currentMode.refreshRate.numerator, \"/\", currentMode.refreshRate.denominator,\n              \" format=\", unsigned(EnumFormat)));\n      }\n    }\n#endif\n\n    /* MADEIRA (ml1190): opt-in diagnostics, DXMT_DISPLAY_MODE_STATS=1. */"""
    if d.count(anchor) != 1:
        raise SystemExit("ml1160 DXGI mode anchor changed")
    d = d.replace(anchor, replacement, 1)
    dxgi.write_text(d)

# Strict verification: exactly one implementation marker each.
s = mad.read_text(); d = dxgi.read_text()
if s.count(marker) != 1 or s.count("g_last_swap_width") < 3:
    raise SystemExit("ml1159 post-patch verification failed")
if d.count(mode_marker) != 1:
    raise SystemExit("ml1160 post-patch verification failed")
print("Build 86 patches applied: ml1159 D3D12 zero-size recovery + ml1160 DXGI current-mode fallback")
