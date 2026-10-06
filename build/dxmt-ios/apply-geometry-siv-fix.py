#!/usr/bin/env python3
"""Apply Madeira's narrow DXBC geometry-shader SIV compatibility fix.

Build 86 also applies the source-backed Spider-Man graphics fixes before DXMT
and madeira-d3d12 are compiled. Keeping this hook here makes a clean CI checkout
receive the exact same fixes every time rather than relying on a dirty submodule.
Build 87 retriggers CI after fixing ml1160's post-patch verifier: the marker is
intentionally present once in a source comment and once in the runtime log.
Build 88 adds ml1161 so a recovered 0x0 drawable updates the D3D12 resource
descriptor itself before allocation/bookkeeping, not only the Metal texture.
Build 89 retries ml1161 with the actual resource-creation anchor used by main.
Build 90 moves ml1161 into the function's existing resolved_desc.
Build 91 avoids InterlockedCompareExchange as an ARM64EC mingw rvalue; the
last swapchain dimensions are volatile LONGs and are read/written directly.
Build 92 fixes the ml1159 verifier itself: the marker intentionally exists in
both a source comment and the runtime log string, so substring count == 1 was
incorrect and stopped CI before madeira-d3d12 compilation even began.
"""

from pathlib import Path
import re
import subprocess
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: apply-geometry-siv-fix.py <dxbc_signature.cpp>")

path = Path(sys.argv[1])
source = path.read_text()
start = source.find("handle_signature_gs(")
if start < 0:
    raise SystemExit(f"could not find handle_signature_gs() in {path}")
end = source.find("\nvoid handle_signature(", start)
if end < 0:
    raise SystemExit(f"could not find end of handle_signature_gs() in {path}")

gs = source[start:end]
decl_start = gs.find("case D3D10_SB_OPCODE_DCL_INPUT_SIV:")
decl_end = gs.find("case D3D10_SB_OPCODE_DCL_INPUT:", decl_start)
if decl_start < 0 or decl_end < 0:
    raise SystemExit(f"could not find geometry shader input-SIV switch in {path}")

input_siv = gs[decl_start:decl_end]
has_clip = "case D3D10_SB_NAME_CLIP_DISTANCE:" in input_siv
has_cull = "case D3D10_SB_NAME_CULL_DISTANCE:" in input_siv
if has_clip and has_cull:
    print("geometry shader CLIP_DISTANCE/CULL_DISTANCE cases already present")
elif has_clip or has_cull:
    raise SystemExit("geometry shader SIV switch has only one of the required cases; refusing a partial patch")
else:
    pattern = re.compile(
        r'(switch \(siv\) \{\s*)'
        r'case D3D10_SB_NAME_POSITION:\s*break;\s*'
        r'default:\s*assert\(0 && "Unexpected/unhandled geometry shader siv"\);',
        re.S,
    )
    matches = list(pattern.finditer(input_siv))
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one known geometry shader SIV switch in {path}; found {len(matches)}")

    replacement = (
        "switch (siv) {\n"
        "    case D3D10_SB_NAME_CLIP_DISTANCE:\n"
        "    case D3D10_SB_NAME_CULL_DISTANCE:\n"
        "    case D3D10_SB_NAME_POSITION:\n"
        "      break;\n"
        "    default:\n"
        "      assert(0 && \"Unexpected/unhandled geometry shader siv\");"
    )
    input_siv = pattern.sub(replacement, input_siv, count=1)
    gs = gs[:decl_start] + input_siv + gs[decl_end:]
    source = source[:start] + gs + source[end:]
    path.write_text(source)
    print("accepted geometry shader POSITION, CLIP_DISTANCE, and CULL_DISTANCE declarations")

# Build 86..92: apply the Spider-Man D3D12/DXGI fixes to the clean checkout
# before both the native DXMT archive and madeira_d3d12 PE runtime are built.
repo_root = Path(__file__).resolve().parents[2]
patch86 = repo_root / "tools/patch-spiderman-build86.py"
subprocess.run([sys.executable, str(patch86), str(repo_root)], check=True)
