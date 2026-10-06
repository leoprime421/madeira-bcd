#!/bin/bash
# Build Wine PE DLLs that upstream's arm64ec-windows set does not ship but
# games import (and, since ml2106, xinput1_1-1_4 with host rumble; see below): older VC++ runtimes (Crysis's Bin64\Crysis64.exe needs
# msvcr80), D3DX9/10/11, d3d10, avifil32, XAudio2, dinput, RichEdit (the
# Rockstar Games SDK / Launcher installers load Msftedit.dll and gdiplus.dll). Configured the way
# upstream's build/wine-pe/build-ntdll.sh configures wine/build-arm64ec;
# stripped and padded by 64 KB past SizeOfImage like the shipped builtins.
# A DLL upstream already ships is never replaced. Run from the repository
# root; needs llvm-mingw on PATH (or MINGW) and the native wine tools.
#   usage: build-wine-extra-dlls.sh [native tools dir]
set -u
R="$(pwd)"
MINGW="${MINGW:-$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}"
export PATH="$MINGW:$PATH"
TOOLS="${1:-}"
B="${WINE_EC_BUILD:-$R/wine/build-arm64ec}"
SHIP="${SHIP_DIR:-$R/app/Madeira/arm64ec-windows}"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"

WANT="msvcr70 msvcr71 msvcr80 msvcr90 msvcr100 msvcr110 msvcrt20 msvcrt40 msvcirt
      msvcp60 msvcp70 msvcp71 msvcp80 msvcp90 msvcp100 msvcp110 msvcp120
      vcomp vcomp90 vcomp100 vcomp110 vcomp120 vcomp140
      d3d10 d3d10_1 dxva2 avifil32 msvfw32 dinput
      wbemprox wbemdisp wmiutils
      riched20 riched32 msftedit gdiplus oledlg mlang usp10 cabinet sspicli msxml3 msxml6
      msasn1 wldp hnetcfg msctf xmllite cryptsp gamingtcui
      xaudio2_0 xaudio2_1 xaudio2_2 xaudio2_3 xaudio2_4 xaudio2_5 xaudio2_6 xaudio2_7 xaudio2_8 xaudio2_9
      x3daudio1_0 x3daudio1_1 x3daudio1_2 x3daudio1_3 x3daudio1_4 x3daudio1_5 x3daudio1_6 x3daudio1_7
      xapofx1_1 xapofx1_2 xapofx1_3 xapofx1_4 xapofx1_5
      d3dcompiler_33 d3dcompiler_34 d3dcompiler_35 d3dcompiler_36 d3dcompiler_37 d3dcompiler_38
      d3dcompiler_39 d3dcompiler_40 d3dcompiler_41 d3dcompiler_42 d3dcompiler_46
      d3dx10_33 d3dx10_34 d3dx10_35 d3dx10_36 d3dx10_37 d3dx10_38 d3dx10_39 d3dx10_40 d3dx10_41 d3dx10_42 d3dx10_43
      d3dx11_42 d3dx11_43"
for n in $(seq 24 42); do WANT="$WANT d3dx9_$n"; done

shipped() { ls "$SHIP" | tr 'A-Z' 'a-z' | grep -qx "$1.dll"; }
targets=""; todo=""
for d in $WANT; do
    [ -d "$R/wine/dlls/$d" ] || continue
    shipped "$d" && continue
    targets="$targets dlls/$d/arm64ec-windows/$d.dll"; todo="$todo $d"
done
# ml2106: the one exception to "never replaces a shipped DLL". xinput1_1-1_4
# (one source, dlls/xinput1_3/main.c) are rebuilt with
# tools/patch-wine-xinput-vibration.py, so XInputSetState reaches the host pad
# (docs/dualsense-output.md), and replace upstream's copies -- which were built
# from this same submodule without the patch. xinput9_1_0 forwards to
# xinput1_4 at run time. A failed build keeps the shipped copies (no rumble,
# nothing else changes). MADEIRA_XINPUT_RUMBLE_BUILD=0 skips it.
XI=""
if [ "${MADEIRA_XINPUT_RUMBLE_BUILD:-1}" != 0 ]; then
    for d in xinput1_1 xinput1_2 xinput1_3 xinput1_4; do
        [ -d "$R/wine/dlls/$d" ] || continue
        targets="$targets dlls/$d/arm64ec-windows/$d.dll"; XI="$XI $d"
    done
fi
# Forza Horizon 4: crypt32 is rebuilt with tools/patch-wine-crypt32-revocation-offline.py
# (an unreachable revocation server is not a chain error under
# MADEIRA_REVOCATION_SOFTFAIL=1, which the app sets for FH4 only) and replaces the
# shipped copy, as xinput does above. A failed build keeps the shipped crypt32.
# MADEIRA_CRYPT32_REVOCATION_BUILD=0 skips it.
CR=""
if [ "${MADEIRA_CRYPT32_REVOCATION_BUILD:-1}" != 0 ] && [ -d "$R/wine/dlls/crypt32" ]; then
    targets="$targets dlls/crypt32/arm64ec-windows/crypt32.dll"; CR="crypt32"
fi
[ -n "$todo$XI$CR" ] || { echo "nothing to build"; exit 0; }

if [ ! -f "$B/Makefile" ]; then
    mkdir -p "$B"
    ( cd "$B" && "$R/wine/configure" --enable-archs=arm64ec --without-x --disable-tests \
          --without-freetype --without-gnutls ${TOOLS:+--with-wine-tools="$TOOLS"} ) > "$B.cfg.log" 2>&1 \
        || { tail -20 "$B.cfg.log"; echo "::error::wine arm64ec configure failed"; exit 1; }
