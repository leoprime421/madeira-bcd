#!/usr/bin/env python3
"""Fix Wine's ARM64EC NtProtectVirtualMemory executable-request early return.

The executable-protection probe calls enter_syscall_callback(), performs the
protect and post-notification, then used to return without its matching
leave_syscall_callback(). That leaves the callback state set and suppresses
later executable-memory notifications to FEX. Patch the pinned Wine submodule
before rebuilding its shipped ARM64EC ntdll.dll.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/signal_arm64ec.c")
source = path.read_text(encoding="utf-8")
start = source.find("NTSTATUS SYSCALL_API NtProtectVirtualMemory(")
end = source.find("\nNTSTATUS SYSCALL_API NtQuerySystemInformation", start)
if start < 0 or end < 0:
    raise SystemExit("exec-req leave: NtProtectVirtualMemory function not found")

function = source[start:end]
marker = "/* madeira-bcd: restore syscall callback state on executable-protect early return */"
if marker in function:
    if "[exec-req-leave-source]" not in function:
        raise SystemExit("exec-req leave: source marker found without diagnostic")
    print("exec-req leave: already patched")
    raise SystemExit(0)

old = """            else if (pNotifyMemoryProtect
                     && !ios_bulk_protect_suppressed( "cur-post", *addr_ptr, *size_ptr, new_prot ))
                pNotifyMemoryProtect( *addr_ptr, *size_ptr, new_prot, TRUE, st );
            return st;"""
if function.count(old) != 1:
    raise SystemExit("exec-req leave: expected early-return layout not found exactly once")

new = """            else if (pNotifyMemoryProtect
                     && !ios_bulk_protect_suppressed( "cur-post", *addr_ptr, *size_ptr, new_prot ))
                pNotifyMemoryProtect( *addr_ptr, *size_ptr, new_prot, TRUE, st );
            /* madeira-bcd: restore syscall callback state on executable-protect early return */
            leave_syscall_callback();
            {
                static int execreq_leave_logged;
                if (!execreq_leave_logged++)
                    ERR( "[exec-req-leave-source] cleared callback after executable protect\n" );
            }
            return st;"""
function = function.replace(old, new, 1)
path.write_text(source[:start] + function + source[end:], encoding="utf-8")
print("exec-req leave: patched", path)
