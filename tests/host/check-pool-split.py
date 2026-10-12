#!/usr/bin/env python3
"""Split JIT pool (pool-split = 1) and POISONED-range salvage in virtual_ios.c; no Wine runs.

GTA V Enhanced on an iPhone 17 Pro Max (logs 2026-10-02 16:00 / 16:19 / 16:33): the
low band [0x148000000, 0x180000000) is split by the main thread's stack, the pool shrank
to the 592-631 MB below it and Social Club's 240 MB libcef.dll copy no longer fit. With
pool-split the app adds the 258-325 MB run above the stack as a second debugger region,
aliased as one span; WINE_IOS_JIT_HOLE names the part in between, which ntdll must never
hand out.

Compiles the production helpers from build/ntdll-unix/virtual_ios.c
(ios_pool_hole_head_place, ios_pool_hole_tail_start, ios_pool_hole_between,
IOS_POOL_IN_HOLE, ios_pool_tail_unreserve, ios_pool_execable_runs) and checks:
  - without a hole every helper is the identity of the old code;
  - a randomized head/tail allocation model built from them never hands out a byte of
    the hole and never overlaps two live ranges, with and without a split;
  - a refused carve that jumped the hole gives its reservation back only while no later
    carve sits on top of it; unsplit it is the old fetch-and-sub;
  - the executable runs of a POISONED range: the 16 KB RW pages are cut out, runs under
    64 KB and unmapped gaps are dropped, a nonsense region answer stops the walk;
and textually: the bump, the warmer, the tail carve, the BIG cap, the pool init and the
POISONED branch use them; the app sets WINE_IOS_JIT_HOLE only for a split pool.
Needs python3 and a C compiler (AddressSanitizer/UBSan).
"""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
native = (root / 'build/ntdll-unix/virtual_ios.c').read_text()
swift = (root / 'app/Madeira/StikJITHelper.swift').read_text()
content = (root / 'app/Madeira/ContentView.swift').read_text()


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2] + '\n'


helpers = ''.join(function(native, sig) for sig in (
    'static size_t ios_pool_hole_head_place(',
    'static size_t ios_pool_hole_tail_start(',
    'static size_t ios_pool_hole_between(',
    'static void ios_pool_tail_unreserve(',
))
macro = native[native.index('#define IOS_POOL_IN_HOLE(o)'):]
macro = macro[:macro.index(')\n', macro.index('ios_jit_hole2_end')) + 2]
runs_typedef = native[native.index('typedef int (*ios_pool_region_fn)'):]
runs_typedef = runs_typedef[:runs_typedef.index(';') + 1] + '\n'
runs = function(native, 'static int ios_pool_execable_runs(')

# --- call sites ---------------------------------------------------------------
bump = native[native.index('/* madeira-bcd split pool: never into the hole (ios_jit_hole_off).'):][:2600]
assert 'ios_pool_hole_head_place( jit_pool_offset, alloc_size,' in bump
assert 'jit_pool_offset = cand + alloc_size;' in bump
assert 'ios_pool_freelist[ios_pool_free_count].off = jit_pool_offset;' in bump

warmer = function(native, 'static void *ios_pool_warmer_thread(')
assert warmer.count('if (IOS_POOL_IN_HOLE(o)) continue;') == 4, 'all four warmer loops skip the hole'

tail = native[native.index('size_t reserve_offset, tail_added, tail_skipped = 0'):][:12000]
assert 'ios_pool_hole_tail_start( ios_jit_pool_size_global, cur, alloc_size,' in tail
assert tail.count('ios_pool_tail_unreserve( &ios_jit_tail_reserved, reserve_offset + alloc_size, tail_added,') == 2
assert '__sync_fetch_and_sub(&ios_jit_tail_reserved' not in tail, 'every rollback goes through ios_pool_tail_unreserve'
assert 'ios_tail_carves[ios_tail_carve_n].off = tail_skip_off;' in tail

cap = native[native.index('enum { TAIL_SMALL = 0x1000000, TAIL_MAX = 0x8000000'):][:2000]
assert 'ios_pool_hole_between( ios_jit_pool_size_global, head_now, tail_now,' in cap
assert 'head_now - tail_now - hole_now - head_reserve' in cap

init = native[native.index('const char *hole = getenv( "WINE_IOS_JIT_HOLE" );'):][:1400]
assert 'h1 < jit_pool_size' in init and '!(h0 & 0x3fff) && !(h1 & 0x3fff)' in init
assert 'ios_jit_hole_off = (size_t)h0;' in init and 'ios_jit_hole_end = (size_t)h1;' in init
assert native.index('ios_jit_pool_size_global = jit_pool_size;') < native.index('const char *hole = getenv( "WINE_IOS_JIT_HOLE" );')

