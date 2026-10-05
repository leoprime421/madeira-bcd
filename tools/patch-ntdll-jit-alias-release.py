#!/usr/bin/env python3
"""Patch virtual_ios.c: retire stale aliases and fix iOS RWX write-drop."""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "build/ntdll-unix/virtual_ios.c")
src = path.read_text()

helper_sig = "static int ios_jit_anon_alias_retire_release("
hook = "if (status == STATUS_SUCCESS) ios_jit_anon_alias_retire_release( base, size );"

if helper_sig not in src and hook not in src:
    insert_anchor = """/* madeira-bcd: the freelist entry a request of `want` bytes takes: the
 * SMALLEST grace-expired range in reach that holds it (first of equals), or -1."""
    helper = r'''/* iOS-Madeira: retire anonymous JIT aliases once their guest allocation is released. */
static int ios_jit_anon_alias_retire_release( void *base, size_t size )
{
    uintptr_t lo = (uintptr_t)base, hi, host_hi;
    int i, retired = 0;
    if (!base || !size || size > UINTPTR_MAX - lo) return 0;
    hi = lo + size;
    host_hi = hi > UINTPTR_MAX - vm_page_mask ? UINTPTR_MAX
             : (hi + vm_page_mask) & ~(uintptr_t)vm_page_mask;
    pthread_mutex_lock( &ios_pool_lock );
    for (i = 0; i < ios_jit_anon_alias_count; i++)
    {
        uintptr_t b = ios_jit_anon_aliases[i].user_va;
        uintptr_t e = ios_jit_anon_aliases[i].user_va_end;
        if (!b || b < lo || b >= hi || e > host_hi) continue;
        dprintf( 2, "[jit-alias-release] MEM_RELEASE %p+0x%lx retires alias [%p,%p) rw=%p rx=%p\n",
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
        if (status == STATUS_SUCCESS) ios_jit_anon_alias_retire_release( base, size );
#endif
        ios_vh_free_site.valid = 0;
        break;"""
    if src.count(insert_anchor) != 1 or src.count(old_release) != 1:
        raise SystemExit("alias-release anchors changed")
    src = src.replace(insert_anchor, helper + insert_anchor, 1)
    src = src.replace(old_release, new_release, 1)
elif helper_sig not in src or hook not in src:
    raise SystemExit("partial alias-release patch found")

# FH4 Continue: Darwin may accept mprotect(RWX) but actually leave RX.  The old
# ml999 path returned success anyway, so no writable alias was registered and
# the next generated store looped forever in SIGBUS/KERN_PROTECTION_FAILURE.
# Fall through to the existing anonymous-JIT remap path when WRITE was dropped;
# that path preserves bytes and registers both RW and RX aliases.
old = '''                if ((unix_prot & PROT_WRITE) && !(info.protection & VM_PROT_WRITE))
                {
                    static unsigned wsurv;
                    if (wsurv++ < 16)
                        dprintf( 2, "ml999: mprotect_exec %p+%#lx asked for W+X but only "
                                 "prot=%#x survived -- returning SUCCESS with WRITE DROPPED. "
                                 "A caller expecting writable backing will fault again\\n",
                                 base, (unsigned long)size, info.protection );
                }
                return 0;  /* genuinely RX/RWX — done */'''
new = '''                if ((unix_prot & PROT_WRITE) && !(info.protection & VM_PROT_WRITE))
                {
                    static unsigned wsurv;
                    if (wsurv++ < 32)
                        dprintf( 2, "ml1154: mprotect_exec %p+%#lx W+X became prot=%#x; "
                                 "falling through to anon JIT dual-map for RW alias\\n",
                                 base, (unsigned long)size, info.protection );
                    /* no return: anonymous-JIT path below creates the RW alias */
                }
                else return 0;  /* requested permissions survived */'''
marker = "ml1154: mprotect_exec"
if marker not in src:
    if src.count(old) != 1:
        raise SystemExit(f"RWX write-drop anchor count={src.count(old)}, expected 1")
    src = src.replace(old, new, 1)

if src.count(helper_sig) != 1 or src.count(hook) != 1 or src.count(marker) != 1:
    raise SystemExit("post-patch verification failed")
path.write_text(src)
print(f"{path}: stale alias retirement + RWX write-drop fallthrough ml1154")
