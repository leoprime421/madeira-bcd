#!/usr/bin/env python3
"""Patch pinned Wine sources needed by Madeira's iOS compatibility layer.

1. Fix Wine's ARM64EC NtProtectVirtualMemory executable-request early return.
2. Force FH4's Xbox realtime endpoint offline inside WinHTTP, before DNS/TLS.
3. Backport Wine's VKD3D-backed D3DReflect and rebuild ARM64EC D3DCompiler.

The FH4 gate is deliberately narrow: it only applies when the current Windows
image basename is ForzaHorizon4.exe and the WinHttpConnect hostname is exactly
rta.xboxlive.com. Other Xbox/Microsoft traffic and other titles are untouched.

The D3DReflect backport follows upstream Wine commit
815fe9deb15b2cc742a243166316a1d33c6a03e0. It replaces the old reflection
parser hit by Ace Combat 7's TrueSky plugin instead of masking the final AV.
"""
from pathlib import Path
import subprocess
import sys

# ---------------------------------------------------------------------------
# ARM64EC executable-request callback-state fix.
# ---------------------------------------------------------------------------
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
else:
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
                    ERR( "[exec-req-leave-source] cleared callback after executable protect\\n" );
            }
            return st;"""
    function = function.replace(old, new, 1)
    path.write_text(source[:start] + function + source[end:], encoding="utf-8")
    print("exec-req leave: patched", path)

# ---------------------------------------------------------------------------
# FH4 Xbox RTA offline gate.
#
# The modified/offline FH4 build uses a local Xbox/Goldberg identity, but the
# title still opens real WinHTTP realtime connections to rta.xboxlive.com.
# Those requests reach Microsoft's websocket upgrade path and can leave the
# game waiting indefinitely at Continue. Do not forge a successful Xbox auth
# response; make only this realtime endpoint deterministically unavailable.
# ---------------------------------------------------------------------------
winhttp_path = Path("wine/dlls/winhttp/session.c")
winhttp = winhttp_path.read_text(encoding="utf-8")
rta_marker = "/* madeira-bcd: FH4-only Xbox RTA offline gate */"

if rta_marker in winhttp:
    if "[fh4-rta-offline]" not in winhttp:
        raise SystemExit("FH4 RTA offline: source marker found without diagnostic")
    print("FH4 RTA offline: already patched")
else:
    anchor = """    if (!server)
    {
        SetLastError( ERROR_INVALID_PARAMETER );
        return NULL;
    }
"""
    if winhttp.count(anchor) != 1:
        raise SystemExit("FH4 RTA offline: WinHttpConnect server-validation anchor changed")

    gate = anchor + """    /* madeira-bcd: FH4-only Xbox RTA offline gate */
    {
        WCHAR image[MAX_PATH];
        const WCHAR *basename = image;
        DWORD len = GetModuleFileNameW( NULL, image, MAX_PATH );

        if (len && len < MAX_PATH)
        {
            const WCHAR *p = image + len;
            while (p > image && p[-1] != '\\\\' && p[-1] != '/') p--;
            basename = p;

            if (!lstrcmpiW( basename, L\"ForzaHorizon4.exe\" ) &&
                !lstrcmpiW( server, L\"rta.xboxlive.com\" ))
            {
                WARN( \"[fh4-rta-offline] blocked WinHttpConnect to %s before DNS/TLS\\n\",
                      debugstr_w(server) );
                SetLastError( ERROR_WINHTTP_NAME_NOT_RESOLVED );
                return NULL;
            }
        }
    }
"""
    winhttp_path.write_text(winhttp.replace(anchor, gate, 1), encoding="utf-8")
    print("FH4 RTA offline: patched", winhttp_path)

# ---------------------------------------------------------------------------
# Ace Combat 7 / TrueSky D3DReflect backport.
#
# Build 170 reaches TrueSky's D3DReflect call, then the pinned Wine parser emits
# a large burst of shader-reflection partial stubs before native formatting
# faults while the guest is still inside TrueSky. Wine upstream replaced that
# parser with the VKD3D reflection implementation. Apply the exact backport and
# rebuild the ARM64EC DLLs now; build-arm64ec is restored before this CI step.
# ---------------------------------------------------------------------------
helper = Path(__file__).with_name("patch-build-wine-d3dreflect-vkd3d.py")
subprocess.run([sys.executable, str(helper)], check=True)