poisoned = native[native.index('if (!ios_pool_range_execable( ios_pool_freelist[i].off,'):][:2400]
assert 'ios_pool_execable_runs( (uint64_t)(uintptr_t)ios_jit_rx_base_global, bad.off, bad.size,' in poisoned
assert '(bad.advised & 2) ? 0' in poisoned, 'a salvaged run found poisoned again is dropped whole'
assert 'ios_pool_freelist[ios_pool_free_count].advised = bad.advised | 2;' in poisoned
print('PASS: bump, warmer, tail carve, BIG cap, pool init and the POISONED branch use the helpers')

assert 'MadeiraConfig.gameValue("pool-split") ?? MadeiraConfig.get("pool-split")' in swift
assert 'if poolSize >= requestedPoolSize {' in swift, 'no split when the pool got its full size'
assert 'poolHole = holeEnd > poolSize ? (off: poolSize, end: holeEnd) : nil' in swift
assert 'VM_FLAGS_OVERWRITE' in swift and 'rw + (rxB - rxA)' in swift, 'one RW alias, one RX->RW distance'
assert 'gapHitsWindow' in swift, 'the hole may never hold the executable window'
hole_env = content[content.index('if let hole = StikJITHelper.poolHole {'):][:500]
assert 'var spec = String(format: "%lx:%lx", hole.off, hole.end)' in hole_env
assert 'spec += String(format: ",%lx:%lx", hole2.off, hole2.end)' in hole_env
assert 'setenv("WINE_IOS_JIT_HOLE", spec, 1)' in hole_env
assert 'unsetenv("WINE_IOS_JIT_HOLE")' in hole_env
print('PASS: the app splits only on request and exports WINE_IOS_JIT_HOLE only for a split pool')

