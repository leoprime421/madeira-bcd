#!/usr/bin/env python3
"""Shrink FEX's per-thread call-ret stack reservation from 16 MB to 8 MB.

Every emulated thread reserves CALLRET_STACK_SIZE (+2 guard pages) in the 16 GB
FEX arena [0x7c00000000, 0x8000000000). Forza Horizon 4's sign-in runs ~270
threads across eight pseudo-processes and the arena runs out: a login.live.com
renderer thread cannot start, or FEXAlloc stops the process (0xbe754993), and
the code buffers degrade to 32 MB under the pressure (device logs 2026-10-12
14:51 and 15:42).

The JIT never uses more than the middle of the reservation: every push/pop
guard (BranchOps.cpp) keeps sp in [base + 2 MB, base + 6 MB) and resets it to
base + 4 MB. 8 MB holds that window whole. The offsets that were derived from
the size (DefaultLocation = SIZE/4, the live window = SIZE/8 .. +SIZE/4) are
pinned to their old values, and the JITCallback check (Dispatcher.cpp,
(sp - base) >> 24) is tightened to the new size (>> 23).

Usage: patch-fex-ios-callret-8mb.py <FEX checkout>
"""
import sys
from pathlib import Path

root = Path(sys.argv[1])
marker = "madeira-bcd: was 0x1000000 (patch-fex-ios-callret-8mb.py)"

edits = [
    ("FEXCore/include/FEXCore/Debug/InternalThreadState.h", [
        ("static constexpr size_t CALLRET_STACK_SIZE {0x1000000};",
         "static constexpr size_t CALLRET_STACK_SIZE {0x800000};  // " + marker),
        ("static constexpr size_t CALLRET_DEFAULT_OFFSET {CALLRET_STACK_SIZE / 4};",
         "static constexpr size_t CALLRET_DEFAULT_OFFSET {0x400000};"),
        ("static constexpr size_t CALLRET_LIVE_OFFSET {CALLRET_STACK_SIZE / 8};",
         "static constexpr size_t CALLRET_LIVE_OFFSET {0x200000};"),
        ("static constexpr size_t CALLRET_LIVE_SIZE {CALLRET_STACK_SIZE / 4};",
         "static constexpr size_t CALLRET_LIVE_SIZE {0x400000};"),
    ]),
    ("Source/Windows/Common/CallRetStack.h", [
        ("Base + FEXCore::Core::InternalThreadState::CALLRET_STACK_SIZE / 4};",
         "Base + FEXCore::Core::InternalThreadState::CALLRET_DEFAULT_OFFSET};"),
    ]),
    ("FEXCore/Source/Interface/Core/Core.cpp", [
        ("CRBase + FEXCore::Core::InternalThreadState::CALLRET_STACK_SIZE / 4 - CRSp",
         "CRBase + FEXCore::Core::InternalThreadState::CALLRET_DEFAULT_OFFSET - CRSp"),
        ("uint64_t default_loc = crbase + FEXCore::Core::InternalThreadState::CALLRET_STACK_SIZE / 4;",
         "uint64_t default_loc = crbase + FEXCore::Core::InternalThreadState::CALLRET_DEFAULT_OFFSET;"),
    ]),
    ("FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp", [
        ("      sub(ARMEmitter::Size::i64Bit, TMP1, REG_CALLRET_SP, TMP1);\n"
         "      lsr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 24);",
         "      sub(ARMEmitter::Size::i64Bit, TMP1, REG_CALLRET_SP, TMP1);\n"
         "      lsr(ARMEmitter::Size::i64Bit, TMP1, TMP1, 23);  // madeira-bcd: 8 MB call-ret stack"),
    ]),
]

first = root / edits[0][0]
if marker in first.read_text():
    print("already patched")
    sys.exit(0)
texts = {}
for rel, pairs in edits:
    src = (root / rel).read_text()
    for old, new in pairs:
        if src.count(old) != 1:
            sys.exit("patch-fex-ios-callret-8mb: anchor not found exactly once in %s: %r" % (rel, old[:70]))
        src = src.replace(old, new)
    texts[rel] = src
left = texts["FEXCore/Source/Interface/Core/Core.cpp"].count("CALLRET_STACK_SIZE / 4") + \
       texts["Source/Windows/Common/CallRetStack.h"].count("CALLRET_STACK_SIZE / 4")
if left:
    sys.exit("patch-fex-ios-callret-8mb: a SIZE/4 default location is left")
for rel, src in texts.items():
    (root / rel).write_text(src)
print("patched call-ret stack to 8 MB in " + str(root))
