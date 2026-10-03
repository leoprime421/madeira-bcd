#!/usr/bin/env python3
"""Exercise the production read fallback with real shared RX/RW mappings.

Runs on Linux/macOS; Wine page bookkeeping and Apple cache APIs are mocked.
The kernel read/pread, file positions, and memory contents are real.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
src = (root / 'build/ntdll-unix/virtual_ios.c').read_text()
start = src.index('static BOOL ios_jit_file_read(')
end = src.index('ssize_t virtual_locked_recvmsg(', start)
code = src[start:end]
preamble = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
typedef int BOOL;
typedef unsigned char BYTE;
#define TRUE 1
#define FALSE 0
#define VPROT_WRITE 2
#define VPROT_GUARD 16
#define VPROT_COMMITTED 32
#define VPROT_WRITEWATCH 64
#ifndef F_GETPATH
#define F_GETPATH 0x7fffffff
#endif
#define max(a,b) ((a) > (b) ? (a) : (b))
static const uintptr_t page_mask = 4095;
static int use_kernel_writewatch, virtual_mutex, held, watch_updates, flushes;
static unsigned char *guest, *writer;
static size_t span = 0x1400000, watched_bytes;
static int live = 1;
static BYTE flags[0x1400000 / 4096];
static void *find_view(void *p, size_t n) {
    uintptr_t a = (uintptr_t)p, b = (uintptr_t)guest;
    return live && a >= b && n <= span && a - b <= span - n ? guest : NULL;
}
static int ios_jit_anon_alias_find_cover(void *p,size_t n,void **rw,void **rx) {
    assert(held);
    if (!find_view(p,n)) return 0;
    *rw = writer + ((uintptr_t)p - (uintptr_t)guest); *rx = p; return 1;
}
static BYTE get_page_vprot(void *p) {
    return flags[((uintptr_t)p - (uintptr_t)guest) / 4096];
}
static void sys_dcache_flush(void *p, size_t n) { (void)p; assert(n); flushes++; errno=EDOM; }
static void sys_icache_invalidate(void *p, size_t n) { (void)p; assert(n); errno=EDOM; }
static void ios_jit_anon_alias_note_write(unsigned long long p) { (void)p; }
static void update_write_watches(void *p,size_t n,size_t got) {
    (void)p; assert(got <= n); watch_updates++; watched_bytes=got;
}
static void server_enter_uninterrupted_section(int *m,sigset_t *s) {
    (void)m;(void)s; assert(!held); held=1;
}
static void server_leave_uninterrupted_section(int *m,sigset_t *s) {
    (void)m;(void)s; assert(held); held=0; errno=ERANGE;
}
static int check_write_access(void *p,size_t n,BOOL *watch) {
    (void)p;(void)n;(void)watch; return 1;
}
'''
main = r'''
int main(void) {
    FILE *backing=tmpfile(), *input=tmpfile();
    assert(backing && input);
    int fd=fileno(input);
    assert(!ftruncate(fileno(backing),span));
    writer=mmap(NULL,span,PROT_READ|PROT_WRITE,MAP_SHARED,fileno(backing),0);
    guest=mmap(NULL,span,PROT_READ,MAP_SHARED,fileno(backing),0);
    assert(writer!=MAP_FAILED && guest!=MAP_FAILED);
    assert(write(fd,"abcdefgh",8)==8);
    flags[0]=flags[1]=VPROT_COMMITTED|VPROT_WRITE;
    assert(lseek(fd,0,SEEK_SET)==0);
    assert(read(fd,guest,8)==-1 && errno==EFAULT);
    assert(lseek(fd,0,SEEK_CUR)==0);
    assert(virtual_locked_read(fd,guest,8)==8);
    assert(!memcmp(guest,"abcdefgh",8));
    assert(lseek(fd,0,SEEK_CUR)==8 && !held && flushes==1);
    assert(lseek(fd,1,SEEK_SET)==1);
    assert(virtual_locked_pread(fd,guest+16,4,2)==4);
    assert(!memcmp(guest+16,"cdef",4) && lseek(fd,0,SEEK_CUR)==1);
    flags[0]|=VPROT_WRITEWATCH;
    assert(virtual_locked_pread(fd,guest+32,12,0)==8);
    assert(watch_updates==1 && watched_bytes==8);
    assert(!memcmp(guest+32,"abcdefgh",8));
    int old_flush=flushes;
    assert(virtual_locked_pread(fd,guest,8,8)==0 && flushes==old_flush);
    for (int i=0;i<3;i++) {
        flags[0]=i==0 ? VPROT_COMMITTED : i==1 ? VPROT_WRITE :
                 VPROT_COMMITTED|VPROT_WRITE|VPROT_GUARD;
        assert(lseek(fd,0,SEEK_SET)==0);
        assert(virtual_locked_read(fd,guest,8)==-1 && errno==EFAULT);
        assert(lseek(fd,0,SEEK_CUR)==0);
    }
    flags[0]=VPROT_COMMITTED|VPROT_WRITE;
    flags[1]=VPROT_COMMITTED;
    assert(virtual_locked_pread(fd,guest+4092,8,0)==-1 && errno==EFAULT);
    live=0;
    assert(virtual_locked_pread(fd,guest,8,0)==-1 && errno==EFAULT);
    live=1;
    assert(virtual_locked_read(-1,guest,8)==-1 && errno==EBADF);
    assert(virtual_locked_pread(fd,guest,8,-1)==-1 && errno==EINVAL);
    ssize_t result; int error;
    held=1;
    assert(!ios_jit_file_read(fd,(void *)(UINTPTR_MAX-3),8,1,0,&result,&error));
    assert(!ios_jit_file_read(fd,guest+span-4,8,1,0,&result,&error));
    assert(ios_jit_file_read(-1,guest,8,1,0,&result,&error));
    assert(result==-1 && error==EBADF);
    held=0;
    char ordinary[8];
    assert(virtual_locked_pread(fd,ordinary,8,0)==8);
    assert(!memcmp(ordinary,"abcdefgh",8));
    assert(virtual_locked_read(fd,guest,0)==0);
    assert(flushes==old_flush && !held);
    /* The exact read size in the GTA log, with every byte verified. */
    size_t big=20959232;
    unsigned char *payload=malloc(big);
    assert(payload);
    for (size_t i=0;i<big;i++) payload[i]=(unsigned char)(i*31+7);
    for (size_t i=0;i<sizeof(flags);i++) flags[i]=VPROT_COMMITTED|VPROT_WRITE;
    assert(pwrite(fd,payload,big,0)==(ssize_t)big);
    assert(virtual_locked_pread(fd,guest,big,0)==(ssize_t)big);
    assert(!memcmp(guest,payload,big));
    free(payload);
    munmap(guest,span); munmap(writer,span); fclose(input); fclose(backing);
    puts("PASS: alias reads, offsets, short reads, EOF, permissions, guards, bounds, errno and ordinary reads");
}
'''
with tempfile.TemporaryDirectory() as td:
    path = Path(td)
    (path/'test.c').write_text(preamble + code + main)
    subprocess.run([os.environ.get('CC','cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    str(path/'test.c'), '-o', str(path/'test')], check=True)
    subprocess.run([str(path/'test')], check=True)