harness = r'''
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
static size_t ios_jit_hole_off, ios_jit_hole_end;
static size_t ios_jit_hole2_off, ios_jit_hole2_end;
''' + macro + helpers + runs_typedef + runs + r'''
#define MB ((size_t)1 << 20)
#define FAIL(...) do { fprintf(stderr, __VA_ARGS__); exit(1); } while (0)

/* --- a model of the pool: head bump + top-down tail carves, the production rules --- */
struct range { size_t off, size; int live; };
static struct range live[4096];
static int nlive;
static size_t total, head, tail_resv;
static unsigned long jumps, below;

static void check_new( size_t off, size_t size )
{
    int i;
    if (off + size > total) FAIL("range 0x%zx+0x%zx past the pool end\n", off, size);
    if (ios_jit_hole_end > ios_jit_hole_off && off < ios_jit_hole_end && off + size > ios_jit_hole_off)
        FAIL("range 0x%zx+0x%zx overlaps the hole [0x%zx,0x%zx)\n", off, size, ios_jit_hole_off, ios_jit_hole_end);
    if (ios_jit_hole2_end > ios_jit_hole2_off && off < ios_jit_hole2_end && off + size > ios_jit_hole2_off)
        FAIL("range 0x%zx+0x%zx overlaps the second hole [0x%zx,0x%zx)\n", off, size, ios_jit_hole2_off, ios_jit_hole2_end);
    for (i = 0; i < nlive; i++)
        if (live[i].live && off < live[i].off + live[i].size && off + size > live[i].off)
            FAIL("range 0x%zx+0x%zx overlaps live 0x%zx+0x%zx\n", off, size, live[i].off, live[i].size);
    live[nlive].off = off; live[nlive].size = size; live[nlive].live = 1; nlive++;
}

static int head_alloc( size_t size )   /* ios_pool_alloc_range_ex's bump */
{
    size_t limit = total - tail_resv;
    size_t cand = ios_pool_hole_head_place( head, size, ios_jit_hole_off, ios_jit_hole_end );
    cand = ios_pool_hole_head_place( cand, size, ios_jit_hole2_off, ios_jit_hole2_end );
    if (cand + size > limit) return 0;
    check_new( cand, size );
    if (cand != head) jumps++;
    head = cand + size;
    return 1;
}

static int tail_alloc( size_t size )   /* NtAllocateVirtualMemoryEx's EC_CODE carve */
{
    int split = ios_jit_hole_end > ios_jit_hole_off;
    size_t cur = tail_resv, start, added, off;
    if (split) start = ios_pool_hole_tail_start( total, ios_pool_hole_tail_start( total, cur, size, ios_jit_hole2_off,
                                                                                ios_jit_hole2_end ),
                                                 size, ios_jit_hole_off, ios_jit_hole_end );
    else start = cur;
    tail_resv = start + size;
    added = start + size - cur;
    off = total - start - size;
    if (start + size > (total / 4) * 3 || off < head)
    {
        ios_pool_tail_unreserve( &tail_resv, start + size, added, split );
        if (tail_resv != cur) FAIL("rollback left the counter at 0x%zx, not 0x%zx\n", tail_resv, cur);
        return 0;
    }
    check_new( off, size );
    if (start != cur) below++;
    return 1;
}

static unsigned long long rng = 88172645463325252ull;
static unsigned rnd( unsigned n ) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (unsigned)(rng % n); }

static void model( size_t pool, size_t h0, size_t h1, int rounds, size_t *heads, size_t *tails )
{
    int r, k;
    *heads = *tails = 0;
    for (r = 0; r < rounds; r++)
    {
        unsigned bias = 1 + rnd(4);   /* 1..4 in 5: from tail-heavy to head-heavy runs */
        total = pool; head = 0; tail_resv = 0; nlive = 0;
        ios_jit_hole_off = h0; ios_jit_hole_end = h1;
        for (k = 0; k < 400 && nlive < 4000; k++)
        {
            size_t sz = ((size_t)(1 + rnd(rnd(4) ? 64 : 4096)) * 0x4000);
            if (rnd(5) < bias) { if (head_alloc( sz )) *heads += sz; }
            else { size_t t = (size_t)0x100000 << rnd(8); if (tail_alloc( t )) *tails += t; }
        }
    }
}

/* GTA V Enhanced + Social Club, sizes from the 2026-10-02 logs: returns the step that failed, 0 if none */
static int gta_scenario( size_t pool, size_t h0, size_t h1 )
{
    size_t i;
    total = pool; head = 0; tail_resv = 0; nlive = 0;
    ios_jit_hole_off = h0; ios_jit_hole_end = h1;
    for (i = 0; i < 275; i += 5) if (!head_alloc( 5 * MB )) return 1;               /* game + launcher images */
    if (!tail_alloc( 128 * MB ) || !tail_alloc( 16 * MB ) || !tail_alloc( 16 * MB )) return 2;   /* FEX code */
    if (!head_alloc( 25 * MB )) return 3;                                           /* SocialClubHelper.exe, chrome_elf */
    if (!head_alloc( 240 * MB )) return 4;                                          /* libcef.dll */
    for (i = 0; i < 30; i += 3) if (!head_alloc( 3 * MB )) return 5;                /* its GPU stack */
    if (!tail_alloc( 16 * MB ) || !tail_alloc( 16 * MB )) return 6;                 /* its code buffers */
    return 0;
}

/* --- a fake VM map for ios_pool_execable_runs --- */
struct reg { uint64_t base, size; unsigned max; };
static struct reg map[16];
static int nmap, nonsense;
static int fake_region( uint64_t *addr, uint64_t *size, unsigned int *max_prot )
{
    int i;
    if (nonsense) { *size = 0x4000; *max_prot = 7; *addr = *addr - 0x8000; return 0; }
    for (i = 0; i < nmap; i++)
        if (map[i].base + map[i].size > *addr) { *addr = map[i].base; *size = map[i].size; *max_prot = map[i].max; return 0; }
    return -1;
}

int main( void )
{
    size_t heads, tails, a, b, c;
    size_t ro[16], rs[16];
    int n;

    /* no hole: identities */
    ios_jit_hole_off = ios_jit_hole_end = 0;
    if (ios_pool_hole_head_place( 0x1234000, 0x8000, 0, 0 ) != 0x1234000) FAIL("head place without hole\n");
    if (ios_pool_hole_tail_start( 600 * MB, 7 * MB, 16 * MB, 0, 0 ) != 7 * MB) FAIL("tail start without hole\n");
    if (ios_pool_hole_between( 600 * MB, 1 * MB, 1 * MB, 0, 0 )) FAIL("between without hole\n");
    if (IOS_POOL_IN_HOLE(0) || IOS_POOL_IN_HOLE(0x4000) || IOS_POOL_IN_HOLE(~(size_t)0)) FAIL("IN_HOLE without hole\n");
    printf("PASS: without WINE_IOS_JIT_HOLE the helpers are the old bump, carve and room\n");

    /* the 16:33 layout: A = 592 MB, stack gap ~5.7 MB, B = 288 MB */
    a = 592 * MB; b = a + 0x5bc000 + 0x4000 * 3; c = b + 288 * MB;
    ios_jit_hole_off = a; ios_jit_hole_end = b;
    if (!IOS_POOL_IN_HOLE(a) || !IOS_POOL_IN_HOLE(b - 0x4000) || IOS_POOL_IN_HOLE(b) || IOS_POOL_IN_HOLE(a - 0x4000))
        FAIL("IN_HOLE bounds\n");
    if (ios_pool_hole_head_place( a - 4 * MB, 4 * MB, a, b ) != a - 4 * MB) FAIL("head that ends at the hole moved\n");
    if (ios_pool_hole_head_place( a - 4 * MB, 4 * MB + 0x4000, a, b ) != b) FAIL("head reaching the hole not moved above it\n");
    if (ios_pool_hole_head_place( b, 64 * MB, a, b ) != b) FAIL("head above the hole moved\n");
    if (ios_pool_hole_tail_start( c, 0, 128 * MB, a, b ) != 0) FAIL("top carve moved\n");
    if (ios_pool_hole_tail_start( c, 250 * MB, 64 * MB, a, b ) != c - a) FAIL("overlapping carve not put below the hole\n");
    if (ios_pool_hole_tail_start( c, c - a, 16 * MB, a, b ) != c - a) FAIL("carve directly below the hole moved\n");
    if (ios_pool_hole_tail_start( c, c - b, 16 * MB, a, b ) != c - a) FAIL("carve ending at the hole top not moved below\n");
    if (ios_pool_hole_tail_start( c, c - b - 16 * MB, 16 * MB, a, b ) != c - b - 16 * MB) FAIL("carve ending at hole_end moved\n");
    if (ios_pool_hole_between( c, 500 * MB, 200 * MB, a, b ) != b - a) FAIL("hole between head and tail not counted\n");
    if (ios_pool_hole_between( c, b + MB, 100 * MB, a, b ) || ios_pool_hole_between( c, 100 * MB, c - a + MB, a, b ))
        FAIL("hole counted once a side passed it\n");
    printf("PASS: head jumps the hole, a carve that would overlap it goes directly below it, room excludes it\n");

    {
        volatile size_t ctr = 300 * MB;
        ios_pool_tail_unreserve( &ctr, 300 * MB, 16 * MB, 0 );
        if (ctr != 284 * MB) FAIL("unsplit rollback is not fetch-and-sub\n");
        ctr = 300 * MB;
        ios_pool_tail_unreserve( &ctr, 300 * MB, 100 * MB, 1 );
        if (ctr != 200 * MB) FAIL("split rollback with nothing on top\n");
        ctr = 316 * MB;   /* another carve came after ours */
        ios_pool_tail_unreserve( &ctr, 300 * MB, 100 * MB, 1 );
        if (ctr != 316 * MB) FAIL("split rollback lowered the counter under a later carve\n");
        printf("PASS: a refused carve gives back what it took, and never lowers the counter under a later carve\n");
    }

    jumps = below = 0;
    model( c, a, b, 300, &heads, &tails );
    if (!jumps || !below) FAIL("the split model never jumped the hole (%lu) or carved below it (%lu)\n", jumps, below);
    printf("PASS: split model, 300 runs: %zu MB head + %zu MB tail handed out (%lu head jumps, %lu carves below "
           "the hole), none in the hole, no overlap\n", heads >> 20, tails >> 20, jumps, below);
    if ((n = gta_scenario( 592 * MB, 0, 0 )) != 4) FAIL("GTA scenario on the 592 MB pool failed at step %d, not 4\n", n);
    if ((n = gta_scenario( c, a, b )) != 0) FAIL("GTA scenario on the split pool failed at step %d\n", n);
    printf("PASS: GTA-shaped run: the 592 MB pool refuses libcef.dll, the split pool (592 + 288 MB) serves it all\n");
    model( 592 * MB, 0, 0, 300, &heads, &tails );
    printf("PASS: unsplit model, 300 runs: %zu MB head + %zu MB tail, no overlap\n", heads >> 20, tails >> 20);
    model( c, 64 * MB, 64 * MB + 0x4000, 100, &heads, &tails );
    model( c, c - 0x8000 - 0x4000, c - 0x8000, 100, &heads, &tails );
    printf("PASS: a 16 KB hole near either end is never handed out\n");

    /* pool-split = low (FH4, 2026-10-12): C 336 MB | window 132 MB | A 560 MB | stack 22 MB | B 256 MB */
    {
        size_t h0 = 336 * MB, h1 = h0 + 132 * MB, h2 = h1 + 560 * MB, h3 = h2 + 22 * MB, end = h3 + 256 * MB;
        ios_jit_hole_off = h0; ios_jit_hole_end = h1; ios_jit_hole2_off = h2; ios_jit_hole2_end = h3;
        if (!IOS_POOL_IN_HOLE(h0) || !IOS_POOL_IN_HOLE(h2) || !IOS_POOL_IN_HOLE(h3 - 0x4000) || IOS_POOL_IN_HOLE(h1)
            || IOS_POOL_IN_HOLE(h3) || IOS_POOL_IN_HOLE(h2 - 0x4000))
            FAIL("IN_HOLE bounds with two holes\n");
        jumps = below = 0;
        model( end, h0, h1, 300, &heads, &tails );
        if (!jumps || !below) FAIL("the two-hole model never jumped (%lu) or carved below (%lu)\n", jumps, below);
        printf("PASS: two-hole model, 300 runs: %zu MB head + %zu MB tail, none in either hole, no overlap\n",
               heads >> 20, tails >> 20);
        ios_jit_hole2_off = ios_jit_hole2_end = 0;
    }

    /* POISONED salvage: off 0x110ac000 size 0x25c000, one RW page at +0x1e0000 (16:19 log) */
    nmap = 3;
    map[0] = (struct reg){ 0x148000000ull, 0x110ac000ull + 0x1e0000, 5 };
    map[1] = (struct reg){ 0x148000000ull + 0x110ac000 + 0x1e0000, 0x4000, 3 };
    map[2] = (struct reg){ 0x148000000ull + 0x110ac000 + 0x1e4000, 0x10000000, 7 };
    n = ios_pool_execable_runs( 0x148000000ull, 0x110ac000, 0x25c000, fake_region, ro, rs, 16 );
    if (n != 2 || ro[0] != 0x110ac000 || rs[0] != 0x1e0000 || ro[1] != 0x110ac000 + 0x1e4000 || rs[1] != 0x78000)
        FAIL("salvage: n=%d %zx+%zx %zx+%zx\n", n, ro[0], rs[0], ro[1], rs[1]);
    /* a run under 64 KB is dropped, an unmapped gap ends a run */
    nmap = 4;
    map[0] = (struct reg){ 0x200000000ull, 0xc000, 5 };
    map[1] = (struct reg){ 0x20000c000ull, 0x4000, 1 };
    map[2] = (struct reg){ 0x200010000ull, 0x20000, 5 };
    map[3] = (struct reg){ 0x200040000ull, 0x40000, 5 };   /* gap [0x30000, 0x40000) before it */
    n = ios_pool_execable_runs( 0x200000000ull, 0, 0x80000, fake_region, ro, rs, 16 );
    if (n != 2 || ro[0] != 0x10000 || rs[0] != 0x20000 || ro[1] != 0x40000 || rs[1] != 0x40000)
        FAIL("salvage with gap: n=%d %zx+%zx %zx+%zx\n", n, ro[0], rs[0], ro[1], rs[1]);
    /* nothing mapped at all, and a nonsense answer, end the walk with no run */
    nmap = 0;
    if (ios_pool_execable_runs( 0x200000000ull, 0, 0x80000, fake_region, ro, rs, 16 ) != 0) FAIL("unmapped salvage\n");
    nonsense = 1;
    if (ios_pool_execable_runs( 0x200000000ull, 0x10000, 0x80000, fake_region, ro, rs, 16 ) != 0) FAIL("nonsense salvage\n");
    nonsense = 0;
    /* `max` caps the output */
    nmap = 3;
    map[0] = (struct reg){ 0x300000000ull, 0x10000, 5 };
    map[1] = (struct reg){ 0x300010000ull, 0x4000, 3 };
    map[2] = (struct reg){ 0x300014000ull, 0x10000, 5 };
    if (ios_pool_execable_runs( 0x300000000ull, 0, 0x24000, fake_region, ro, rs, 1 ) != 1) FAIL("max ignored\n");
    printf("PASS: a POISONED range keeps its executable runs of 64 KB or more; RW pages, gaps and short runs are cut\n");
    return 0;
}
'''

with tempfile.TemporaryDirectory() as t:
    c = Path(t) / 'split.c'
    c.write_text(harness)
    exe = Path(t) / 'split'
    subprocess.run(['cc', '-std=gnu11', '-O1', '-Wall', '-Wno-unused-function', '-fsanitize=address,undefined',
                    '-fno-sanitize-recover=all', str(c), '-o', str(exe)], check=True)
    out = subprocess.run([str(exe)], capture_output=True, text=True)
    print(out.stdout, end='')
    assert out.returncode == 0, out.stderr
