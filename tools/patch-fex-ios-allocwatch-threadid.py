#!/usr/bin/env python3
"""Use host thread identity for AllocWatch on iOS ARM64EC.

GTA V build 0.1.25 faults in FEXCore::Utils::AllocWatch::Clear()+0x14:

    ldr x10, [x18, #0x58]      // TEB->ThreadLocalStoragePointer
    ...
    ldr x9, [x10, x9, lsl #3]  // x10 == NULL -> AV READ 0

AllocWatch::CurrentThreadId() already avoids implicit Windows TLS on the iOS
WOW64 module by using TPIDRRO_EL0.  ARM64EC was explicitly excluded from that
safe path and therefore uses a C++ thread_local byte.  Some FEX/ARM64EC threads
do not have TEB->ThreadLocalStoragePointer initialized when Clear() runs.

For this diagnostic, the value only needs to be unique per host thread.  The
host pthread pointer from TPIDRRO_EL0 is exactly that and requires no Windows
TLS, so use it for every iOS Windows FEX module, including ARM64EC.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1
            else "FEX/FEXCore/Source/Utils/AllocWatch.cpp")
src = path.read_text()

old = "#if defined(FEX_IOS_HOST) && defined(_WIN32) && !defined(ARCHITECTURE_arm64ec)"
new = "#if defined(FEX_IOS_HOST) && defined(_WIN32)"

if old not in src:
    if new in src and "TPIDRRO_EL0" in src:
        print(f"{path}: AllocWatch iOS thread-id patch already present")
        raise SystemExit(0)
    raise SystemExit(f"{path}: expected CurrentThreadId preprocessor guard not found")

if src.count(old) != 1:
    raise SystemExit(f"{path}: expected one guard, found {src.count(old)}")

src = src.replace(old, new, 1)

old_comment = """    // The WOW64 module must not use implicit TLS: Wine enters it from init_wow64() on a path that
    // never sets up TEB->ThreadLocalStoragePointer, and the compiler's TLS sequence reads the TEB
    // through x18, which is not the TEB on the iOS host. TPIDRRO_EL0 (the host thread pointer) is
    // unique per thread and needs no memory access."""
new_comment = """    // iOS FEX modules must not use implicit Windows TLS here.  Some WOW64 and
    // ARM64EC entry paths have no TEB->ThreadLocalStoragePointer yet, and the
    // generated TLS sequence would dereference NULL. TPIDRRO_EL0 is the host
    // pthread pointer, is unique per thread, and needs no memory access."""

if old_comment in src:
    src = src.replace(old_comment, new_comment, 1)

path.write_text(src)
print(f"{path}: AllocWatch CurrentThreadId now uses TPIDRRO_EL0 on iOS ARM64EC too")
