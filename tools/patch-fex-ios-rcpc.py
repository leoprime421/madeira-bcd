#!/usr/bin/env python3
"""Let a game opt out of FEAT_LRCPC in the iOS ARM64EC FEX module.

The iOS branch of FEX::Windows::CPUFeatures::FetchHostFeatures synthesizes
HostFeatures instead of reading the normal host-feature override.  On devices
that advertise FEAT_LRCPC, that makes x86 acquire polls lower to LDAPRB.  FH4
can then remain in a tight poll when the byte is updated by another translated
thread.  Madeira sets MADEIRA_FEX_NO_RCPC=1 only for FH4; keep the default
feature set for every other title.

Build trigger note: ml1156 splits oversized recycled JIT tail carves in
patch-ntdll-jit-alias-release.py, so a tiny FEX request no longer consumes an
entire larger free carve. This file is in build-ipa.yml's push filter, so this
revision intentionally starts the IPA build containing that fix.
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: MADEIRA_FEX_NO_RCPC"
if marker in src:
    print("already patched")
    sys.exit(0)

anchor = "  HostFeatures.SupportsRCPC = true;\n"
if src.count(anchor) != 1:
    sys.exit("patch-fex-ios-rcpc: SupportsRCPC anchor not found exactly once")

replacement = anchor + """  /* madeira-bcd: MADEIRA_FEX_NO_RCPC=1 opts this launch out of
   * FEAT_LRCPC. The iOS ARM64EC path otherwise emits LDAPRB for x86 acquire
   * polls; FH4's startup poll needs the stronger LDAR/TSO path. */
  if (const char* NoRCPC = getenv("MADEIRA_FEX_NO_RCPC");
      NoRCPC && NoRCPC[0] == '1') {
    HostFeatures.SupportsRCPC = false;
  }
"""
src = src.replace(anchor, replacement, 1)

if "#include <cstdlib>" not in src:
    include = "#include <windows.h>\n"
    if src.count(include) != 1:
        sys.exit("patch-fex-ios-rcpc: include anchor not found")
    src = src.replace(include, include + "#include <cstdlib>\n", 1)

open(path, "w").write(src)
print("patched " + path)
