#!/bin/bash
# Build FEX's aarch64 WOW64 module (libwow64fex.dll, shipped as
# app/Madeira/aarch64-windows/xtajit.dll -- the CPU backend wow64.dll loads for
# 32-bit x86 programs) from the FEX submodule with madeira-bcd's patches that
# apply to it, and ship it over the committed copy. Options are
# build/fex-wow64/build.sh's (FEX_IOS_HOST_BUILD turns on the 32-bit guest
# window). Run from the repo root; on a failed build the committed module stays.
#
#  - patch-fex-ios-cpuid-index.py: CPUID 0x80000002-4 indexed the per-CPU table
#    with the raw host CPU number; 32-bit Crysis died in strlen(0xfff68000)
#    inside Function_8000_0002h before its first frame (log 2026-09-30 11:57).
#  - patch-fex-ios-teb-tsd.py: the WinAPI shims' GetCurrentTEB() read x18,
#    which is 0 on some iOS threads (32-bit Crysis, TlsGetValue, log 12:25).
set -eu
R="$(pwd)"
MINGW="${MINGW:-$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}"
export PATH="$MINGW:$PATH"
B="${FEX_WOW64_BUILD:-$R/FEX/build-wow64}"
SHIP="$R/app/Madeira/aarch64-windows/xtajit.dll"
CPUIDF="FEXCore/Source/Interface/Core/CPUID.cpp"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"

git -C FEX diff --name-only | while read -r f; do git -C FEX checkout -- "$f"; done
python3 "$R/tools/patch-fex-ios-cpuid-index.py" "$R/FEX/$CPUIDF"
python3 "$R/tools/patch-fex-ios-teb-tsd.py" "$R/FEX/Source/Windows/Common/Priv.h"
python3 "$R/tools/patch-fex-virtualprotect-result.py" "$R/FEX/FEXCore/include/FEXCore/Utils/AllocatorHooks.h"
restore() { git -C "$R/FEX" checkout -- "$CPUIDF" Source/Windows/Common/Priv.h FEXCore/include/FEXCore/Utils/AllocatorHooks.h; }
trap restore EXIT

cmake -S "$R/FEX" -B "$B" -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_TOOLCHAIN_FILE="$R/FEX/Data/CMake/toolchain_mingw.cmake" \
    -DMINGW_TRIPLE=aarch64-w64-mingw32 \
    -DFEX_IOS_HOST_BUILD=ON -DCMAKE_C_FLAGS=-DFEX_IOS_HOST \
    -DCMAKE_CXX_FLAGS=-DFEX_IOS_HOST -DCMAKE_ASM_FLAGS=-DFEX_IOS_HOST \
    -DENABLE_LTO=OFF -DENABLE_ASSERTIONS=OFF -DENABLE_JEMALLOC_GLIBC_ALLOC=OFF \
    -DBUILD_TESTING=OFF -DBUILD_FEXCONFIG=OFF -DENABLE_CCACHE=OFF -DBUILD_THUNKS=OFF \
    -DTUNE_ARCH=generic -DTUNE_CPU=none \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 > "$B.cfg.log" 2>&1 \
    || { tail -30 "$B.cfg.log"; exit 1; }
cmake --build "$B" --target wow64fex -j"$JOBS" > "$B.build.log" 2>&1 \
    || { grep -m 20 "error" "$B.build.log"; exit 1; }

"$MINGW/llvm-readobj" --coff-exports "$B/Bin/libwow64fex.dll" | awk '$1 == "Name:" { print $2 }' | sort > "$B.new-exports"
"$MINGW/llvm-readobj" --coff-exports "$SHIP" | awk '$1 == "Name:" { print $2 }' | sort > "$B.old-exports"
if ! diff "$B.old-exports" "$B.new-exports"; then
    echo "::warning::the rebuilt WOW64 module exports differ from the committed xtajit.dll; keeping the committed one"
    exit 1
fi
cp "$B/Bin/libwow64fex.dll" "$SHIP"
echo "::notice::xtajit.dll (WOW64) built from FEX $(git -C FEX rev-parse --short HEAD) with the CPUID index wrap and the TSD-slot TEB for the WinAPI shims, and shipped"
