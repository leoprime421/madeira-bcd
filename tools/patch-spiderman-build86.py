#!/usr/bin/env python3
"""Spider-Man graphics fixes: zero-size textures, DXGI modes and descriptor consistency.

Build 85 reaches a real D3D12 swapchain (1280x720 / 1408x648), then Metal
asserts because a later D3D12 TEXTURE2D descriptor reaches winemetal as 0x0.
Build 87 proved the Metal-side recovery works, but also proved the D3D12
resource object still retained the original 0x0 descriptor. Keep both sides
consistent so GetDesc/allocation/resource bookkeeping see the same dimensions.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
mad = root / "madeira-d3d12/src/pe/madeira_d3d12.c"
dxgi = root / "dxmt/src/dxgi/dxgi_output.cpp"

# --- madeira_d3d12: remember the last valid swapchain dimensions and use them
# only when a later 2D texture descriptor has a missing dimension. Do not turn
# arbitrary invalid 1D/3D resources into 1x1 textures.
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
    /* ml1159: Spider-Man build 85 creates the real 1280x720/1408x648
     * swapchain successfully, then a later TEXTURE2D reaches Metal as 0x0.
     * Metal asserts instead of returning an HRESULT. Windows never hands
     * Metal a zero-size texture; for this drawable-adjacent case recover the
     * missing dimensions from the last valid swapchain rather than inventing
     * 1x1. Other invalid dimensions are rejected below. */
    if (desc->Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
        (!ti->width || !ti->height)) {
        UINT sw = (UINT)InterlockedCompareExchange(&g_last_swap_width, 0, 0);
        UINT sh = (UINT)InterlockedCompareExchange(&g_last_swap_height, 0, 0);
        if (sw && sh) {
            static LONG said;
            UINT oldw = ti->width, oldh = ti->height;
            if (!ti->width) ti->width = sw;
            if (!ti->height) ti->height = sh;
            if (InterlockedIncrement(&said) <= 16)
                d3d12_log("[madeira-d3d12] ml1159 zero-size TEXTURE2D %ux%u -> swapchain %ux%u\n",
                          oldw, oldh, ti->width, ti->height);
        }
    }
    if (!ti->width || !ti->height) {
        d3d12_log("[madeira-d3d12] ml1159 refusing invalid texture dimensions %ux%u dim=%u\n",
                  ti->width, ti->height, (unsigned)desc->Dimension);
        return 0;
    }
    ti->depth = 1;"""
    if s.count(tex_old) != 1:
        raise SystemExit("ml1159 texinfo anchor changed")
    s = s.replace(tex_old, tex_new, 1)

    swap_anchor = """static HRESULT mad_swap_make_buffers(struct mad_swapchain *s) {
    D3D12_RESOURCE_DESC rd;
    UINT i, n = s->desc.BufferCount ? s->desc.BufferCount : 2;
    int is_depth;"""
    swap_new = swap_anchor + """
    /* ml1159: publish only a valid drawable size. The resource builder may
     * use it to repair a later zero-dimension 2D drawable resource. */
    if (s->desc.Width && s->desc.Height) {
        InterlockedExchange(&g_last_swap_width, (LONG)s->desc.Width);
        InterlockedExchange(&g_last_swap_height, (LONG)s->desc.Height);
    }"""
    if s.count(swap_anchor) != 1:
        raise SystemExit("ml1159 swapchain anchor changed")
    s = s.replace(swap_anchor, swap_new, 1)
    mad.write_text(s)

# Build 89/90 / ml1161: keep the actual D3D12 descriptor consistent with the
# Metal texture. mad_create_resource_at() already creates resolved_desc before
# any resource allocation, so repair that existing local copy instead of
# declaring a second D3D12_RESOURCE_DESC in the middle of the function.
s = mad.read_text()
desc_marker = "ml1161 resource-desc"
if desc_marker not in s:
    desc_anchor = """    D3D12_RESOURCE_DESC resolved_desc = *desc;
    if (desc->Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
        resolved_desc.MipLevels = (UINT16)mad_resource_mip_count(desc);
    desc = &resolved_desc;   /* subresource indexing matches the Metal allocation */"""
    desc_replacement = """    D3D12_RESOURCE_DESC resolved_desc = *desc;
    /* ml1161 resource-desc: ml1159 repaired only the temporary Metal texture
     * descriptor. Repair the function's existing resolved_desc before it is
     * copied into the resource, so GetDesc()/size/bookkeeping all agree. */
    if (resolved_desc.Dimension == D3D12_RESOURCE_DIMENSION_TEXTURE2D &&
        (!resolved_desc.Width || !resolved_desc.Height)) {
        UINT sw = (UINT)InterlockedCompareExchange(&g_last_swap_width, 0, 0);
        UINT sh = (UINT)InterlockedCompareExchange(&g_last_swap_height, 0, 0);
        if (sw && sh) {
            UINT64 oldw = resolved_desc.Width;
            UINT oldh = resolved_desc.Height;
            if (!resolved_desc.Width) resolved_desc.Width = sw;
            if (!resolved_desc.Height) resolved_desc.Height = sh;
            {
                static LONG said_desc;
                if (InterlockedIncrement(&said_desc) <= 16)
                    d3d12_log("[madeira-d3d12] ml1161 resource-desc %llux%u -> %llux%u (swapchain)\n",
                              (unsigned long long)oldw, oldh,
                              (unsigned long long)resolved_desc.Width, resolved_desc.Height);
            }
        }
    }
    if (resolved_desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
        resolved_desc.MipLevels = (UINT16)mad_resource_mip_count(&resolved_desc);
    desc = &resolved_desc;   /* subresource indexing matches the Metal allocation */"""
    if s.count(desc_anchor) != 1:
        raise SystemExit(f"ml1161 resolved-desc anchor changed (found {s.count(desc_anchor)})")
    s = s.replace(desc_anchor, desc_replacement, 1)
    mad.write_text(s)

# --- DXGI: iOS/headless WSI can enumerate zero modes while current mode works.
# Expose that current mode as a single valid DXGI mode instead of returning 0.
d = dxgi.read_text()
mode_marker = "ml1160 fallback-current"
if mode_marker not in d:
    anchor = """      dstModeId += 1;
    }

    /* MADEIRA (ml1190): opt-in diagnostics, DXMT_DISPLAY_MODE_STATS=1. */"""
    replacement = """      dstModeId += 1;
    }

#ifdef DXMT_IOS
    /* ml1160 fallback-current: the Madeira/iOS WSI backend can report a
     * current mode while exposing no enumerable mode list. Spider-Man treats
     * an empty DXGI list as "Could not find any display mode". Publish the
     * real current mode, preserving the requested DXGI format. */
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

# Strict verification. ml1160 intentionally appears twice: once in the source
# comment and once in the runtime log string. ml1161 likewise has one source
# marker and one runtime marker.
s = mad.read_text(); d = dxgi.read_text()
if s.count(marker) != 1 or s.count("g_last_swap_width") < 3:
    raise SystemExit("ml1159 post-patch verification failed")
if s.count("/* ml1161 resource-desc:") != 1 or s.count("[madeira-d3d12] ml1161 resource-desc ") != 1:
    raise SystemExit("ml1161 post-patch verification failed")
if d.count("/* ml1160 fallback-current:") != 1 or d.count("[dxgi-modes] ml1160 fallback-current ") != 1:
    raise SystemExit("ml1160 post-patch verification failed")
print("Spider-Man patches applied: ml1159 Metal size + ml1160 DXGI mode + ml1161 D3D12 descriptor consistency")
