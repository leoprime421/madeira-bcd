#!/usr/bin/env python3
"""Make FEX CodeBuffer guard pages respect iOS' 16 KiB host page size.

FEX's generic code reserves only FEX_PAGE_SIZE (4 KiB) at the end of each
CodeBuffer. On iOS/ARM64, VirtualProtect works at 16 KiB host-page granularity.
Trying to protect an address aligned only to 4 KiB fails with
ERROR_INVALID_PARAMETER (87).

For FEX_IOS_HOST:
  * reserve the final 16 KiB host page logically,
  * do NOT call Win32 VirtualProtect on the JIT-pool carve (Wine reports that
    pool alias as MEM_FREE, so VirtualProtect returns ERROR_INVALID_PARAMETER),
  * report only bytes before that host page as usable,
  * exclude the same host page in IsAddressInCodeBuffer,
  * emit a capped [ios-guard] trace so CI/runtime can verify the iOS path.

The guard remains enforced by FEX's UsableSize / buffer-full checks. Other
hosts keep FEX's original physically protected 4 KiB guard page.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1] if len(sys.argv) > 1 else "FEX")
hpp = root / "FEXCore/Source/Interface/Core/CPUBackend.h"
cpp = root / "FEXCore/Source/Interface/Core/CPUBackend.cpp"

hs = hpp.read_text()
cs = cpp.read_text()

header_old = """    /// Returns the number of bytes available for storing code
    size_t UsableSize() const {
      return AllocatedSize - FEXCore::Utils::FEX_PAGE_SIZE;
    }
"""
header_new = """    /// Returns the number of bytes available for storing code
    size_t UsableSize() const {
#ifdef FEX_IOS_HOST
      // iOS arm64 uses 16 KiB VM pages. The guard must cover a full host page
      // or VirtualProtect rejects the 4 KiB-aligned address with error 87.
      constexpr size_t IOS_HOST_PAGE_SIZE = 0x4000;
      return AllocatedSize - IOS_HOST_PAGE_SIZE;
#else
      return AllocatedSize - FEXCore::Utils::FEX_PAGE_SIZE;
#endif
    }
"""
if header_new not in hs:
    if header_old not in hs:
        raise SystemExit("ios-codebuffer-guard: UsableSize anchor not found")
    hs = hs.replace(header_old, header_new, 1)

guard_old = """    // Protect the last page of the allocated buffer to trigger SIGSEGV on write access
    uintptr_t LastPageAddr = AlignDown(reinterpret_cast<uintptr_t>(Ptr) + Size - 1, FEXCore::Utils::FEX_PAGE_SIZE);
    if (!FEXCore::Allocator::VirtualProtect(reinterpret_cast<void*>(LastPageAddr), FEXCore::Utils::FEX_PAGE_SIZE,
                                            FEXCore::Allocator::ProtectOptions::None)) {
      LogMan::Msg::EFmt("Failed to mprotect last page of code buffer.");
    }
"""
guard_new = """    // Protect the last page of the allocated buffer to trigger SIGSEGV on write access.
#ifdef FEX_IOS_HOST
    // Madeira's executable CodeBuffers are carved from the app-owned dual-map
    // JIT pool. Wine's Win32 VM bookkeeping does not own that RX alias:
    // VirtualQuery reports MEM_FREE for it, so VirtualProtect(PAGE_NOACCESS)
    // returns ERROR_INVALID_PARAMETER even when address and size are 16 KiB
    // aligned. Do not send this pool carve through Win32 VirtualProtect.
    //
    // Keep a full 16 KiB logical guard instead. UsableSize() and the normal
    // buffer-full checks stop emission before this page.
    constexpr size_t GuardPageSize = 0x4000;
    uintptr_t LastPageAddr = AlignDown(reinterpret_cast<uintptr_t>(Ptr) + Size - 1, GuardPageSize);
    static std::atomic<int> IosGuardLogCount {0};
    if (IosGuardLogCount.fetch_add(1, std::memory_order_relaxed) < 8) {
      LogMan::Msg::EFmt("[ios-guard] logical-only code buffer={} size=0x{:x} guard={}+0x{:x} reason=jit-pool-not-win32-owned",
                        fmt::ptr(Ptr), Size, fmt::ptr(reinterpret_cast<void*>(LastPageAddr)), GuardPageSize);
    }
#else
    constexpr size_t GuardPageSize = FEXCore::Utils::FEX_PAGE_SIZE;
    uintptr_t LastPageAddr = AlignDown(reinterpret_cast<uintptr_t>(Ptr) + Size - 1, GuardPageSize);
    if (!FEXCore::Allocator::VirtualProtect(reinterpret_cast<void*>(LastPageAddr), GuardPageSize,
                                            FEXCore::Allocator::ProtectOptions::None)) {
      LogMan::Msg::EFmt("Failed to mprotect last page of code buffer.");
    }
#endif
"""
if guard_new not in cs:
    if guard_old not in cs:
        raise SystemExit("ios-codebuffer-guard: constructor guard anchor not found")
    cs = cs.replace(guard_old, guard_new, 1)

check_old = """      // The last page of the code buffer is protected, so we need to exclude it from the valid range
      // when checking if the address is in the code buffer.
      uintptr_t LastPageAddr = AlignDown(reinterpret_cast<uintptr_t>(Buffer.Ptr) + Buffer.AllocatedSize - 1, FEXCore::Utils::FEX_PAGE_SIZE);
      return (Address >= reinterpret_cast<uintptr_t>(Buffer.Ptr) && Address < LastPageAddr);
"""
check_new = """      // The last page of the code buffer is protected, so we need to exclude it from the valid range
      // when checking if the address is in the code buffer.
#ifdef FEX_IOS_HOST
      constexpr size_t GuardPageSize = 0x4000;
#else
      constexpr size_t GuardPageSize = FEXCore::Utils::FEX_PAGE_SIZE;
#endif
      uintptr_t LastPageAddr = AlignDown(reinterpret_cast<uintptr_t>(Buffer.Ptr) + Buffer.AllocatedSize - 1, GuardPageSize);
      return (Address >= reinterpret_cast<uintptr_t>(Buffer.Ptr) && Address < LastPageAddr);
"""
if check_new not in cs:
    if check_old not in cs:
        raise SystemExit("ios-codebuffer-guard: IsAddressInCodeBuffer anchor not found")
    cs = cs.replace(check_old, check_new, 1)

hpp.write_text(hs)
cpp.write_text(cs)
print("iOS CodeBuffer guard: 16 KiB host-page guard applied")
