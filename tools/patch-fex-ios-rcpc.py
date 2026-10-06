#!/usr/bin/env python3
"""Let a game opt out of FEAT_LRCPC in the iOS ARM64EC FEX module.

The iOS branch of FEX::Windows::CPUFeatures::FetchHostFeatures synthesizes
HostFeatures instead of reading the normal host-feature override.  On devices
that advertise FEAT_LRCPC, that makes x86 acquire polls lower to LDAPRB.  FH4
can then remain in a tight poll when the byte is updated by another translated
thread.  Madeira sets MADEIRA_FEX_NO_RCPC=1 only for FH4; keep the default
feature set for every other title.

Build trigger note: Build 85 carries ml1158, which routes late PE/JIT aliases
to the FEX emulator owned by the PEB that actually mapped the image. Build 84
Spider-Man showed wintrust.dll mapped by the parent being pushed into the
crash-handler child's alias table; the parent then saw its pool-copy RIP as
NOEXEC. The implementation is applied by patch-ntdll-jit-alias-release.py;
this file remains the filtered CI trigger so the IPA contains that fix.
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
