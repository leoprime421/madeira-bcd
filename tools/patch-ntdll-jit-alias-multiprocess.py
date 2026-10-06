#!/usr/bin/env python3
"""Route late iOS JIT image aliases to the FEX emulator that owns the mapping PEB.

Build 84 Spider-Man showed the exact failure this fixes:
  wintrust.dll mapped by the parent PEB was pushed to a child pseudo-process's
  FEX alias table, so the parent's IosJitReverseTranslate() could not turn the
  pool-copy RIP back into the PE VA and FEX emitted NOEXEC.

virtual_ios.c historically keeps one global push callback. Every ARM64EC
pseudo-process has its own libarm64ecfex.dll and alias table, so "last process
to register wins" is not a valid routing policy once children are alive.
ml1158 retains one callback per registered PEB and uses the current mapping PEB
to select the destination. Mappings created before an emulator registers are
still handled by the existing catch-up drain in unix_ios_push_jit_aliases().
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1 else "build/ntdll-unix/virtual_ios.c")
src = path.read_text()

marker = "[alias-push] ml1158"
helper_name = "ios_jit_alias_callback_for_peb"

if marker in src:
    required = (helper_name, "ios_jit_alias_register_emulator", "ios_jit_alias_unregister_emulator")
    if not all(x in src for x in required):
        raise SystemExit("partial ml1158 alias-routing patch found")
    print(f"{path}: ml1158 already applied")
    raise SystemExit(0)

registry_old = '''#define IOS_ALIAS_REG_MAX 32
static void *ios_jit_alias_registered[IOS_ALIAS_REG_MAX];
static int ios_jit_alias_registered_n;
'''
registry_new = '''#define IOS_ALIAS_REG_MAX 32
static void *ios_jit_alias_registered[IOS_ALIAS_REG_MAX];
typedef void (*ios_jit_alias_cb_t)(unsigned long long, unsigned long long, unsigned long long);
static ios_jit_alias_cb_t ios_jit_alias_registered_cb[IOS_ALIAS_REG_MAX];
static int ios_jit_alias_registered_n;

/* ml1158: each pseudo-process has its own libarm64ecfex.dll and therefore its
 * own IosJitAlias table.  The old single global callback was overwritten by
 * the most recently started child, so a DLL mapped later by the parent was
 * pushed into the child's table.  Keep a callback beside every registered
 * PEB and route late mappings to the process that actually mapped them. */
static ios_jit_alias_cb_t ios_jit_alias_callback_for_peb( void *peb )
{
    int i, n = ios_jit_alias_registered_n;
    if (!peb) return NULL;
    for (i = 0; i < n && i < IOS_ALIAS_REG_MAX; i++)
        if (ios_jit_alias_registered[i] == peb)
            return ios_jit_alias_registered_cb[i];
    return NULL;
}

static void ios_jit_alias_register_emulator( void *peb, ios_jit_alias_cb_t cb )
{
    int i, n = ios_jit_alias_registered_n, free_slot = -1;
    if (!peb || !cb) return;

    for (i = 0; i < n && i < IOS_ALIAS_REG_MAX; i++)
    {
        if (ios_jit_alias_registered[i] == peb)
        {
            ios_jit_alias_registered_cb[i] = cb;
            __sync_synchronize();
            return;
        }
        if (!ios_jit_alias_registered[i] && free_slot < 0) free_slot = i;
    }

    if (free_slot < 0)
    {
        if (n >= IOS_ALIAS_REG_MAX) return;
        free_slot = n;
        ios_jit_alias_registered_n = n + 1;
    }

    /* Publish callback first, PEB last: readers only use a slot after the PEB
     * matches, so they cannot observe a newly-published PEB with a stale cb. */
    ios_jit_alias_registered_cb[free_slot] = cb;
    __sync_synchronize();
    ios_jit_alias_registered[free_slot] = peb;
    __sync_synchronize();
}

static void ios_jit_alias_unregister_emulator( void *peb )
{
    int i, n = ios_jit_alias_registered_n;
    if (!peb) return;
    for (i = 0; i < n && i < IOS_ALIAS_REG_MAX; i++)
    {
        if (ios_jit_alias_registered[i] != peb) continue;
        /* Hide the slot before clearing a callback whose code may be reclaimed. */
        ios_jit_alias_registered[i] = NULL;
        __sync_synchronize();
        ios_jit_alias_registered_cb[i] = NULL;
        __sync_synchronize();
        return;
    }
}
'''
if src.count(registry_old) != 1:
    raise SystemExit(f"ml1158 registry anchor count={src.count(registry_old)}, expected 1")
src = src.replace(registry_old, registry_new, 1)

register_old = '''    prev_peb = ios_jit_alias_pushback_peb;
    ios_jit_alias_pushback_cb = params->callback;
    ios_jit_alias_pushback_peb = self;
    if (self && !ios_jit_alias_has_emulator( self ) && ios_jit_alias_registered_n < IOS_ALIAS_REG_MAX)
    {
        ios_jit_alias_registered[ios_jit_alias_registered_n] = self;
        __sync_synchronize();
        ios_jit_alias_registered_n++;
    }
'''
register_new = '''    prev_peb = ios_jit_alias_pushback_peb;
    ios_jit_alias_register_emulator( self, params->callback );
    /* Keep the legacy latest callback for callers with no process identity
     * (notably the sub-floor bridge), but image mappings below no longer use
     * it when a concrete PEB is known. */
    ios_jit_alias_pushback_cb = params->callback;
    ios_jit_alias_pushback_peb = self;
'''
if src.count(register_old) != 1:
    raise SystemExit(f"ml1158 registration anchor count={src.count(register_old)}, expected 1")
src = src.replace(register_old, register_new, 1)

# Replace only the late-image push block following this unique comment.  A
# brace scanner avoids depending on the diagnostic prose inside the old block.
push_anchor = "/* If xtajit64 has already registered its alias-mapping push callback"
pos = src.find(push_anchor)
if pos < 0:
    raise SystemExit("ml1158 late-image push comment not found")
start = src.find("    if (ios_jit_alias_pushback_cb)", pos)
if start < 0:
    raise SystemExit("ml1158 late-image push if not found")
brace = src.find("{", start)
if brace < 0:
    raise SystemExit("ml1158 late-image push opening brace not found")
depth = 0
end = None
for i in range(brace, len(src)):
    c = src[i]
    if c == "{":
        depth += 1
    elif c == "}":
        depth -= 1
        if depth == 0:
            end = i + 1
            break
if end is None:
    raise SystemExit("ml1158 late-image push block is unbalanced")

push_new = '''    {
        void *cur = ios_jit_current_peb();
        ios_jit_alias_cb_t cb = ios_jit_alias_callback_for_peb( cur );

        /* Preserve the old behaviour only when the mapping is known to belong
         * to the process that owns the global latest callback, or when no PEB
         * can be identified.  Never send a known parent's mapping to a child. */
        if (!cb && (!cur || cur == ios_jit_alias_pushback_peb))
            cb = ios_jit_alias_pushback_cb;

        if (cb)
        {
            if (cur && ios_jit_alias_pushback_peb && cur != ios_jit_alias_pushback_peb)
            {
                static int routed_n;
                if (routed_n++ < 24)
                    dprintf( 2, "[alias-push] ml1158 image %p+0x%lx (%s) mapped by peb=%p "
                                "routed to its OWN emulator cb=%p (latest peb=%p)\\n",
                             pe_base, (unsigned long)size, ios_pe_module_name( pe_base, size ),
                             cur, (void *)cb, ios_jit_alias_pushback_peb );
            }
            cb( (unsigned long long)(uintptr_t)pe_base,
                (unsigned long long)(uintptr_t)jit_base,
                (unsigned long long)size );
        }
        else if (cur && ios_jit_alias_has_emulator( cur ))
        {
            static int missing_n;
            if (missing_n++ < 8)
                dprintf( 2, "[alias-push] ml1158 peb=%p has an emulator but no live callback; "
                            "NOT misrouting image %p+0x%lx (%s); catch-up drain will own it\\n",
                         cur, pe_base, (unsigned long)size, ios_pe_module_name( pe_base, size ) );
        }
        /* If this PEB has not registered FEX yet, do nothing: the existing
         * unix_ios_push_jit_aliases catch-up loop pushes all mappings at init. */
    }'''
src = src[:start] + push_new + src[end:]

reclaim_anchor = '''    if (peb == ios_jit_alias_pushback_peb && ios_jit_alias_pushback_cb)
    {'''
reclaim_new = '''    /* ml1158: remove this process's callback before its FEX pool copy can be
     * reclaimed/reused.  Future late mappings must never call dead code. */
    ios_jit_alias_unregister_emulator( peb );

    if (peb == ios_jit_alias_pushback_peb && ios_jit_alias_pushback_cb)
    {'''
if src.count(reclaim_anchor) != 1:
    raise SystemExit(f"ml1158 reclaim anchor count={src.count(reclaim_anchor)}, expected 1")
src = src.replace(reclaim_anchor, reclaim_new, 1)

checks = [
    "[alias-push] ml1158 image",
    "ios_jit_alias_callback_for_peb( cur )",
    "ios_jit_alias_register_emulator( self, params->callback )",
    "ios_jit_alias_unregister_emulator( peb )",
]
if not all(x in src for x in checks):
    raise SystemExit("ml1158 post-patch verification failed")

path.write_text(src)
print(f"{path}: ml1158 per-PEB FEX alias callback routing applied")
