#!/usr/bin/env python3
"""Raise FEX's iOS ARM64EC alias table from 256 to 1024 entries.

BTCpu64IosAddAliasMapping keeps one IosAliasEntry (32 bytes) per pool-copied image.
The unix side drains EVERY registered mapping of the whole process tree into an
emulator when it registers, so each new pseudo-process starts with more entries than
the one before: Forza Horizon 4's CEF children got 105, 182 and then 255 mappings
(device log 2026-10-05 11:22, build 73). The third ForzaWebHelper.exe (the software
GPU process Chromium starts after the first one fails) filled the table at its second
DLL (vcruntime140.dll, "IosAliasEntries FULL (256 entries)") and died executing a
non-executable stack page one step later. Module.S walks IosAliasCount entries, so
only this constant changes (1024 entries = 32 KB of .bss). The log line names the
capacity so tools/build-xtajit64.sh's caller can verify the shipped module has it.

Usage: patch-fex-ios-alias-capacity.py FEX/Source/Windows/ARM64EC/IosJitAlias.cpp
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "FULL (cap 1024; {} entries)"
if marker in src:
    print("already patched")
    sys.exit(0)

old_cap = "constexpr int kMaxEntries = 256;"
old_msg = "IosAliasEntries FULL ({} entries)"
if src.count(old_cap) != 1 or src.count(old_msg) != 1:
    sys.exit("patch-fex-ios-alias-capacity: anchors not found exactly once")
src = src.replace(old_cap, "constexpr int kMaxEntries = 1024;  // madeira-bcd: was 256 (see tools/patch-fex-ios-alias-capacity.py)")
src = src.replace(old_msg, "IosAliasEntries FULL (cap 1024; {} entries)")
open(path, "w").write(src)
print("patched " + path)
