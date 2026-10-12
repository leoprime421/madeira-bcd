#!/usr/bin/env python3
"""Exercise the actual partial-page decommit helpers; real Mach APIs on macOS.

Linux models Mach write errors and uses real mprotect for permissions. Tests
check bytes outside the decommit, restored permissions, writable FEX pages,
and failures. No Wine process or IPA is needed.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = (root / 'build/ntdll-unix/virtual_ios.c').read_text()
start = source.index('#define IOS_DC_INLINE')
end = source.index('static NTSTATUS decommit_pages(', start)
helpers = source[start:end]
body = source[end:source.index('#undef IOS_DC_INLINE', end)]
assert '"subpage-memset"' not in body
assert body.count('!ios_dc_zero_edge(') == 3
assert 'dc_verify = base;' not in body

code = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/mman.h>
#ifdef __APPLE__
#include <mach/mach.h>
#include <mach/mach_vm.h>
#else
typedef uintptr_t mach_vm_address_t, vm_offset_t;
typedef size_t mach_vm_size_t;
typedef unsigned int mach_msg_type_number_t;
typedef int kern_return_t, vm_prot_t, mach_port_t;
typedef int *vm_region_info_t;
typedef struct { int protection, max_protection; } vm_region_basic_info_data_64_t;
#define VM_REGION_BASIC_INFO_COUNT_64 2
#define VM_REGION_BASIC_INFO_64 0
#define KERN_SUCCESS 0
#define KERN_PROTECTION_FAILURE 2
#define KERN_INVALID_ADDRESS 1
#define VM_PROT_READ PROT_READ
#define VM_PROT_WRITE PROT_WRITE
#define VM_PROT_EXECUTE PROT_EXEC
#define MACH_PORT_NULL 0
#define FALSE 0
#define mach_task_self() 0
static char *allocation;
static size_t allocation_size;
static int current_prot, max_prot = PROT_READ | PROT_WRITE | PROT_EXEC;
static kern_return_t mach_vm_region(int task, mach_vm_address_t *addr, mach_vm_size_t *size,
    int flavor, vm_region_info_t info, mach_msg_type_number_t *count, mach_port_t *object)
{
    vm_region_basic_info_data_64_t *out = (void *)info;
    if (*addr < (uintptr_t)allocation || *addr >= (uintptr_t)allocation + allocation_size)
        return KERN_INVALID_ADDRESS;
    *addr = (uintptr_t)allocation; *size = allocation_size;
    out->protection = current_prot; out->max_protection = max_prot;
    *count = VM_REGION_BASIC_INFO_COUNT_64; *object = 0; return 0;
}
static int mach_port_deallocate(int task, int object) { return 0; }
static kern_return_t mach_vm_protect(int task, mach_vm_address_t addr, mach_vm_size_t size,
    int maximum, vm_prot_t prot)
{
    if (prot & ~max_prot) return KERN_PROTECTION_FAILURE;
    if (mprotect((void *)addr, size, prot)) return KERN_INVALID_ADDRESS;
    current_prot = prot; return 0;
}
static kern_return_t mach_vm_write(int task, mach_vm_address_t addr, vm_offset_t data,
    mach_msg_type_number_t size)
{
    /* As Darwin: no access at all is KERN_INVALID_ADDRESS, read-only is a
     * protection failure (macOS runner, 2026-10-12: kr=1 on PROT_NONE). */
    if (!current_prot) return KERN_INVALID_ADDRESS;
    if (!(current_prot & PROT_WRITE)) return KERN_PROTECTION_FAILURE;
    memcpy((void *)addr, (void *)data, size);
    return KERN_SUCCESS;
}
/* Match the Darwin declaration on Linux too, so a name collision is caught
 * locally instead of only by the macOS runner. */
extern kern_return_t host_page_size(mach_port_t, size_t *);
#endif
/* The extracted Wine helpers use this name for a variable; Mach reserves it
 * for a function. Keep the alias after the system headers. */
#define host_page_size dc_test_page_size
static size_t host_page_size, host_page_mask;
static void *ios_jit_rx_base_global, *ios_jit_rw_base_global;
static size_t ios_jit_pool_size_global;
static int alias_overlap, fail_write, fail_protect;
#define ROUND_ADDR(addr, mask) ((void *)((uintptr_t)(addr) & ~(uintptr_t)(mask)))
int ios_jit_anon_alias_overlaps(void *base, size_t size, uintptr_t *lo, uintptr_t *hi)
{ return alias_overlap; }
static kern_return_t test_write(mach_port_t task, mach_vm_address_t addr,
    vm_offset_t data, mach_msg_type_number_t size)
{
    if (fail_write) return KERN_PROTECTION_FAILURE;
    return mach_vm_write(task, addr, data, size);
}
static kern_return_t test_protect(mach_port_t task, mach_vm_address_t addr,
    mach_vm_size_t size, int maximum, vm_prot_t prot)
{
    if (fail_protect && (prot & VM_PROT_WRITE)) return KERN_PROTECTION_FAILURE;
    return mach_vm_protect(task, addr, size, maximum, prot);
}
#define mach_vm_write test_write
#define mach_vm_protect test_protect
''' + helpers + r'''
#undef mach_vm_write
#undef mach_vm_protect
static void protect_page(char *p, int prot)
{
    assert(mach_vm_protect(mach_task_self(), (uintptr_t)p, host_page_size, 0, prot) == KERN_SUCCESS);
}
static void run(char *p, int prot, size_t offset, size_t len)
{
    vm_prot_t before, before_max, after, after_max;
    protect_page(p, PROT_READ | PROT_WRITE);
    memset(p, 0xa5, host_page_size);
    protect_page(p, prot);
    assert(ios_dc_query(p, &before, &before_max));
    assert(ios_dc_zero_edge(p + offset, len));
    assert(ios_dc_query(p, &after, &after_max));
    assert(before == after && before_max == after_max);
    protect_page(p, PROT_READ | PROT_WRITE);
    for (size_t i = 0; i < host_page_size; i++)
        assert((unsigned char)p[i] == ((i >= offset && i < offset + len) ? 0 : 0xa5));
}
int main(void)
{
    char *p;
    vm_prot_t prot, maximum;
#ifdef __APPLE__
    host_page_size = getpagesize();
#else
    host_page_size = 16384; /* iOS host page vs 4 KiB guest page */
#endif
    host_page_mask = host_page_size - 1;
    /* Overallocate and align so the Linux substitute also models 16 KiB pages. */
    p = mmap(NULL, 2 * host_page_size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0);
    assert(p != MAP_FAILED);
    p = (char *)(((uintptr_t)p + host_page_mask) & ~host_page_mask);
#ifndef __APPLE__
    allocation = p; allocation_size = host_page_size; current_prot = PROT_READ | PROT_WRITE;
#endif
    for (int prot = 0; prot <= (PROT_READ | PROT_WRITE); prot++)
    {
        run(p, prot, host_page_size / 4, host_page_size * 3 / 4);
        run(p, prot, 0, host_page_size / 4);
        run(p, prot, host_page_size / 4, host_page_size / 4);
    }
    /* Writable FEX clears must not be rejected by protection-change guards. */
    alias_overlap = 1;
    run(p, PROT_READ | PROT_WRITE, 0, host_page_size);
    protect_page(p, PROT_NONE);
    assert(!ios_dc_zero_edge(p, 4096));
    assert(ios_dc_query(p, &prot, &maximum) && prot == PROT_NONE);
    alias_overlap = 0;
    ios_jit_rx_base_global = p; ios_jit_pool_size_global = host_page_size;
    assert(!ios_dc_zero_edge(p, 4096));
    ios_jit_rx_base_global = NULL; ios_jit_pool_size_global = 0;
    fail_write = 1;
    assert(!ios_dc_zero_edge(p, 4096));
    assert(ios_dc_query(p, &prot, &maximum) && prot == PROT_NONE);
    fail_write = 0; fail_protect = 1;
    assert(!ios_dc_zero_edge(p, 4096));
    assert(ios_dc_query(p, &prot, &maximum) && prot == PROT_NONE);
    fail_protect = 0;
    run(p, PROT_NONE, host_page_size / 4, host_page_size * 3 / 4);
    puts("PASS: zeroing, neighbor bytes, restored protections, writable FEX path, failure recovery");
    return 0;
}
'''
with tempfile.TemporaryDirectory() as directory:
    src = Path(directory) / 'decommit.c'
    exe = Path(directory) / 'decommit'
    src.write_text(code)
    subprocess.run([os.environ.get('CC', 'cc'), '-O2', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-parameter', '-o', str(exe), str(src)], check=True)
    subprocess.run([str(exe)], check=True, timeout=15)
