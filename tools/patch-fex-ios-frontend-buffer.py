#!/usr/bin/env python3
"""Shrink FEX's per-thread decoded-instruction buffer from 65536 to 8192 entries.

Every emulated thread holds a Frontend buffer of DefaultDecodedBufferSize
DecodedInst (128 bytes each): 8 MB of the 16 GB FEX arena [0x7c00000000,
0x8000000000). Forza Horizon 4's sign-in runs eight pseudo-processes and ~265
threads; at ~40 MB of arena per thread (8 MB of this, 16 MB of call-ret stack, 16
MB of IR) the arena was full and the login.live.com renderer's next thread could
not reserve its call-ret stack (device logs 2026-10-12 13:14 and 13:20). The
decoder already ends a block at MaxInst (5000 by default) and at this constant
(Frontend.cpp: FinalInstruction), so 8192 entries (1 MB) never truncate a block
the default limit allows; 7 MB per thread goes back to the arena.

Usage: patch-fex-ios-frontend-buffer.py FEX/FEXCore/Source/Interface/Core/Frontend.h
"""
import sys

path = sys.argv[1]
src = open(path).read()
new = "static constexpr size_t DefaultDecodedBufferSize = 0x2000;  // madeira-bcd: was 0x10000"
if new in src:
    print("already patched")
    sys.exit(0)
old = "static constexpr size_t DefaultDecodedBufferSize = 0x10000;"
if src.count(old) != 1:
    sys.exit("patch-fex-ios-frontend-buffer: anchor not found exactly once")
open(path, "w").write(src.replace(old, new))
print("patched " + path)
