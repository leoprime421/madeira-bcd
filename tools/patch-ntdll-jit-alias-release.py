#!/usr/bin/env python3
"""Patch virtual_ios.c: retire stale aliases, fix iOS RWX write-drop, split tail reuse,
and auto-enable the existing W^X fast path for official Steam FH4."""
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

# FH4/CEF can recycle a large free tail carve for a tiny EC-code request.
# ml438 consumed the WHOLE free carve and returned that full size to FEX
# (observed: 16 MB reused for a 16 KB request), turning the remainder into
# live-but-unused pool space. Split the selected carve in place: the requested
# prefix becomes live and the unused suffix remains a free carve.
tail_old = '''                void *jit_rx = (char *)ios_jit_rx_base_global + ios_tail_carves[best].off;
                void *jit_rw = (char *)ios_jit_rw_base_global + ios_tail_carves[best].off;
                size_t got = ios_tail_carves[best].size;
                ios_tail_carves[best].free = 0;
                pthread_mutex_unlock( &ios_tail_carve_lock );'''
tail_new = '''                void *jit_rx = (char *)ios_jit_rx_base_global + ios_tail_carves[best].off;
                void *jit_rw = (char *)ios_jit_rw_base_global + ios_tail_carves[best].off;
                size_t whole = ios_tail_carves[best].size;
                size_t got = alloc_size;
                size_t remainder = 0;
                if (whole > alloc_size && ios_tail_carve_n < IOS_TAIL_CARVE_MAX)
                {
                    remainder = whole - alloc_size;
                    ios_tail_carves[ios_tail_carve_n].off = ios_tail_carves[best].off + alloc_size;
                    ios_tail_carves[ios_tail_carve_n].size = remainder;
                    ios_tail_carves[ios_tail_carve_n].free = 1;
                    ios_tail_carves[best].size = alloc_size;
                    ios_tail_carves[best].free = 0;
                    ios_tail_carve_n++;
                }
                else
                {
                    /* If the bookkeeping table is full, retain the old safe
                     * whole-carve behaviour rather than create an untracked range. */
                    got = whole;
                    ios_tail_carves[best].free = 0;
                }
                pthread_mutex_unlock( &ios_tail_carve_lock );
                if (remainder)
                    dprintf(2, "[jit-pool] ml1156: split reused tail carve rx=%p "
                               "asked=0x%lx remainder=0x%lx\\n",
                            jit_rx, (unsigned long)got, (unsigned long)remainder);'''
tail_marker = "ml1156: split reused tail carve"
if tail_marker not in src:
    if src.count(tail_old) != 1:
        raise SystemExit(f"tail split anchor count={src.count(tail_old)}, expected 1")
    src = src.replace(tail_old, tail_new, 1)

if (src.count(helper_sig) != 1 or src.count(hook) != 1 or
        src.count(marker) != 1 or src.count(tail_marker) != 1):
    raise SystemExit("post-patch verification failed")
path.write_text(src)

# Steam FH4 build 83 does not crash; it spends millions of Mach round trips
# emulating ordinary stores to one PAGE_EXECUTE_READWRITE SMC page. Madeira
# already has the ml691/ml694 page-granular W^X fast path for exactly this:
# after 32 faults it makes the translated guest page host RW (never RWX), so
# later stores land directly. ml695 left it opt-in globally because of stale
# translation/address-reuse hazards. Auto-enable it ONLY for the official Steam
# FH4 install when the user has not explicitly set MADEIRA_WX=0/1.
signal_path = path.with_name("signal_arm64_ios.c")
sig = signal_path.read_text()
wx_old = '''        const char *e = getenv( "MADEIRA_WX" );
        v = (e && e[0] == '1') ? 1 : 0;
        dprintf( STDERR_FILENO, "[wx] ml695 MADEIRA_WX=%s -> W^X %s (experimental, opt-in; threshold 32, %d slots)\\n",
                 e ? e : "(unset)", v ? "ENABLED" : "DISABLED", IOS_WX_MAX );'''
wx_new = '''        const char *e = getenv( "MADEIRA_WX" );
        const char *steam_path = getenv( "SteamAppPath" );
        int steam_fh4 = steam_path && strstr( steam_path, "steamapps" ) &&
                        strstr( steam_path, "ForzaHorizon4" );

        /* ml1157: Build 83 official Steam FH4 spent >3.5M faults at one JIT
         * store site targeting one SMC page while W^X was OFF. This existing
         * fast path is exactly the measured cure: after 32 writes to a page,
         * guest RX becomes host RW (not RWX), eliminating per-store Mach
         * exceptions. Keep ml695's safe default for every other title and
         * honor an explicit MADEIRA_WX=0/1 override. */
        if (e) v = (e[0] == '1') ? 1 : 0;
        else v = steam_fh4 ? 1 : 0;
        dprintf( STDERR_FILENO,
                 "[wx] ml1157 MADEIRA_WX=%s steam-fh4=%d -> W^X %s "
                 "(threshold 32, %d slots)\\n",
                 e ? e : "(unset)", steam_fh4,
                 v ? "ENABLED" : "DISABLED", IOS_WX_MAX );'''
wx_marker = "[wx] ml1157"
if wx_marker not in sig:
    if sig.count(wx_old) != 1:
        raise SystemExit(f"Steam FH4 W^X anchor count={sig.count(wx_old)}, expected 1")
    sig = sig.replace(wx_old, wx_new, 1)
signal_path.write_text(sig)

if sig.count(wx_marker) != 1:
    raise SystemExit("Steam FH4 W^X post-patch verification failed")

print(f"{path}: alias retirement + RWX write-drop ml1154 + tail split ml1156")
print(f"{signal_path}: Steam FH4 auto W^X ml1157")
