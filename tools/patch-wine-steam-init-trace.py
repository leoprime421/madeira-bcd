#!/usr/bin/env python3
"""Add narrow diagnostics around Steam and Social Club DLL_PROCESS_ATTACH.

The trace is intentionally diagnostic-only: it does not change a DLL return
value, Steam state, or loader semantics. While steam_api64.dll is inside its
entry point it records nested LdrLoadDll/LdrGetProcedureAddress activity, which
helps distinguish a Wine/API compatibility failure from a missing dependency.
"""
from pathlib import Path
import sys

p = Path(sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/loader.c")
s = p.read_text()

marker = "/* madeira-bcd steam-init trace rev=4 */"
if marker in s:
    print("steam-init trace: already patched")
    raise SystemExit(0)
if "/* madeira-bcd steam-init trace rev=3 */" in s:
    raise SystemExit("steam-init trace: old patch already present; start with a clean Wine checkout")

anchor = """static NTSTATUS MODULE_InitDLL( WINE_MODREF *wm, UINT reason, LPVOID lpReserved )
{
"""
if anchor not in s:
    raise SystemExit("steam-init trace: MODULE_InitDLL anchor not found")

prefix = """/* madeira-bcd steam-init trace rev=4 */
#ifdef __arm64ec__
static int steam_init_trace_depth;
static unsigned int steam_init_trace_ops;

static int steam_init_trace_module( const WINE_MODREF *wm )
{
    static const WCHAR steam_name[] = L"steam_api64.dll";
    UNICODE_STRING name, wanted;

    if (!wm || !wm->ldr.BaseDllName.Buffer) return 0;
    name = wm->ldr.BaseDllName;
    RtlInitUnicodeString( &wanted, steam_name );
    return RtlEqualUnicodeString( &name, &wanted, TRUE );
}

static int socialclub_init_trace_module( const WINE_MODREF *wm )
{
    static const WCHAR socialclub_name[] = L"socialclub.dll";
    UNICODE_STRING name, wanted;

    if (!wm || !wm->ldr.BaseDllName.Buffer) return 0;
    name = wm->ldr.BaseDllName;
    RtlInitUnicodeString( &wanted, socialclub_name );
    return RtlEqualUnicodeString( &name, &wanted, TRUE );
}
#endif

"""
s = s.replace(anchor, prefix + anchor, 1)

# Add one function-local trace flag, but keep Wine's __TRY/__EXCEPT/__ENDTRY
# sequence contiguous: MS SEH syntax does not allow statements between the
# closing __TRY brace and __EXCEPT.
locals_anchor = """    BOOL retv = FALSE;
"""
locals_new = locals_anchor + """#ifdef __arm64ec__
    int steam_trace = 0;
    int socialclub_trace = 0;
#endif
"""
if locals_anchor not in s:
    raise SystemExit("steam-init trace: locals anchor not found")
s = s.replace(locals_anchor, locals_new, 1)

old = """    __TRY
    {
        retv = call_dll_entry_point( entry, module, reason, lpReserved );
        if (!retv)
            status = STATUS_DLL_INIT_FAILED;
    }
"""
new = """#ifdef __arm64ec__
    steam_trace = (reason == DLL_PROCESS_ATTACH && steam_init_trace_module( wm ));
    socialclub_trace = (reason == DLL_PROCESS_ATTACH && socialclub_init_trace_module( wm ));
    if (socialclub_trace)
        ERR( "[socialclub-init] ENTER module=%p entry=%p reserved=%p tls=%hd flags=%08lx\\n",
             module, entry, lpReserved, wm->ldr.TlsIndex, wm->ldr.Flags );
    if (steam_trace)
    {
        steam_init_trace_depth++;
        steam_init_trace_ops = 0;
        ERR( "[steam-init] ENTER module=%p entry=%p reserved=%p tls=%hd flags=%08lx\\n",
             module, entry, lpReserved, wm->ldr.TlsIndex, wm->ldr.Flags );
    }
#endif

    __TRY
    {
        retv = call_dll_entry_point( entry, module, reason, lpReserved );
        if (!retv)
            status = STATUS_DLL_INIT_FAILED;
    }
"""
if old not in s:
    raise SystemExit("steam-init trace: entry-call anchor not found")
s = s.replace(old, new, 1)

endtry = """    __ENDTRY

    /* The state of the module list may have changed due to the call
"""
endtry_new = """    __ENDTRY

#ifdef __arm64ec__
    if (socialclub_trace)
        ERR( "[socialclub-init] LEAVE module=%p entry=%p retval=%d status=%08lx\\n",
             module, entry, retv, status );
    if (steam_trace)
    {
        ERR( "[steam-init] LEAVE module=%p entry=%p retval=%d status=%08lx ops=%u\\n",
             module, entry, retv, status, steam_init_trace_ops );
        steam_init_trace_depth--;
    }
#endif

    /* The state of the module list may have changed due to the call
"""
if endtry not in s:
    raise SystemExit("steam-init trace: __ENDTRY anchor not found")
s = s.replace(endtry, endtry_new, 1)

ldr_anchor = """NTSTATUS WINAPI DECLSPEC_HOTPATCH LdrLoadDll(LPCWSTR search_path, DWORD *load_flags,
                                             const UNICODE_STRING *libname, HMODULE* hModule)
{
"""
ldr_new = ldr_anchor + """#ifdef __arm64ec__
    int steam_trace_this_load = steam_init_trace_depth && steam_init_trace_ops++ < 160;
    if (steam_trace_this_load)
        ERR( "[steam-init] LdrLoadDll request=%s search=%s flags=%08lx\\n",
             debugstr_us(libname), debugstr_w(search_path),
             load_flags ? *load_flags : 0 );
#endif
"""
if ldr_anchor not in s:
    raise SystemExit("steam-init trace: LdrLoadDll anchor not found")
s = s.replace(ldr_anchor, ldr_new, 1)

get_anchor = """NTSTATUS WINAPI LdrGetProcedureAddress(HMODULE module, const ANSI_STRING *name,
                                       ULONG ord, PVOID *address)
{
"""
get_new = get_anchor + """#ifdef __arm64ec__
    if (steam_init_trace_depth && steam_init_trace_ops++ < 160)
        ERR( "[steam-init] LdrGetProcedureAddress module=%p name=%s ord=%lu\\n",
             module, name ? debugstr_an(name->Buffer, name->Length) : "(ordinal)", ord );
#endif
"""
if get_anchor not in s:
    raise SystemExit("steam-init trace: LdrGetProcedureAddress anchor not found")
s = s.replace(get_anchor, get_new, 1)

# Result logging: patch the concrete function bodies after their entry probes.
path_old = """        if ((nts = LdrGetDllPath( libname->Buffer, (ULONG_PTR)search_path & load_library_search_flags, &path_name, &dummy )))
            return nts;"""
path_new = """        if ((nts = LdrGetDllPath( libname->Buffer, (ULONG_PTR)search_path & load_library_search_flags, &path_name, &dummy )))
        {
#ifdef __arm64ec__
            if (steam_trace_this_load)
                ERR( "[steam-init] LdrLoadDll result=%08lx module=%p stage=path\\n", nts, NULL );
#endif
            return nts;
        }"""
if path_old not in s:
    raise SystemExit("steam-init trace: LdrGetDllPath return anchor not found")
s = s.replace(path_old, path_new, 1)

load_return_old = """    if (path_name != search_path) RtlReleasePath( path_name );
    return nts;
}
"""
load_return_new = """    if (path_name != search_path) RtlReleasePath( path_name );
#ifdef __arm64ec__
    if (steam_trace_this_load)
        ERR( "[steam-init] LdrLoadDll result=%08lx module=%p\\n",
             nts, wm ? wm->ldr.DllBase : NULL );
#endif
    return nts;
}
"""
if load_return_old not in s:
    raise SystemExit("steam-init trace: LdrLoadDll return anchor not found")
s = s.replace(load_return_old, load_return_new, 1)

get_return_old = """    RtlLeaveCriticalSection( &loader_section );
    return ret;
}
"""
get_return_new = """    RtlLeaveCriticalSection( &loader_section );
#ifdef __arm64ec__
    if (steam_init_trace_depth && steam_init_trace_ops <= 160)
        ERR( "[steam-init] LdrGetProcedureAddress result=%08lx module=%p name=%s ord=%lu address=%p\\n",
             ret, module, name ? debugstr_an(name->Buffer, name->Length) : "(ordinal)",
             ord, (ret == STATUS_SUCCESS && address) ? *address : NULL );
#endif
    return ret;
}
"""
if get_return_old not in s:
    raise SystemExit("steam-init trace: LdrGetProcedureAddress return anchor not found")
s = s.replace(get_return_old, get_return_new, 1)

p.write_text(s)
print("steam-init trace: patched", p)