fi
# widl looks for imported typelibs (stdole2.tlb) under aarch64-windows, its
# arch dir for ARM64EC, but an arm64ec-only tree builds them under
# arm64ec-windows; without this riched20, hnetcfg and wbemdisp fail with
# "cannot find stdole2.tlb".
mkdir -p "$B/dlls/stdole2.tlb" && ln -sfn arm64ec-windows "$B/dlls/stdole2.tlb/aarch64-windows"
# msvcr*: mirror the data exports into the PE mapping (see the script).
python3 "$R/tools/patch-wine-msvcrt-datasync.py" "$R/wine/dlls/msvcrt/main.c"
xi_patched=0
if [ -n "$XI" ]; then
    python3 "$R/tools/patch-wine-xinput-vibration.py" "$R/wine/dlls/xinput1_3/main.c" && xi_patched=1
    # Old objects from an unpatched build must not satisfy make.
    for d in $XI; do rm -f "$B/dlls/$d/arm64ec-windows/$d.dll" "$B/dlls/$d"/arm64ec-windows/*.o; done
fi
cr_patched=0
if [ -n "$CR" ]; then
    python3 "$R/tools/patch-wine-crypt32-revocation-offline.py" "$R/wine/dlls/crypt32/chain.c" && cr_patched=1
    rm -f "$B/dlls/crypt32/arm64ec-windows/crypt32.dll" "$B/dlls/crypt32"/arm64ec-windows/*.o
fi
make -C "$B" -k -j"$JOBS" $targets > "$B.build.log" 2>&1
git -C "$R/wine" checkout -- dlls/msvcrt/main.c dlls/xinput1_3/main.c dlls/crypt32/chain.c
if [ "$cr_patched" = 1 ]; then
    f="$B/dlls/crypt32/arm64ec-windows/crypt32.dll"
    if [ -f "$f" ]; then
        cp "$f" "$SHIP/crypt32.dll.tmp"
        "$MINGW/llvm-strip" "$SHIP/crypt32.dll.tmp"
        python3 - "$SHIP/crypt32.dll.tmp" <<'PY2'
import struct, sys
p = sys.argv[1]; d = open(p, 'rb').read()
pe = struct.unpack_from('<I', d, 0x3c)[0]
target = struct.unpack_from('<I', d, pe + 24 + 56)[0] + 0x10000
if len(d) < target:
    open(p, 'ab').write(b'\0' * (target - len(d)))
PY2
        mv "$SHIP/crypt32.dll.tmp" "$SHIP/crypt32.dll"
        echo "::notice::crypt32 rebuilt with the offline-revocation switch (MADEIRA_REVOCATION_SOFTFAIL) and shipped"
    else
        echo "::warning::crypt32 revocation build failed; shipped crypt32.dll kept"
        grep -m 10 "crypt32" "$B.build.log"
    fi
elif [ -n "$CR" ]; then
    echo "::warning::crypt32 revocation patch did not apply; shipped crypt32.dll kept"
fi
xi_built=0; xi_failed=""
if [ "$xi_patched" = 1 ]; then
    for d in $XI; do
        f="$B/dlls/$d/arm64ec-windows/$d.dll"
        if [ ! -f "$f" ]; then xi_failed="$xi_failed $d"; continue; fi
        cp "$f" "$SHIP/$d.dll.tmp"
        "$MINGW/llvm-strip" "$SHIP/$d.dll.tmp"
        python3 - "$SHIP/$d.dll.tmp" <<'PY'
import struct, sys
p = sys.argv[1]; d = open(p, 'rb').read()
pe = struct.unpack_from('<I', d, 0x3c)[0]
target = struct.unpack_from('<I', d, pe + 24 + 56)[0] + 0x10000
if len(d) < target:
    open(p, 'ab').write(b'\0' * (target - len(d)))
PY
        mv "$SHIP/$d.dll.tmp" "$SHIP/$d.dll"
        xi_built=$((xi_built + 1))
    done
    echo "::notice::xinput with host rumble (ml2106): replaced $xi_built shipped DLLs${xi_failed:+ (failed, shipped copy kept:$xi_failed)}"
elif [ -n "$XI" ]; then
    echo "::warning::xinput rumble patch did not apply; shipped xinput DLLs kept"
fi
built=0; failed=""
for d in $todo; do
    f="$B/dlls/$d/arm64ec-windows/$d.dll"
    if [ ! -f "$f" ]; then failed="$failed $d"; continue; fi
    cp "$f" "$SHIP/$d.dll.tmp"
    "$MINGW/llvm-strip" "$SHIP/$d.dll.tmp"
    python3 - "$SHIP/$d.dll.tmp" <<'PY'
import struct, sys
p = sys.argv[1]; d = open(p, 'rb').read()
pe = struct.unpack_from('<I', d, 0x3c)[0]
target = struct.unpack_from('<I', d, pe + 24 + 56)[0] + 0x10000
if len(d) < target:
    open(p, 'ab').write(b'\0' * (target - len(d)))
PY
    mv "$SHIP/$d.dll.tmp" "$SHIP/$d.dll"
    built=$((built + 1))
done
echo "::notice::built $built extra Wine DLLs for arm64ec${failed:+ (failed:$failed)}"
[ -n "$failed" ] && grep -m 10 "error" "$B.build.log"
exit 0
