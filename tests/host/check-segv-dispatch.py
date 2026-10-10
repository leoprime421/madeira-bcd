#!/usr/bin/env python3
"""Exercise the actual SIGSEGV dispatch tail with host-only Wine/platform stubs.

Repeated faults at a shared FEX host PC must reach guest exception dispatch,
without an instruction skip or register poisoning. Also check that VM and
syscall faults still take their existing recovery paths. No Apple SDK needed.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
s = (root / 'build/ntdll-unix/signal_arm64_ios.c').read_text()
start = s.index('    rec.NumberParameters = 2;', s.index('static void segv_handler('))
end = s.index('    setup_exception( context, &rec );\n}', start)
tail = s[start:end + len('    setup_exception( context, &rec );')]
code = r'''
#include <stdint.h>
#include <stddef.h>
#include <assert.h>
#include <stdio.h>
#define WINE_IOS 1
#define EXCEPTION_EXECUTE_FAULT 8
#define EXCEPTION_WRITE_FAULT 1
#define EXCEPTION_READ_FAULT 0
#define ERR(...) ((void)0)
typedef uintptr_t ULONG_PTR;
typedef struct { uintptr_t pc, sp, x[31]; } Context;
typedef struct { void *si_addr; } SigInfo;
typedef struct { int NumberParameters; uintptr_t ExceptionInformation[2]; } EXCEPTION_RECORD;
typedef struct { const char *dli_fname, *dli_sname; void *dli_saddr, *dli_fbase; } Dl_info;
#define PC_sig(c) ((c)->pc)
#define SP_sig(c) ((c)->sp)
#define REGn_sig(n,c) ((c)->x[n])
void *ios_jit_rx_base_global = (void *)0x100000000ULL;
size_t ios_jit_pool_size_global = 0x100000;
static uintptr_t ios_teb_for_signals;
static unsigned delivered, fixed, mode;
static int ios_sig_storm_gate(unsigned long *n) { return ++*n < 20; }
static int dladdr(void *p, Dl_info *d) { return 0; }
void ios_dump_fault_region(void *p) {}
static int ios_subfloor_service(Context *c, void *p, const char *s) { return 0; }
static void ios_fixup_x18_for_return(Context *c) { fixed++; }
static int virtual_handle_fault(EXCEPTION_RECORD *r, void *sp) { return mode != 1; }
static int handle_syscall_fault(Context *c, EXCEPTION_RECORD *r) { return mode == 2; }
static void setup_exception(Context *c, EXCEPTION_RECORD *r) {
    assert(r->NumberParameters == 2);
    assert(r->ExceptionInformation[0] == EXCEPTION_READ_FAULT);
    assert(r->ExceptionInformation[1] == 0);
    delivered++;
}
static void route(Context *context, SigInfo *siginfo, unsigned esr) {
    EXCEPTION_RECORD rec = {0};
''' + tail + r'''
}
int main(void) {
    Context threads[2] = {{.pc=0x100000660ULL, .x={42}}, {.pc=0x100000660ULL, .x={43}}};
    SigInfo info = {0};
    for (unsigned i=0; i<24; i++) {
        Context *c = &threads[i % 2];
        route(c, &info, 0);
        assert(c->pc == 0x100000660ULL);
        assert(c->x[0] == 42 + i % 2);
        assert(delivered == i + 1);
    }
    mode=1; route(&threads[0], &info, 0);
    assert(delivered == 24 && fixed == 1);
    mode=2; route(&threads[0], &info, 0);
    assert(delivered == 24 && fixed == 2);
    puts("PASS: repeated FEX PCs preserve registers and guest SEH; VM/syscall recovery retained");
}
'''
with tempfile.TemporaryDirectory() as d:
    src, exe = Path(d)/'test.c', Path(d)/'test'
    src.write_text(code)
    subprocess.run(['cc', '-Wall', '-Wno-unused-function', '-Wno-unused-variable',
                    '-fsanitize=address,undefined', str(src), '-o', str(exe)], check=True)
    subprocess.run([str(exe)], check=True)
