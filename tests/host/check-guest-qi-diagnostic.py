#!/usr/bin/env python3
"""Run the actual crash-only QI probe with captured x86 code and safe-read stubs.
No Darwin SDK is needed; this tests extraction, negative displacements, and
unreadable/partial guest memory without dereferencing guest addresses.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = (root / 'build/ntdll-unix/signal_arm64_ios.c').read_text()
probe = source.split('/* BEGIN guest-qi diagnostic.', 1)[1]
probe = probe.split('*/', 1)[1].split('/* END guest-qi diagnostic.', 1)[0]
harness = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <stdlib.h>
typedef uint64_t mach_vm_address_t;
typedef uint64_t mach_vm_size_t;
#define KERN_SUCCESS 0
static int mode, reads;
static uint64_t target;
static const unsigned char guid[16] = {0x07,0x28,0x05,0x1f,0x46,0x0b,0xcc,0x4a,0x8a,0x89,0x36,0x4f,0x79,0x37,0x18,0xa4};
static int mach_task_self(void) { return 0; }
static int mach_vm_read_overwrite(int task, uint64_t addr, uint64_t size, uint64_t dst, uint64_t *got) {
    uint64_t value;
    (void)task; ++reads; *got = 0;
    if (mode == 1) return 1;
    if (mode == 2) { *got = size - 1; return 0; }
    if (addr == target && size == 16) memcpy((void *)(uintptr_t)dst, guid, 16);
    else if (addr == 0x70000000 && size == 8) { value = 0x71000000; memcpy((void *)(uintptr_t)dst, &value, 8); }
    else if (addr == 0x71000000 && size == 8) { value = 0x72000000; memcpy((void *)(uintptr_t)dst, &value, 8); }
    else if (addr == 0x72000000 && size == 32) { uint64_t v[4] = {0x1111,0x2222,0x3333,0x4444}; memcpy((void *)(uintptr_t)dst, v, 32); }
    else if (addr == 0x73000040 && size == 8) { value = 0; memcpy((void *)(uintptr_t)dst, &value, 8); }
    else return 1;
    *got = size; return 0;
}
int main(int argc, char **argv) {
    /* Build 104: 48 bytes before the innermost guest return address. */
    unsigned char pre[48] = {
      0x56,0x10,0xff,0xd7,0x85,0xc0,0x0f,0x85,0x01,0x01,0x00,0x00,0x8b,0x46,0x20,0x4c,
      0x8d,0x44,0x24,0x40,0x41,0x89,0x06,0x48,0x8d,0x15,0xb3,0xd1,0x0e,0x03,0x49,0x8b,
      0x4d,0x00,0x48,0xc7,0x44,0x24,0x40,0x00,0x00,0x00,0x00,0x48,0x8b,0x01,0xff,0x10};
    uint64_t ret=0x1263d263f, live_r13=0x70000000, live_rsp=0x73000000, live_rax=0x80004002;
    int e=0;
    (void)argc; mode=atoi(argv[1]); target=0x1294bf7e0;
    if (mode == 3) { int32_t rel=-0x1000; memcpy(pre+26,&rel,4); target=0x1263d162d; }
    if (mode == 4) pre[47]=0x11;
    if (mode == 5) e=1;
PROBE
    if ((mode == 4 || mode == 5) && reads) return 2;
    return 0;
}
'''.replace('PROBE', probe)
with tempfile.TemporaryDirectory(prefix='guest-qi-test-') as temp:
    src = Path(temp) / 'probe.c'
    binary = Path(temp) / 'probe'
    src.write_text(harness)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-D_GNU_SOURCE', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', str(src), '-o', str(binary)], check=True)
    env = dict(os.environ, ASAN_OPTIONS='detect_leaks=0')
    for case in range(6):
        run = subprocess.run([str(binary), str(case)], capture_output=True, text=True, env=env, check=True)
        out = run.stderr
        if case in (0, 3):
            assert 'iid={1f052807-0b46-4acc-8a89-364f793718a4}' in out, out
            assert 'slots QI=1111 AddRef=2222 Release=3333 method3=4444' in out, out
            assert 'out=0 out_read=1' in out, out
        elif case in (1, 2):
            assert 'iid_read=0' in out and 'object_read=0' in out and 'out_read=0' in out, out
            assert 'iid={' not in out and 'slots QI=' not in out, out
        else:
            assert not out, out
print('PASS: captured call, signed displacement, failed/short reads, mismatched call and outer frame')
