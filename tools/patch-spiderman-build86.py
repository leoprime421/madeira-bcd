#!/usr/bin/env python3
"""Spider-Man graphics fixes used by the IPA build (ml1159/ml1160/ml1161)."""
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
mad = root / "madeira-d3d12/src/pe/madeira_d3d12.c"
dxgi = root / "dxmt/src/dxgi/dxgi_output.cpp"

s = mad.read_text()
marker = "ml1159 zero-size TEXTURE2D"
if marker not in s:
    global_anchor = "static struct mad_device *g_last_device;   /* ml887: GetDevice fallback for children without a back pointer */\n"
    global_new = global_anchor + "static volatile LONG g_last_swap_width, g_last_swap_height; /* ml1159: last valid drawable size */\n"
    if s.count(global_anchor) != 1:
        raise SystemExit("ml1159 global anchor changed")
    s = s.replace(global_anchor, global_new, 1)

    tex_old = """    ti->pixel_format = *pf;
    ti->width = (uint32_t)desc->Width;
    ti->height = desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D ? 1 : desc->Height;
    ti->depth = 1;"""
    tex_new = """    ti->pixel_format = *pf;
    ti->width = (uint32_t)desc->Width;
    ti->height = desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE1D ? 1 : desc->Height;
    /* ml1159 zero-size TEXTURE2D: use the last valid drawable dimensions.
     * Keep this deliberately expression-simple for llvm-mingw ARM64EC C. */
    if (desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D) {
        if (!ti->width && g_last_swap_width > 0)
            ti->width = (uint32_t)g_last_swap_width;
        if (!ti->height && g_last_swap_height > 0)
            ti->height = (uint32_t)g_last_swap_height;
    }
    if (!ti->width || !ti->height)
        return 0;
    ti->depth = 1;"""
    if s.count(tex_old) != 1:
        raise SystemExit("ml1159 texinfo anchor changed")
    s = s.replace(tex_old, tex_new, 1)

    swap_anchor = """static HRESULT mad_swap_make_buffers(struct mad_swapchain *s) {
    D3D12_RESOURCE_DESC rd;
    UINT i, n = s->desc.BufferCount ? s->desc.BufferCount : 2;
    int is_depth;"""
    swap_new = swap_anchor + """
    /* ml1159: publish a valid drawable size for later resource recovery. */
    if (s->desc.Width && s->desc.Height) {
        g_last_swap_width = (LONG)s->desc.Width;
        g_last_swap_height = (LONG)s->desc.Height;
    }"""
    if s.count(swap_anchor) != 1:
        raise SystemExit("ml1159 swapchain anchor changed")
    s = s.replace(swap_anchor, swap_new, 1)
    mad.write_text(s)

# ml1161: repair the existing resolved_desc before the resource copies it.
s = mad.read_text()
if "ml1161 resource-desc" not in s:
    old = """    D3D12_RESOURCE_DESC resolved_desc = *desc;
    if (desc->Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
        resolved_desc.MipLevels = (UINT16)mad_resource_mip_count(desc);
    desc = &resolved_desc;   /* subresource indexing matches the Metal allocation */"""
    new = """    D3D12_RESOURCE_DESC resolved_desc = *desc;
    /* ml1161 resource-desc: keep GetDesc/bookkeeping consistent with ml1159.
     * No temporary declarations or logging here: this hot path must also build
     * with llvm-mingw's ARM64EC C frontend. */
    if (resolved_desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D) {
        if (!resolved_desc.Width && g_last_swap_width > 0)
            resolved_desc.Width = (UINT64)g_last_swap_width;
        if (!resolved_desc.Height && g_last_swap_height > 0)
            resolved_desc.Height = (UINT)g_last_swap_height;
    }
    if (resolved_desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
        resolved_desc.MipLevels = (UINT16)mad_resource_mip_count(&resolved_desc);
    desc = &resolved_desc;   /* subresource indexing matches the Metal allocation */"""
    if s.count(old) != 1:
        raise SystemExit(f"ml1161 resolved-desc anchor changed (found {s.count(old)})")
    s = s.replace(old, new, 1)
    mad.write_text(s)

# ml1160: if iOS WSI enumerates no modes, expose the valid current mode.
d = dxgi.read_text()
if "ml1160 fallback-current" not in d:
    anchor = """      dstModeId += 1;
    }

    /* MADEIRA (ml1190): opt-in diagnostics, DXMT_DISPLAY_MODE_STATS=1. */"""
    replacement = """      dstModeId += 1;
    }

#ifdef DXMT_IOS
    /* ml1160 fallback-current: publish the real current iOS mode if the
     * enumerable WSI mode list is empty. */
    if (dstModeId == 0) {
      wsi::WsiMode currentMode = {};
      if (wsi::getCurrentDisplayMode(monitor_, &currentMode) &&
          currentMode.width && currentMode.height) {
        if (!currentMode.refreshRate.numerator || !currentMode.refreshRate.denominator) {
          currentMode.refreshRate.numerator = 60;
          currentMode.refreshRate.denominator = 1;
        }
        if (pDesc != nullptr) {
          DXGI_MODE_DESC1 mode = ConvertDisplayMode(currentMode);
          mode.Format = EnumFormat;
          modeList.push_back(mode);
        }
        dstModeId = 1;
        static std::atomic<unsigned> fallbackLogs{0};
        if (fallbackLogs.fetch_add(1, std::memory_order_relaxed) < 16)
          Logger::warn(str::format("[dxgi-modes] ml1160 fallback-current ",
              currentMode.width, "x", currentMode.height, " @ ",
              currentMode.refreshRate.numerator, "/", currentMode.refreshRate.denominator,
              " format=", unsigned(EnumFormat)));
      }
    }
#endif

    /* MADEIRA (ml1190): opt-in diagnostics, DXMT_DISPLAY_MODE_STATS=1. */"""
    if d.count(anchor) != 1:
        raise SystemExit("ml1160 DXGI mode anchor changed")
    d = d.replace(anchor, replacement, 1)
    dxgi.write_text(d)

s = mad.read_text(); d = dxgi.read_text()
if s.count("/* ml1159 zero-size TEXTURE2D:") != 1 or s.count("g_last_swap_width") < 3:
    raise SystemExit("ml1159 post-patch verification failed")
if s.count("/* ml1161 resource-desc:") != 1:
    raise SystemExit("ml1161 post-patch verification failed")
if d.count("/* ml1160 fallback-current:") != 1 or d.count("[dxgi-modes] ml1160 fallback-current ") != 1:
    raise SystemExit("ml1160 post-patch verification failed")
print("Spider-Man patches applied: ml1159 Metal size + ml1160 DXGI mode + ml1161 descriptor consistency (minimal ARM64EC C)")
