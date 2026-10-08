#!/bin/bash
# Ship the ARM64EC Media Foundation decoder frontends and winegstreamer PE
# client. Build only PE targets: GStreamer is not available on iOS.
set -eu
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MINGW="${MINGW:-$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin}"
export PATH="$MINGW:$PATH"
B="${WINE_EC_BUILD:-$R/wine/build-arm64ec}"
SHIP="${SHIP_DIR:-$R/app/Madeira/arm64ec-windows}"
TOOLS="${1:-}"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"
mkdir -p "$B" "$SHIP"
# Reconfigure cached trees too: the extra-DLL build used to disable this module.
( cd "$B" && "$R/wine/configure" --enable-archs=arm64ec --without-x --disable-tests \
    --without-freetype --without-gnutls --enable-winegstreamer ${TOOLS:+--with-wine-tools="$TOOLS"} ) > "$B.media-config.log" 2>&1 \
    || { tail -30 "$B.media-config.log"; exit 1; }
DLLS="winegstreamer msmpeg2vdec msauddecmft"
TARGETS=""
for d in $DLLS; do TARGETS="$TARGETS dlls/$d/arm64ec-windows/$d.dll"; done
make -C "$B" -j"$JOBS" $TARGETS > "$B.media-build.log" 2>&1 \
    || { tail -50 "$B.media-build.log"; exit 1; }
for d in $DLLS; do
    cp "$B/dlls/$d/arm64ec-windows/$d.dll" "$SHIP/$d.dll.tmp"
    "$MINGW/llvm-strip" "$SHIP/$d.dll.tmp"
    python3 - "$SHIP/$d.dll.tmp" <<'PY'
import struct, sys
p = sys.argv[1]
d = open(p, 'rb').read()
pe = struct.unpack_from('<I', d, 0x3c)[0]
target = struct.unpack_from('<I', d, pe + 24 + 56)[0] + 0x10000
if len(d) < target:
    with open(p, 'ab') as f:
        f.write(b'\0' * (target - len(d)))
PY
    mv "$SHIP/$d.dll.tmp" "$SHIP/$d.dll"
done
echo "::notice::ARM64EC H.264/AAC decoder frontends and winegstreamer built and shipped"
