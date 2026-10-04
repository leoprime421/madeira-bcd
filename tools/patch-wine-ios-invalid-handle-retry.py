#!/usr/bin/env python3
"""Recover transient STATUS_INVALID_HANDLE on iOS file opens.

Madeira runs wineserver and Wine clients as threads in one native process.
Unlike upstream Wine, all of them share the host fd table. If a cached client
fd is closed just as the server reuses that number, a normal FILE_OPEN can
transiently surface STATUS_INVALID_HANDLE even though the path exists.

This patch does NOT turn failure into success. It retries the real path
translation / create_file request once, only for plain FILE_OPEN operations.
All other NTSTATUS values keep their original semantics.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/unix/file.c")
src = path.read_text()

marker = "MADEIRA_IOS_INVALID_HANDLE_RETRY"
if marker in src:
    print(f"{path}: invalid-handle retry already present")
    raise SystemExit(0)

anchor1 = r"""    else status = get_nt_and_unix_names( &new_attr, &nt_name, &unix_name, disposition,
                                         options & FILE_OPEN_REPARSE_POINT );

    if (status == STATUS_BAD_DEVICE_TYPE)
"""
repl1 = r"""    else status = get_nt_and_unix_names( &new_attr, &nt_name, &unix_name, disposition,
                                         options & FILE_OPEN_REPARSE_POINT );

#ifdef WINE_IOS
    /* MADEIRA_IOS_INVALID_HANDLE_RETRY
     * An absolute FILE_OPEN should not become permanently invalid merely
     * because one host descriptor number raced with an in-process close.
     * Rebuild translation state once; do not retry semantic failures. */
    if (status == STATUS_INVALID_HANDLE && disposition == FILE_OPEN && !attr->RootDirectory)
    {
        free( unix_name );
        unix_name = NULL;
        free( nt_name.Buffer );
        nt_name.Buffer = NULL;
        new_attr = *attr;
        status = get_nt_and_unix_names( &new_attr, &nt_name, &unix_name, disposition,
                                        options & FILE_OPEN_REPARSE_POINT );
        dprintf( 2, "[ios-open-retry] stage=translate tid=%04x status=%08x name=%s rev=ml1140\n",
                 (unsigned int)GetCurrentThreadId(), (unsigned int)status,
                 debugstr_us(attr->ObjectName) );
    }
#endif

    if (status == STATUS_BAD_DEVICE_TYPE)
"""
if src.count(anchor1) != 1:
    raise SystemExit(f"{path}: translation anchor count={src.count(anchor1)}, expected 1")
src = src.replace(anchor1, repl1, 1)

anchor2 = r"""        name_hidden = is_hidden_file( unix_name );
        status = open_unix_file( handle, unix_name, access, &new_attr, attributes,
                                 sharing, disposition, options, ea_buffer, ea_length );
#ifdef WINE_IOS
        /* ml487 (#78): remember which handles are steamui *.js so NtReadFile can
"""
repl2 = r"""        name_hidden = is_hidden_file( unix_name );
        status = open_unix_file( handle, unix_name, access, &new_attr, attributes,
                                 sharing, disposition, options, ea_buffer, ea_length );
#ifdef WINE_IOS
        /* Madeira's in-process wineserver shares the native fd table with all
         * pseudo-processes. Build 32 proved that STATUS_INVALID_HANDLE can escape
         * the first recovery on GTA5.exe's steam_api64.dll open. At this point
         * path translation already succeeded, so replay the real create_file
         * request up to twice regardless of RootDirectory/stat shape. A genuinely
         * invalid root/handle simply returns the same status; no success is
         * fabricated and all access/share/options are preserved. */
        if (status == STATUS_INVALID_HANDLE && disposition == FILE_OPEN)
        {
            unsigned int attempt;
            for (attempt = 1; attempt <= 2 && status == STATUS_INVALID_HANDLE; attempt++)
            {
                *handle = 0;
                status = open_unix_file( handle, unix_name, access, &new_attr, attributes,
                                         sharing, disposition, options, ea_buffer, ea_length );
                dprintf( 2, "[ios-open-retry] stage=create attempt=%u tid=%04x status=%08x "
                         "handle=%p root=%p unix=%s rev=ml1141\n",
                         attempt, (unsigned int)GetCurrentThreadId(), (unsigned int)status,
                         *handle, new_attr.RootDirectory, unix_name );
            }
        }

        /* ml487 (#78): remember which handles are steamui *.js so NtReadFile can
"""
if src.count(anchor2) != 1:
    raise SystemExit(f"{path}: create_file anchor count={src.count(anchor2)}, expected 1")
src = src.replace(anchor2, repl2, 1)

path.write_text(src)
print(f"{path}: added one-shot iOS STATUS_INVALID_HANDLE recovery")
