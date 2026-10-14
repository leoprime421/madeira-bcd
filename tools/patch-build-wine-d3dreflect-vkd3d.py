#!/usr/bin/env python3
"""Backport Wine's VKD3D-backed D3DReflect path and rebuild the ARM64EC DLLs.

Ace Combat 7's TrueSky plugin calls D3DReflect from d3dcompiler_43.  Madeira's
pinned Wine still uses the old in-d3dcompiler reflection parser, which emits a
large number of partial-stub/FIXME calls before the observed ARM64EC crash.
Wine upstream replaced that parser in commit
815fe9deb15b2cc742a243166316a1d33c6a03e0 with D3DReflectVKD3D().

This script backports only the runtime pieces needed by that change, using the
immutable upstream commit for the two large source files and verifying their
Git blob IDs before installing them.  It then rebuilds and ships wined3d plus
D3DCompiler 43/47 so the new export is present at runtime.
"""

from pathlib import Path
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WINE = ROOT / "wine"
UPSTREAM = "815fe9deb15b2cc742a243166316a1d33c6a03e0"


def git_blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(f"blob {len(data)}\0".encode("ascii"))
    h.update(data)
    return h.hexdigest()


def fetch_verified(path: str, expected_blob: str) -> bytes:
    url = f"https://raw.githubusercontent.com/wine-mirror/wine/{UPSTREAM}/{path}"
    last = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "madeira-bcd-build"})
            with urllib.request.urlopen(req, timeout=45) as r:
                data = r.read()
            actual = git_blob_sha(data)
            if actual != expected_blob:
                raise RuntimeError(f"{path}: blob {actual}, expected {expected_blob}")
            return data
        except Exception as exc:
            last = exc
            if attempt != 3:
                time.sleep(2 * attempt)
    raise RuntimeError(f"failed to fetch verified upstream {path}: {last}")


def replace_once(path: Path, old: str, new: str, marker: str, label: str) -> None:
    s = path.read_text(encoding="utf-8")
    if marker in s:
        print(f"ace7 d3dreflect: {label} already patched")
        return
    if s.count(old) != 1:
        raise RuntimeError(f"ace7 d3dreflect: {label} anchor changed (count={s.count(old)})")
    path.write_text(s.replace(old, new, 1), encoding="utf-8")
    print(f"ace7 d3dreflect: patched {label}")


# 1) Replace the old VKD3D D3D12-only reflection helper with the upstream
# version that supports the D3DCompiler 10/11 interface generations too.
vkd3d_reflection = WINE / "libs/vkd3d/libs/vkd3d-utils/reflection.c"
vkd3d_text = vkd3d_reflection.read_text(encoding="utf-8")
if "reflection_iid_supported" not in vkd3d_text or "D3DReflectVKD3D" not in vkd3d_text:
    data = fetch_verified(
        "libs/vkd3d/libs/vkd3d-utils/reflection.c",
        "db5a9852e165278e4f1d1128ba31a10eeb4731c0",
    )
    vkd3d_reflection.write_bytes(data)
    print("ace7 d3dreflect: installed upstream VKD3D reflection.c")
else:
    print("ace7 d3dreflect: VKD3D reflection.c already modern")

# 2) d3dcompiler_43 is also the parent source for d3dcompiler_47.  Upstream's
# wrapper delegates D3DReflect to the version-aware VKD3D implementation.
d3dc_reflection = WINE / "dlls/d3dcompiler_43/reflection.c"
d3dc_text = d3dc_reflection.read_text(encoding="utf-8")
if "vkd3d_D3DReflectVKD3D" not in d3dc_text:
    data = fetch_verified(
        "dlls/d3dcompiler_43/reflection.c",
        "c918b2a14768b859f6c5e6e00376d1a625011ac7",
    )
    d3dc_reflection.write_bytes(data)
    print("ace7 d3dreflect: installed upstream d3dcompiler reflection wrapper")
else:
    print("ace7 d3dreflect: d3dcompiler reflection wrapper already modern")

# 3) The new helper needs stable names for the historical ShaderReflection
# IIDs so one VKD3D object can service compiler versions 40/43/46/47.
header = WINE / "include/d3d11shader.h"
old_iids = """#if D3D_COMPILER_VERSION <= 42
DEFINE_GUID(IID_ID3D11ShaderReflection, 0x17f27486, 0xa342, 0x4d10, 0x88, 0x42, 0xab, 0x08, 0x74, 0xe7, 0xf6, 0x70);
#elif D3D_COMPILER_VERSION == 43
DEFINE_GUID(IID_ID3D11ShaderReflection, 0x0a233719, 0x3960, 0x4578, 0x9d, 0x7c, 0x20, 0x3b, 0x8b, 0x1d, 0x9c, 0xc1);
#else
DEFINE_GUID(IID_ID3D11ShaderReflection, 0x8d536ca1, 0x0cca, 0x4956, 0xa8, 0x37, 0x78, 0x69, 0x63, 0x75, 0x55, 0x84);
#endif
"""
new_iids = """DEFINE_GUID(IID_ID3D11ShaderReflection_v40, 0x17f27486, 0xa342, 0x4d10, 0x88, 0x42, 0xab, 0x08, 0x74, 0xe7, 0xf6, 0x70);
DEFINE_GUID(IID_ID3D11ShaderReflection_v43, 0x0a233719, 0x3960, 0x4578, 0x9d, 0x7c, 0x20, 0x3b, 0x8b, 0x1d, 0x9c, 0xc1);
DEFINE_GUID(IID_ID3D11ShaderReflection_v46, 0x8d536ca1, 0x0cca, 0x4956, 0xa8, 0x37, 0x78, 0x69, 0x63, 0x75, 0x55, 0x84);

#if D3D_COMPILER_VERSION <= 42
# define IID_ID3D11ShaderReflection IID_ID3D11ShaderReflection_v40
#elif D3D_COMPILER_VERSION == 43
# define IID_ID3D11ShaderReflection IID_ID3D11ShaderReflection_v43
#else
# define IID_ID3D11ShaderReflection IID_ID3D11ShaderReflection_v46
#endif
"""
replace_once(header, old_iids, new_iids,
             "IID_ID3D11ShaderReflection_v43", "d3d11shader versioned IIDs")

