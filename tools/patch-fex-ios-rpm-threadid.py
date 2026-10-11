#!/usr/bin/env python3
"""Read rpmalloc's ARM64EC owner identity from Wine's published host TSD slot.

Keep TEB-valued IDs: rpmalloc also uses owner_thread as a TEB diagnostic
address. Do not substitute the pthread pointer or a hardcoded TSD offset.
The x18-relative bootstrap fallback is retained until Wine publishes the slot.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "FEX")
path = root / "External/rpmalloc/rpmalloc/rpmalloc.c"
source = path.read_text()
marker = "/* madeira-bcd: rpmalloc owner identity from host TSD */"
old = "\treturn (uintptr_t)(*(void* volatile*)((char*)NtCurrentTeb() + 0x30));"
new = """\t/* madeira-bcd: rpmalloc owner identity from host TSD */
#if defined(ARCHITECTURE_arm64ec) || defined(__arm64ec__) || defined(_M_ARM64EC)
\t/* Same dynamic offset as FEX's GetCurrentTEB, imported by ProcessInit.
\t * Reading x18 here can fault and re-enter the allocator while acquiring
\t * or releasing its recursive lock. The host TSD load needs no repair. */
\textern uint32_t IosTebTsdOffset;
\tconst uint32_t offset = IosTebTsdOffset;
\tif (offset) {
\t\tuintptr_t host_thread;
\t\t__asm__ volatile("mrs %0, TPIDRRO_EL0" : "=r"(host_thread));
\t\tconst uintptr_t teb = *(uintptr_t volatile*)((host_thread & ~(uintptr_t)7) + offset);
\t\tif (teb) return teb;
\t}
#endif
\t/* Before ProcessInit, preserve the pinned allocator's bootstrap path. */
\treturn (uintptr_t)(*(void* volatile*)((char*)NtCurrentTeb() + 0x30));"""
if marker not in source:
    if source.count(old) != 1:
        raise SystemExit("rpmalloc thread-id: expected one iOS owner-ID return")
    path.write_text(source.replace(old, new, 1))
elif new not in source:
    raise SystemExit("rpmalloc thread-id: incomplete existing patch")

module = root / "Source/Windows/ARM64EC/Module.cpp"
source = module.read_text()
anchor = '  LogMan::Msg::EFmt("[build-id] xtajit64 rev=" MADEIRA_REV " compiled " __DATE__ " " __TIME__);'
report = """
#ifdef FEX_IOS_HOST
  LogMan::Msg::EFmt("[fex-rpm-id] allocator owner identity uses host TSD offset=0x{:x}", IosTebTsdOffset);
#endif"""
if "[fex-rpm-id]" not in source:
    if source.count(anchor) != 1:
        raise SystemExit("rpmalloc thread-id: ProcessInit build marker changed")
    module.write_text(source.replace(anchor, anchor + report, 1))
elif report not in source:
    raise SystemExit("rpmalloc thread-id: incomplete ProcessInit marker")
print("rpmalloc ARM64EC: host TSD owner identity patched")
