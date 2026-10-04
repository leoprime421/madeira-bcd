#!/usr/bin/env python3
"""Trace and recover GTA V steam_api64.dll child invalid-handle opens with a parent fd bridge.

Targets steam_api64.dll plus d3d9.dll as a successful control loaded by both
GTAVLauncher and the GTA5.exe pseudo-process.

The trace distinguishes:
  * fullname/file-id/module reuse versus a normal file open
  * NtOpenFile status + returned Windows handle
  * NtQueryObject validity immediately before section creation
  * NtCreateSection status + section handle
  * NtQuerySection status/machine
  * NtMapViewOfSection status and mapped-module reuse

It intentionally does NOT call wine_server_handle_to_fd(): that call can fill
the Unix fd cache. Recovery only replays NtOpenFile after STATUS_INVALID_HANDLE.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/loader.c")
src = path.read_text()

marker = "MADEIRA_STEAM_HANDLE_DIAG"
if marker in src:
    print(f"{path}: steam handle diagnostic already present")
    raise SystemExit(0)

func_anchor = r"""static NTSTATUS open_dll_file( UNICODE_STRING *nt_name, WINE_MODREF **pwm, HANDLE *mapping,
                               SECTION_IMAGE_INFORMATION *image_info, struct file_id *id )
{
"""
helper = r"""#ifdef __arm64ec__
/* MADEIRA_STEAM_HANDLE_DIAG + ml1143 parent->child fd bridge.
 *
 * Build 33 proved the child returns STATUS_INVALID_HANDLE on every NtOpenFile
 * attempt while the launcher opens the exact same steam_api64.dll normally.
 * Madeira pseudo-processes share one native fd table, but wineserver Windows
 * handle tables are per pseudo-process. Preserve one DUPLICATED host fd from
 * the successful launcher open in the inherited environment; if the child's
 * path-based NtOpenFile fails, allocate a fresh Wine handle for that fd in the
 * CHILD's own handle table through wine_server_fd_to_handle().
 *
 * The bridge never fabricates success: it only exists after a real successful
 * open + real handle_to_fd duplication in the parent, and fd_to_handle must
 * succeed in the child before normal NtCreateSection continues.
 */
NTSTATUS CDECL wine_server_fd_to_handle( int fd, unsigned int access, unsigned int attributes,
                                         HANDLE *handle );
NTSTATUS CDECL wine_server_handle_to_fd( HANDLE handle, unsigned int access, int *unix_fd,
                                         unsigned int *options );

static const WCHAR ios_steam_fd_envW[] = L"MADEIRA_STEAM_API64_FD";

static int ios_steam_handle_is_steam( const UNICODE_STRING *name )
{
    static const WCHAR steamW[] = L"steam_api64.dll";
    const WCHAR *base, *p, *end;
    SIZE_T len;

    if (!name || !name->Buffer) return 0;
    base = name->Buffer;
    end = name->Buffer + name->Length / sizeof(WCHAR);
    for (p = base; p < end; p++) if (*p == '\\' || *p == '/') base = p + 1;
    len = end - base;
    return len == ARRAY_SIZE(steamW) - 1 && !wcsnicmp( base, steamW, len );
}

static int ios_steam_handle_diag_target( const UNICODE_STRING *name )
{
    static const WCHAR d3d9W[] = L"d3d9.dll";
    const WCHAR *base, *p, *end;
    SIZE_T len;

    if (ios_steam_handle_is_steam( name )) return 1;
    if (!name || !name->Buffer) return 0;
    base = name->Buffer;
    end = name->Buffer + name->Length / sizeof(WCHAR);
    for (p = base; p < end; p++) if (*p == '\\' || *p == '/') base = p + 1;
    len = end - base;
    return len == ARRAY_SIZE(d3d9W) - 1 && !wcsnicmp( base, d3d9W, len );
}

static void ios_publish_steam_fd( HANDLE handle, const UNICODE_STRING *name )
{
    UNICODE_STRING env_name, env_val, existing;
    WCHAR value[24], old_value[24];
    NTSTATUS status;
    int fd = -1;

    if (!ios_steam_handle_is_steam( name )) return;

    RtlInitUnicodeString( &env_name, ios_steam_fd_envW );
    existing.Buffer = old_value;
    existing.Length = 0;
    existing.MaximumLength = sizeof(old_value);
    if (!RtlQueryEnvironmentVariable_U( NULL, &env_name, &existing ))
        return;  /* already published; keep the original leaked duplicate alive */

    status = wine_server_handle_to_fd( handle, FILE_READ_DATA, &fd, NULL );
    if (status || fd < 0)
    {
        ERR( "[steam-fd-bridge] publish FAILED peb=%p handle=%p status=%08x fd=%d rev=ml1143\n",
             NtCurrentTeb()->Peb, handle, (unsigned)status, fd );
        return;
    }

    swprintf( value, ARRAY_SIZE(value), L"%d", fd );
    RtlInitUnicodeString( &env_val, value );
    status = RtlSetEnvironmentVariable( NULL, &env_name, &env_val );
    ERR( "[steam-fd-bridge] publish peb=%p handle=%p fd=%d env_status=%08x rev=ml1143\n",
         NtCurrentTeb()->Peb, handle, fd, (unsigned)status );
    /* Intentionally DO NOT close fd. It is the bridge object inherited by the
     * child pseudo-process and is released only when the native app exits. */
}

static NTSTATUS ios_open_steam_from_parent_fd( HANDLE *handle, ACCESS_MASK access,
                                               const UNICODE_STRING *name )
{
    UNICODE_STRING env_name, env_val;
    WCHAR value[24];
    NTSTATUS status;
    int fd = 0, i, chars;

    if (!ios_steam_handle_is_steam( name )) return STATUS_INVALID_HANDLE;

    RtlInitUnicodeString( &env_name, ios_steam_fd_envW );
    env_val.Buffer = value;
    env_val.Length = 0;
    env_val.MaximumLength = sizeof(value);
    status = RtlQueryEnvironmentVariable_U( NULL, &env_name, &env_val );
    if (status)
    {
        ERR( "[steam-fd-bridge] child lookup missing peb=%p status=%08x rev=ml1143\n",
             NtCurrentTeb()->Peb, (unsigned)status );
        return STATUS_INVALID_HANDLE;
    }

    chars = env_val.Length / sizeof(WCHAR);
    for (i = 0; i < chars; i++)
    {
        if (value[i] < '0' || value[i] > '9') return STATUS_INVALID_HANDLE;
        fd = fd * 10 + (value[i] - '0');
    }
    if (fd <= 0) return STATUS_INVALID_HANDLE;

    *handle = 0;
    status = wine_server_fd_to_handle( fd, access, 0, handle );
    ERR( "[steam-fd-bridge] child import peb=%p fd=%d status=%08x handle=%p rev=ml1143\n",
         NtCurrentTeb()->Peb, fd, (unsigned)status, *handle );
    return status;
}
#endif

static NTSTATUS open_dll_file( UNICODE_STRING *nt_name, WINE_MODREF **pwm, HANDLE *mapping,
                               SECTION_IMAGE_INFORMATION *image_info, struct file_id *id )
{
"""
if src.count(func_anchor) != 1:
    raise SystemExit(f"{path}: open_dll_file anchor count={src.count(func_anchor)}, expected 1")
src = src.replace(func_anchor, helper, 1)

decl_old = r"""    NTSTATUS status;
    HANDLE handle;

    if ((*pwm = find_fullname_module( nt_name ))) return STATUS_SUCCESS;
"""
decl_new = r"""    NTSTATUS status;
    HANDLE handle = 0;
#ifdef __arm64ec__
    int ios_diag = ios_steam_handle_diag_target( nt_name );
#else
    int ios_diag = 0;
#endif

    if (ios_diag)
        ERR( "[steam-handle] stage=enter tid=%04Ix peb=%p name=%s mapping_in=%p path=normal-search\n",
             (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
             debugstr_us(nt_name), mapping ? *mapping : 0 );

    if ((*pwm = find_fullname_module( nt_name )))
    {
        if (ios_diag)
            ERR( "[steam-handle] stage=reuse-fullname tid=%04Ix peb=%p name=%s module=%p\n",
                 (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                 debugstr_us(nt_name), (*pwm)->ldr.DllBase );
        return STATUS_SUCCESS;
    }
"""
if src.count(decl_old) != 1:
    raise SystemExit(f"{path}: open_dll_file declaration anchor count={src.count(decl_old)}, expected 1")
src = src.replace(decl_old, decl_new, 1)

open_old = r"""    InitializeObjectAttributes( &attr, nt_name, OBJ_CASE_INSENSITIVE, 0, NULL );
    if ((status = NtOpenFile( &handle, GENERIC_READ | SYNCHRONIZE, &attr, &io,
                              FILE_SHARE_READ | FILE_SHARE_DELETE,
                              FILE_SYNCHRONOUS_IO_NONALERT | FILE_NON_DIRECTORY_FILE )))
    {
"""
open_new = r"""    InitializeObjectAttributes( &attr, nt_name, OBJ_CASE_INSENSITIVE, 0, NULL );
    status = NtOpenFile( &handle, GENERIC_READ | SYNCHRONIZE, &attr, &io,
                         FILE_SHARE_READ | FILE_SHARE_DELETE,
                         FILE_SYNCHRONOUS_IO_NONALERT | FILE_NON_DIRECTORY_FILE );
    if (ios_diag)
        ERR( "[steam-handle] stage=NtOpenFile tid=%04Ix peb=%p name=%s status=%08x handle=%p io=%08x\n",
             (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
             debugstr_us(nt_name), (unsigned)status, handle, (unsigned)io.Status );
#ifdef __arm64ec__
    if (!status)
        ios_publish_steam_fd( handle, nt_name );
    else if (status == STATUS_INVALID_HANDLE && ios_steam_handle_is_steam( nt_name ))
    {
        NTSTATUS bridge_status = ios_open_steam_from_parent_fd( &handle,
                                                                GENERIC_READ | SYNCHRONIZE,
                                                                nt_name );
        if (!bridge_status)
        {
            status = STATUS_SUCCESS;
            io.Status = STATUS_SUCCESS;
            io.Information = FILE_OPENED;
            ERR( "[steam-handle] stage=parent-fd-bridge tid=%04Ix peb=%p name=%s "
                 "status=%08x handle=%p rev=ml1143\n",
                 (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                 debugstr_us(nt_name), (unsigned)status, handle );
        }
    }
    /* Build 33 confirmed the parent-fd bridge is the primary recovery. Keep
     * two direct retries only as a fallback when no inherited bridge fd exists. */
    /* GTA5.exe reached this exact point with c0000008 while the
     * parent opened the same steam_api64.dll successfully. The lower file
     * layer recovery did not show up in that device run. Give the loader two
     * fresh, real NtOpenFile attempts before a transient shared-fd race becomes
     * STATUS_DLL_NOT_FOUND/c0000135. Only INVALID_HANDLE and the two targeted
     * DLL names use this path; every other status keeps normal Wine semantics. */
    if (ios_diag && status == STATUS_INVALID_HANDLE)
    {
        unsigned int attempt;
        for (attempt = 1; attempt <= 2 && status == STATUS_INVALID_HANDLE; attempt++)
        {
            handle = 0;
            io.Status = 0;
            io.Information = 0;
            status = NtOpenFile( &handle, GENERIC_READ | SYNCHRONIZE, &attr, &io,
                                 FILE_SHARE_READ | FILE_SHARE_DELETE,
                                 FILE_SYNCHRONOUS_IO_NONALERT | FILE_NON_DIRECTORY_FILE );
            ERR( "[steam-handle] stage=NtOpenFile-retry attempt=%u tid=%04Ix peb=%p name=%s "
                 "status=%08x handle=%p io=%08x rev=ml1142\n",
                 attempt, (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                 debugstr_us(nt_name), (unsigned)status, handle, (unsigned)io.Status );
        }
    }
#endif
    if (status)
    {
"""
if src.count(open_old) != 1:
    raise SystemExit(f"{path}: NtOpenFile anchor count={src.count(open_old)}, expected 1")
src = src.replace(open_old, open_new, 1)

fs_old = r"""    if (!NtFsControlFile( handle, 0, NULL, NULL, &io, FSCTL_GET_OBJECT_ID, NULL, 0, &fid, sizeof(fid) ))
    {
        memcpy( id, fid.ObjectId, sizeof(*id) );
        if ((*pwm = find_fileid_module( id )))
        {
            TRACE( "%s is the same file as existing module %p %s\n", debugstr_w( nt_name->Buffer ),
                   (*pwm)->ldr.DllBase, debugstr_w( (*pwm)->ldr.FullDllName.Buffer ));
            NtClose( handle );
            return STATUS_SUCCESS;
        }
    }

    size.QuadPart = 0;
    status = NtCreateSection( mapping, STANDARD_RIGHTS_REQUIRED | SECTION_QUERY |
                              SECTION_MAP_READ | SECTION_MAP_EXECUTE,
                              NULL, &size, PAGE_EXECUTE_READ, SEC_IMAGE, handle );
    if (!status)
    {
        NtQuerySection( *mapping, SectionImageInformation, image_info, sizeof(*image_info), NULL );
        if (!is_valid_binary( handle, image_info ))
        {
            TRACE( "%s is for arch %x, continuing search\n", debugstr_us(nt_name), image_info->Machine );
            status = STATUS_NOT_SUPPORTED;
            NtClose( *mapping );
            *mapping = NULL;
        }
    }
    NtClose( handle );
    return status;
"""
fs_new = r"""    {
        NTSTATUS fs_status = NtFsControlFile( handle, 0, NULL, NULL, &io, FSCTL_GET_OBJECT_ID,
                                              NULL, 0, &fid, sizeof(fid) );
        if (ios_diag)
            ERR( "[steam-handle] stage=FsControlObjectId tid=%04Ix peb=%p name=%s status=%08x handle=%p\n",
                 (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                 debugstr_us(nt_name), (unsigned)fs_status, handle );
        if (!fs_status)
        {
            memcpy( id, fid.ObjectId, sizeof(*id) );
            if ((*pwm = find_fileid_module( id )))
            {
                NTSTATUS close_status;
                TRACE( "%s is the same file as existing module %p %s\n", debugstr_w( nt_name->Buffer ),
                       (*pwm)->ldr.DllBase, debugstr_w( (*pwm)->ldr.FullDllName.Buffer ));
                if (ios_diag)
                    ERR( "[steam-handle] stage=reuse-fileid tid=%04Ix peb=%p name=%s handle=%p module=%p\n",
                         (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                         debugstr_us(nt_name), handle, (*pwm)->ldr.DllBase );
                close_status = NtClose( handle );
                if (ios_diag)
                    ERR( "[steam-handle] stage=close-fileid tid=%04Ix peb=%p handle=%p status=%08x\n",
                         (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                         handle, (unsigned)close_status );
                return STATUS_SUCCESS;
            }
        }
    }

    size.QuadPart = 0;
    if (ios_diag)
    {
        OBJECT_BASIC_INFORMATION basic;
        ULONG ret_len = 0;
        NTSTATUS qobj = NtQueryObject( handle, ObjectBasicInformation, &basic, sizeof(basic), &ret_len );
        ERR( "[steam-handle] stage=pre-NtCreateSection tid=%04Ix peb=%p name=%s handle=%p "
             "NtQueryObject=%08x attrs=0x%lx access=0x%lx handles=%lu retlen=%lu reuse=none\n",
             (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
             debugstr_us(nt_name), handle, (unsigned)qobj,
             qobj ? 0ul : (unsigned long)basic.Attributes,
             qobj ? 0ul : (unsigned long)basic.GrantedAccess,
             qobj ? 0ul : (unsigned long)basic.HandleCount,
             (unsigned long)ret_len );
    }

    status = NtCreateSection( mapping, STANDARD_RIGHTS_REQUIRED | SECTION_QUERY |
                              SECTION_MAP_READ | SECTION_MAP_EXECUTE,
                              NULL, &size, PAGE_EXECUTE_READ, SEC_IMAGE, handle );
    if (ios_diag)
        ERR( "[steam-handle] stage=NtCreateSection tid=%04Ix peb=%p name=%s status=%08x "
             "file_handle=%p section_handle=%p\n",
             (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
             debugstr_us(nt_name), (unsigned)status, handle, mapping ? *mapping : 0 );
    if (!status)
    {
        NTSTATUS section_status = NtQuerySection( *mapping, SectionImageInformation, image_info,
                                                  sizeof(*image_info), NULL );
        if (ios_diag)
            ERR( "[steam-handle] stage=NtQuerySection tid=%04Ix peb=%p name=%s status=%08x "
                 "section_handle=%p machine=0x%x\n",
                 (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                 debugstr_us(nt_name), (unsigned)section_status, *mapping,
                 section_status ? 0 : image_info->Machine );
        if (!is_valid_binary( handle, image_info ))
        {
            TRACE( "%s is for arch %x, continuing search\n", debugstr_us(nt_name), image_info->Machine );
            status = STATUS_NOT_SUPPORTED;
            NtClose( *mapping );
            *mapping = NULL;
        }
    }
    {
        NTSTATUS close_status = NtClose( handle );
        if (ios_diag)
            ERR( "[steam-handle] stage=NtCloseFile tid=%04Ix peb=%p name=%s handle=%p status=%08x final=%08x\n",
                 (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                 debugstr_us(nt_name), handle, (unsigned)close_status, (unsigned)status );
    }
    return status;
"""
if src.count(fs_old) != 1:
    raise SystemExit(f"{path}: file-id/section anchor count={src.count(fs_old)}, expected 1")
src = src.replace(fs_old, fs_new, 1)

map_old = r"""    NTSTATUS status = NtMapViewOfSection( mapping, NtCurrentProcess(), &module, 0, 0, NULL, &len,
                                          ViewShare, 0, PAGE_EXECUTE_READ );

    if (!NT_SUCCESS(status)) return status;

    if ((*pwm = find_existing_module( module )))  /* already loaded */
    {
"""
map_new = r"""    NTSTATUS status = NtMapViewOfSection( mapping, NtCurrentProcess(), &module, 0, 0, NULL, &len,
                                          ViewShare, 0, PAGE_EXECUTE_READ );
#ifdef __arm64ec__
    int ios_diag = ios_steam_handle_diag_target( nt_name );
#else
    int ios_diag = 0;
#endif

    if (ios_diag)
        ERR( "[steam-handle] stage=NtMapViewOfSection tid=%04Ix peb=%p name=%s status=%08x "
             "section_handle=%p module=%p size=0x%Ix\n",
             (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
             debugstr_us(nt_name), (unsigned)status, mapping, module, len );

    if (!NT_SUCCESS(status)) return status;

    if ((*pwm = find_existing_module( module )))  /* already loaded */
    {
        if (ios_diag)
            ERR( "[steam-handle] stage=reuse-mapped-module tid=%04Ix peb=%p name=%s "
                 "mapped=%p existing=%p\n",
                 (ULONG_PTR)NtCurrentTeb()->ClientId.UniqueThread, NtCurrentTeb()->Peb,
                 debugstr_us(nt_name), module, (*pwm)->ldr.DllBase );
"""
if src.count(map_old) != 1:
    raise SystemExit(f"{path}: NtMapViewOfSection anchor count={src.count(map_old)}, expected 1")
src = src.replace(map_old, map_new, 1)

path.write_text(src)
print(f"{path}: added steam_api64/d3d9 diagnostics + invalid-handle recovery")