# 4) Export the helper from wined3d, where Wine's embedded libvkd3d is linked.
spec = WINE / "dlls/wined3d/wined3d.spec"
spec_anchor = "@ stdcall vkd3d_D3DCompile2VKD3D(ptr long ptr ptr ptr ptr ptr long long long ptr long ptr ptr long) D3DCompile2VKD3D\n"
spec_new = spec_anchor + "@ stdcall vkd3d_D3DReflectVKD3D(ptr long ptr ptr long) D3DReflectVKD3D\n"
replace_once(spec, spec_anchor, spec_new,
             "vkd3d_D3DReflectVKD3D", "wined3d D3DReflectVKD3D export")

# 5) wined3d_main owns INITGUID, so instantiate the D3D10 reflection IIDs used
# by the shared VKD3D implementation.
main_c = WINE / "dlls/wined3d/wined3d_main.c"
main_anchor = '#include "wined3d_gl.h"\n'
main_new = main_anchor + '#include <d3d10.h>\n#include <d3d10_1shader.h>\n'
replace_once(main_c, main_anchor, main_new,
             "#include <d3d10_1shader.h>", "wined3d reflection GUID includes")

# Source-only use is supported for local investigations.  In the IPA workflow
# build-arm64ec is already restored/configured by the time the caller runs us.
build = WINE / "build-arm64ec"
if not (build / "Makefile").exists():
    print("ace7 d3dreflect: build-arm64ec absent; source backport complete, rebuild skipped")
    raise SystemExit(0)

# Force relink of the three runtime DLLs. Source mtimes force the affected
# libvkd3d object to rebuild; deleting final DLLs avoids a restored cache being
# accepted without checking the new wined3d export.
targets = [
    "dlls/wined3d/arm64ec-windows/wined3d.dll",
    "dlls/d3dcompiler_43/arm64ec-windows/d3dcompiler_43.dll",
    "dlls/d3dcompiler_47/arm64ec-windows/d3dcompiler_47.dll",
]
for rel in targets:
    p = build / rel
    if p.exists():
        p.unlink()

jobs = str(max(2, os.cpu_count() or 4))
log = ROOT / "build" / "ace7-d3dreflect-arm64ec.log"
log.parent.mkdir(parents=True, exist_ok=True)
print(f"ace7 d3dreflect: rebuilding wined3d + d3dcompiler 43/47 (-j{jobs})")
with log.open("wb") as out:
    proc = subprocess.run(["make", "-C", str(build), f"-j{jobs}", *targets],
                          cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
if proc.returncode:
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
    print("\n".join(tail), file=sys.stderr)
    raise SystemExit(f"ace7 d3dreflect: ARM64EC rebuild failed ({proc.returncode})")

ship = ROOT / "app/Madeira/arm64ec-windows"
mingw_strip = ROOT / "toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin/llvm-strip"
strip = str(mingw_strip) if mingw_strip.exists() else shutil.which("llvm-strip")
if not strip:
    raise SystemExit("ace7 d3dreflect: llvm-strip not found")


def pad_pe(path: Path) -> None:
    data = path.read_bytes()
    if len(data) < 0x40:
        raise RuntimeError(f"{path}: short PE")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise RuntimeError(f"{path}: invalid PE signature")
    size_of_image = struct.unpack_from("<I", data, pe + 24 + 56)[0]
    target = size_of_image + 0x10000
    if len(data) < target:
        with path.open("ab") as f:
            f.write(b"\0" * (target - len(data)))


for rel in targets:
    built = build / rel
    if not built.exists():
        raise SystemExit(f"ace7 d3dreflect: expected output missing: {built}")
    name = built.name
    tmp = ship / (name + ".ace7.tmp")
    shutil.copy2(built, tmp)
    subprocess.run([strip, str(tmp)], check=True)
    pad_pe(tmp)
    os.replace(tmp, ship / name)
    print(f"ace7 d3dreflect: shipped rebuilt {name}")

print("ace7 d3dreflect: backport + ARM64EC runtime rebuild complete")
