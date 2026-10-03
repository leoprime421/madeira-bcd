#!/usr/bin/env python3
"""Avoid ARM64EC pool-copy reads through relocatable alias globals.

GTA V build 0.1.24 reaches libarm64ecfex.dll and faults at a generated
load equivalent to:

    ldr x9, [x10, x9, lsl #3]

with x10 == 0.  The FEX ARM64EC module already avoids several global-object
patterns on iOS because PE .data/.rdata relocations are consumed through a
JIT-pool copy.  IosJitAlias.cpp still adds two unnecessary indirections:

    g_Entries    -> IosAliasEntries
    g_EntryCount -> IosAliasCount

Both objects live in the same translation unit.  Use the real extern "C"
storage directly so the hot alias lookup cannot depend on an intermediate
pointer/reference relocation being valid in the executable pool copy.

This is deliberately source-only and build-time: the FEX submodule stays
pinned and tools/build-xtajit64.sh restores the source after producing the
module.
"""
from pathlib import Path
import sys

path = Path(sys.argv[1] if len(sys.argv) > 1
            else "FEX/Source/Windows/ARM64EC/IosJitAlias.cpp")
src = path.read_text()

marker = "MADEIRA_IOS_ALIAS_DIRECT_STORAGE"
if marker in src:
    print(f"{path}: direct alias storage patch already present")
    raise SystemExit(0)

block = """namespace {
IosAliasEntry* const g_Entries = IosAliasEntries;
volatile int& g_EntryCount = IosAliasCount;
}  // namespace
"""

if src.count(block) != 1:
    raise SystemExit(
        f"{path}: alias-indirection block count={src.count(block)}, expected 1"
    )

# Verify the pinned source still has the shape this patch was written for.
entries_uses = src.count("g_Entries")
count_uses = src.count("g_EntryCount")
if entries_uses < 2 or count_uses < 2:
    raise SystemExit(
        f"{path}: unexpected alias usage counts "
        f"g_Entries={entries_uses} g_EntryCount={count_uses}"
    )

replacement = """/* MADEIRA_IOS_ALIAS_DIRECT_STORAGE
 * iOS/ARM64EC: do not introduce pointer/reference globals that merely alias
 * IosAliasEntries/IosAliasCount.  Pool-executing code must address the real
 * storage directly; an unsynchronised relocation in an intermediate global
 * turns a normal indexed lookup into a NULL-base load.
 */
"""

src = src.replace(block, replacement, 1)
src = src.replace("g_Entries", "IosAliasEntries")
src = src.replace("g_EntryCount", "IosAliasCount")

if "g_Entries" in src or "g_EntryCount" in src:
    raise SystemExit(f"{path}: stale alias-indirection name remains after patch")
if src.count(marker) != 1:
    raise SystemExit(f"{path}: marker verification failed")

path.write_text(src)
print(
    f"{path}: patched alias lookups to direct IosAliasEntries/IosAliasCount storage "
    f"(removed {entries_uses} g_Entries and {count_uses} g_EntryCount references)"
)
