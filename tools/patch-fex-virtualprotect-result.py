#!/usr/bin/env python3
"""Fix FEX's Windows VirtualProtect wrapper and add narrow failure diagnostics.

The ios-port-2607 branch currently returns (::VirtualProtect(...) == 0), which
inverts Win32 BOOL semantics: a successful VirtualProtect is reported to FEX as
false, while a failure is reported as true. It also discards the previous page
protection by passing a null output pointer.

This patch restores the Win32 contract and logs only genuine failures. It does
not relax page protections or force success.
"""
from pathlib import Path
import sys

p = Path(sys.argv[1] if len(sys.argv) > 1 else "FEX/FEXCore/include/FEXCore/Utils/AllocatorHooks.h")
s = p.read_text()

# GetLastError is declared separately from memoryapi.h in llvm-mingw.
include_anchor = "#include <memoryapi.h>\n"
if "#include <errhandlingapi.h>\n" not in s:
    if include_anchor not in s:
        raise SystemExit("FEX VirtualProtect: memoryapi include anchor not found")
    s = s.replace(include_anchor, include_anchor + "#include <errhandlingapi.h>\n", 1)

marker = "/* madeira-bcd VirtualProtect semantics fix rev=1 */"
if marker in s:
    print("FEX VirtualProtect: already patched")
    raise SystemExit(0)

old = """inline bool VirtualProtect(void* Ptr, size_t Size, ProtectOptions options) {
  DWORD prot {PAGE_NOACCESS};

  if (options == ProtectOptions::None) {
    prot = PAGE_NOACCESS;
  } else if (options == ProtectOptions::Read) {
    prot = PAGE_READONLY;
  } else if (options == (ProtectOptions::Read | ProtectOptions::Write)) {
    prot = PAGE_READWRITE;
  } else if (options == (ProtectOptions::Read | ProtectOptions::Exec)) {
    prot = PAGE_EXECUTE_READ;
  } else if (options == (ProtectOptions::Read | ProtectOptions::Write | ProtectOptions::Exec)) {
    prot = PAGE_EXECUTE_READWRITE;
  } else {
    LOGMAN_MSG_A_FMT("Unknown VirtualProtect options combination");
  }

  return ::VirtualProtect(Ptr, Size, prot, nullptr) == 0;
}
"""

new = """/* madeira-bcd VirtualProtect semantics fix rev=1 */
inline bool VirtualProtect(void* Ptr, size_t Size, ProtectOptions options) {
  DWORD prot {PAGE_NOACCESS};

  if (options == ProtectOptions::None) {
    prot = PAGE_NOACCESS;
  } else if (options == ProtectOptions::Read) {
    prot = PAGE_READONLY;
  } else if (options == (ProtectOptions::Read | ProtectOptions::Write)) {
    prot = PAGE_READWRITE;
  } else if (options == (ProtectOptions::Read | ProtectOptions::Exec)) {
    prot = PAGE_EXECUTE_READ;
  } else if (options == (ProtectOptions::Read | ProtectOptions::Write | ProtectOptions::Exec)) {
    prot = PAGE_EXECUTE_READWRITE;
  } else {
    LOGMAN_MSG_A_FMT("Unknown VirtualProtect options combination");
  }

  DWORD old_prot {};
  const BOOL ok = ::VirtualProtect(Ptr, Size, prot, &old_prot);
#ifdef FEX_IOS_HOST
  if (!ok) {
    MEMORY_BASIC_INFORMATION mbi {};
    const SIZE_T q = ::VirtualQuery(Ptr, &mbi, sizeof(mbi));
    LogMan::Msg::EFmt(
      "[fex-vprotect] FAILED ptr={} size=0x{:x} prot=0x{:x} last_error={} query={} "
      "base={} region=0x{:x} state=0x{:x} old=0x{:x} type=0x{:x}",
      fmt::ptr(Ptr), Size, prot, GetLastError(), q,
      fmt::ptr(q ? mbi.BaseAddress : nullptr), q ? mbi.RegionSize : 0,
      q ? mbi.State : 0, q ? mbi.Protect : 0, q ? mbi.Type : 0);
  }
#endif
  return ok != 0;
}
"""

if old not in s:
    raise SystemExit("FEX VirtualProtect: expected wrapper anchor not found")

s = s.replace(old, new, 1)
p.write_text(s)
print("FEX VirtualProtect: fixed Win32 BOOL semantics and enabled real-failure diagnostics")
