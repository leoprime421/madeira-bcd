#!/usr/bin/env python3
"""Forza Horizon 4: ForzaWebHelper.exe children get their own Chromium log; no Wine runs.

Compiles fh4_cef_child_args from build/ntdll-unix/process_ios.c and checks that only
ForzaWebHelper.exe with a --type= is touched, that switches the game already passes are
not repeated, that the GPU process alone gets --v=1, and that a too small buffer yields 0.
Needs python3 and a C compiler."""
from pathlib import Path
import subprocess, sys, tempfile, os

root = Path(__file__).resolve().parents[2]
proc = (root / 'build/ntdll-unix/process_ios.c').read_text()


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index('\n}', start) + 2] + '\n'


create = function(proc, 'NTSTATUS WINAPI NtCreateUserProcess(')
assert 'fh4_cef_child_args(' in create and create.index('fh4_cef_child_args(') < create.index('create_startup_info( attr.ObjectName')

code = '''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef unsigned short WCHAR;
''' + ''.join(function(proc, s) for s in ('static int sc_switch_end(', 'static int sc_image_is(', 'static int fh4_cef_child_args(')) + r'''
static int run( const char *img, const char *cl, unsigned n, char *out, int cap )
{
    WCHAR wi[512], wc[1024]; int i, li = (int)strlen( img ), lc = (int)strlen( cl );
    for (i = 0; i < li; i++) wi[i] = (unsigned char)img[i];
    for (i = 0; i < lc; i++) wc[i] = (unsigned char)cl[i];
    return fh4_cef_child_args( wi, li, wc, lc, n, out, cap );
}
#define FAIL(...) do { fprintf( stderr, __VA_ARGS__ ); exit( 1 ); } while (0)
int main( void )
{
    char o[200];
    const char *fw = "\\??\\C:\\Program Files (x86)\\Forza Horizon 4\\gamedata\\ForzaWebHelper.exe";
    if (run( fw, "\"x\\ForzaWebHelper.exe\" --type=gpu-process --no-sandbox", 3, o, sizeof(o) ) == 0) FAIL( "gpu: none\n" );
    if (strcmp( o, " --enable-logging --log-severity=info --log-file=C:\\fh4_cef_3_gpu-process.log --v=1" ))
        FAIL( "gpu: [%s]\n", o );
    if (!run( fw, "x --type=utility --utility-sub-type=network.mojom.NetworkService", 4, o, sizeof(o) ) ||
        strcmp( o, " --enable-logging --log-severity=info --log-file=C:\\fh4_cef_4_utility.log" )) FAIL( "utility: [%s]\n", o );
    if (!run( fw, "x --type=renderer --enable-logging --log-severity=warning", 5, o, sizeof(o) ) ||
        strcmp( o, " --log-file=C:\\fh4_cef_5_renderer.log" )) FAIL( "renderer: [%s]\n", o );
    if (run( fw, "x --type=renderer --enable-logging --log-severity=info --log-file=C:\\a.log", 6, o, sizeof(o) ) != 0)
        FAIL( "all present must add nothing\n" );
    if (run( fw, "x --enable-logging", 7, o, sizeof(o) ) != 0) FAIL( "browser (no --type=) touched\n" );
    if (run( "C:\\x\\steamwebhelper.exe", "x --type=gpu-process", 8, o, sizeof(o) ) != 0) FAIL( "other helper touched\n" );
    if (run( "C:\\x\\NotForzaWebHelper.exe", "x --type=gpu-process", 9, o, sizeof(o) ) != 0) FAIL( "look-alike touched\n" );
    if (run( fw, "x --no-type=gpu-process", 10, o, sizeof(o) ) != 0) FAIL( "--no-type= taken for --type=\n" );
    if (run( fw, "x --type=gpu-process", 11, o, 20 ) != 0) FAIL( "small buffer must give 0\n" );
    printf( "ok\n" );
    return 0;
}
'''
with tempfile.TemporaryDirectory() as d:
    src, exe = os.path.join(d, 't.c'), os.path.join(d, 't')
    open(src, 'w').write(code)
    cc = subprocess.run(['cc', '-Wall', '-Wno-unused-function', '-fsanitize=address,undefined', '-o', exe, src],
                        capture_output=True, text=True)
    if cc.returncode:
        print(cc.stderr); sys.exit(1)
    r = subprocess.run([exe], capture_output=True, text=True)
    sys.stdout.write(r.stdout + r.stderr)
    if r.returncode or 'ok' not in r.stdout:
        print('FAILED'); sys.exit(1)
print('PASS: ForzaWebHelper.exe children (and only they) get --enable-logging/--log-severity/--log-file, the GPU process --v=1')
