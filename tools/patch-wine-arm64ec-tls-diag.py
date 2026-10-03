#!/usr/bin/env python3
"""Instrument ARM64EC bootstrap TLS state without changing TLS lifetime.

Prints the current TEB/PEB TLS state at the two points needed to compare the
main process and an iOS pseudo-process child:
  1. immediately after load_dll(xtajit64.dll)
  2. immediately before arm64ec_process_init()

This is intentionally diagnostics-only.  Do not add ensure_thread_tls() here;
the device log must first prove whether parent and child actually differ.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/loader.c")
src = path.read_text()

marker = "MADEIRA_EC_TLS_DIAG"
if marker in src:
    print(f"{path}: ARM64EC TLS diagnostic already present")
    raise SystemExit(0)

anchor = """#ifdef __arm64ec__

static void load_arm64ec_module(void)
"""
helper = r"""#ifdef __arm64ec__

/* MADEIRA_EC_TLS_DIAG
 * Diagnostic only: compare the main ARM64EC process with an iOS pseudo-process
 * child before changing TLS initialization order. */
static void ios_arm64ec_tls_diag( const char *stage )
{
    TEB *teb = NtCurrentTeb();
    PEB *peb = teb ? teb->Peb : NULL;
    unsigned int set = 0, i;

    if (peb)
    {
        for (i = 0; i < sizeof(peb->TlsBitmapBits) / sizeof(peb->TlsBitmapBits[0]); i++)
        {
            ULONG v = peb->TlsBitmapBits[i];
            while (v) { set += v & 1; v >>= 1; }
        }
    }

    ERR( "[ec-tls-diag] stage=%s tid=%04Ix teb=%p teb_tls=%p peb=%p "
         "tls_bitmap=%p tls_module_count=%u bitmap_set=%u bits=%08lx/%08lx\n",
         stage,
         teb ? (ULONG_PTR)teb->ClientId.UniqueThread : 0,
         teb,
         teb ? (void *)teb->ThreadLocalStoragePointer : NULL,
         peb,
         peb ? peb->TlsBitmap : NULL,
         tls_module_count,
         set,
         peb ? (unsigned long)peb->TlsBitmapBits[0] : 0,
         peb ? (unsigned long)peb->TlsBitmapBits[1] : 0 );
}

static void load_arm64ec_module(void)
"""
if src.count(anchor) != 1:
    raise SystemExit(f"{path}: load_arm64ec_module anchor count={src.count(anchor)}, expected 1")
src = src.replace(anchor, helper, 1)

after_load = r"""    if ((status = load_dll( NULL, module, 0, &wm, FALSE )))
    {
        ERR( "could not load %s, status %lx\n", debugstr_w(module), status );
        NtTerminateProcess( GetCurrentProcess(), status );
    }

    /* Phase 1: set up dispatcher pointers and FEX function pointers BEFORE
"""
after_load_new = r"""    if ((status = load_dll( NULL, module, 0, &wm, FALSE )))
    {
        ERR( "could not load %s, status %lx\n", debugstr_w(module), status );
        NtTerminateProcess( GetCurrentProcess(), status );
    }

    ios_arm64ec_tls_diag( "post-load-dll-xtajit64" );

    /* Phase 1: set up dispatcher pointers and FEX function pointers BEFORE
"""
if src.count(after_load) != 1:
    raise SystemExit(f"{path}: post-load anchor count={src.count(after_load)}, expected 1")
src = src.replace(after_load, after_load_new, 1)

pre_init = r"""    /* Phase 2: invoke FEX's ProcessInit/ThreadInit. */
    ERR( "load_arm64ec_module: about to call arm64ec_process_init\n" );
"""
pre_init_new = r"""    /* Phase 2: invoke FEX's ProcessInit/ThreadInit. */
    ios_arm64ec_tls_diag( "pre-arm64ec-process-init" );
    ERR( "load_arm64ec_module: about to call arm64ec_process_init\n" );
"""
if src.count(pre_init) != 1:
    raise SystemExit(f"{path}: pre-init anchor count={src.count(pre_init)}, expected 1")
src = src.replace(pre_init, pre_init_new, 1)

path.write_text(src)
print(f"{path}: added ARM64EC TLS diagnostics (no TLS behavior change)")
