#!/usr/bin/env python3
"""Patch virtual_ios.c so MEM_RELEASE retires stale anonymous JIT aliases.

Observed GTA V launcher sequence:
  1. 0x7020510000+0x13fd000 is allocated RWX and receives a 0x1400000
     host-page-rounded anonymous JIT alias.
  2. The Wine view is MEM_RELEASEd.
  3. The same guest VA is reused as normal PAGE_READWRITE memory.
  4. The stale alias still matches, so set_protection() physically forces the
     new allocation back to RX and stores are routed to the dead alias.

This patch changes only alias ownership lifetime. Pool storage/reclamation is
left to the existing pool ledger.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "build/ntdll-unix/virtual_ios.c")
src = path.read_text()

helper_sig = "static int ios_jit_anon_alias_retire_release("
hook = "if (status == STATUS_SUCCESS) ios_jit_anon_alias_retire_release( base, size );"

if helper_sig in src and hook in src:
    print(f"{path}: stale alias release fix already present")
    raise SystemExit(0)
if helper_sig in src or hook in src:
    raise SystemExit(f"{path}: partial stale alias release fix found")

insert_anchor = """/* madeira-bcd: the freelist entry a request of `want` bytes takes: the
 * SMALLEST grace-expired range in reach that holds it (first of equals), or -1."""

helper = r'''/* iOS-Madeira: retire anonymous JIT aliases once the guest allocation that
 * owns them has been successfully MEM_RELEASEd.
 *
 * The anonymous alias is host-page rounded and may extend beyond Wine's exact
 * view size (GTA: 0x13fd000 -> 0x1400000 on 16 KB pages). Compare against the
 * host-rounded release end. Only aliases wholly owned by that released range
 * are tombstoned; neighbouring/partial aliases are left alone.
 *
 * The pool bytes are intentionally NOT freed here. The pool ledger owns their
 * lifetime. Lock-free alias readers use user_va as the live marker, so retain
 * the existing tombstone order: end=0, barrier, retire Mono bridge, user_va=0.
 */
static int ios_jit_anon_alias_retire_release( void *base, size_t size )
{
    uintptr_t lo = (uintptr_t)base, hi, host_hi;
    int i, retired = 0;

    if (!base || !size || size > UINTPTR_MAX - lo) return 0;
    hi = lo + size;
    host_hi = hi > UINTPTR_MAX - host_page_mask
            ? UINTPTR_MAX : (hi + host_page_mask) & ~(uintptr_t)host_page_mask;

    pthread_mutex_lock( &ios_pool_lock );
    for (i = 0; i < ios_jit_anon_alias_count; i++)
    {
        uintptr_t b = ios_jit_anon_aliases[i].user_va;
        uintptr_t e = ios_jit_anon_aliases[i].user_va_end;

        if (!b) continue;
        if (b < lo || b >= hi || e > host_hi) continue;

        dprintf( 2, "[jit-alias-release] MEM_RELEASE %p+0x%lx retires alias "
                    "[%p,%p) rw=%p rx=%p\\n",
                 base, (unsigned long)size, (void *)b, (void *)e,
                 (void *)ios_jit_anon_aliases[i].jit_rw_alias,
                 (void *)ios_jit_anon_aliases[i].jit_rx_alias );

        ios_jit_anon_aliases[i].user_va_end = 0;
        __sync_synchronize();
        ios_mono_alias_retire( b );
        ios_jit_anon_aliases[i].user_va = 0;
        retired++;
    }
    pthread_mutex_unlock( &ios_pool_lock );
    return retired;
}

'''

old_release = """    case MEM_RELEASE:
        if (!size) size = view->size;
        if (base == view->base && size == view->size)
            ios_vh_capture_free_site( view, __builtin_return_address(0) );
        status = free_pages( view, base, size );
        ios_vh_free_site.valid = 0;
        break;"""

new_release = """    case MEM_RELEASE:
        if (!size) size = view->size;
        if (base == view->base && size == view->size)
            ios_vh_capture_free_site( view, __builtin_return_address(0) );
        status = free_pages( view, base, size );
#ifdef WINE_IOS
        /* Do not let a released guest VA keep claiming ownership through an
         * anonymous JIT alias when Windows immediately reuses that address. */
        if (status == STATUS_SUCCESS) ios_jit_anon_alias_retire_release( base, size );
#endif
        ios_vh_free_site.valid = 0;
        break;"""

if src.count(insert_anchor) != 1:
    raise SystemExit(f"{path}: helper anchor count={src.count(insert_anchor)}, expected 1")
if src.count(old_release) != 1:
    raise SystemExit(f"{path}: MEM_RELEASE anchor count={src.count(old_release)}, expected 1")

src = src.replace(insert_anchor, helper + insert_anchor, 1)
src = src.replace(old_release, new_release, 1)

if src.count(helper_sig) != 1 or src.count(hook) != 1:
    raise SystemExit(f"{path}: post-patch verification failed")

path.write_text(src)
print(f"{path}: patched stale anon-JIT alias retirement on successful MEM_RELEASE")
