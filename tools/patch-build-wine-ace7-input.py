#!/usr/bin/env python3
"""Ace Combat 7 input compatibility for Madeira's pinned Wine.

The iOS DirectInput joystick is deliberately hidden by default because the same
physical pad is normally exposed through XInput.  Ace Combat 7 creates the
DirectInput worker and, in Build 172, receives neither the pad nor keyboard
through that path.  The session log also shows the frontend keyboard events
arriving successfully, so the missing piece is downstream of iOS input capture.

Keep the workaround title-local by keying it off MADEIRA_INITIAL_CWD, which the
launcher publishes before Wine enters the PE.  For ACE COMBAT 7 only:
  * expose Madeira's iOS DirectInput joystick even without MADEIRA_DINPUT_PAD=1;
  * use the legacy DirectInput keyboard hook instead of RawInput/HID, because
    the iOS-injected key path has no backing HID device to feed WM_INPUT.

Rebuild and ship only dinput.dll.
"""

from pathlib import Path
import os
import shutil
import struct
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WINE = ROOT / "wine"
BUILD = WINE / "build-arm64ec"
SHIP = ROOT / "app/Madeira/arm64ec-windows"


def replace_once(path: Path, old: str, new: str, marker: str, label: str) -> None:
    s = path.read_text(encoding="utf-8")
    if marker in s:
        print(f"ace7 input: {label} already patched")
        return
    count = s.count(old)
    if count != 1:
        raise SystemExit(f"ace7 input: {label} anchor changed (count={count})")
    path.write_text(s.replace(old, new, 1), encoding="utf-8")
    print(f"ace7 input: patched {label}")


# Controller: keep the global default OFF, but auto-enable it for ACE COMBAT 7.
joystick = WINE / "dlls/dinput/joystick_ios.c"
old_pad = '''    e = getenv( "MADEIRA_DINPUT_PAD" );
    off = (e && e[0] == '1') ? 0 : 1;
'''
new_pad = '''    e = getenv( "MADEIRA_DINPUT_PAD" );
    off = (e && e[0] == '1') ? 0 : 1;
    /* madeira-bcd: Ace Combat 7 uses DirectInput for its controller path.
     * MADEIRA_INITIAL_CWD is set by WineProcessBridge before PE startup. */
    if (off)
    {
        const char *cwd = getenv( "MADEIRA_INITIAL_CWD" );
        if (cwd && (strstr( cwd, "ACE COMBAT 7" ) || strstr( cwd, "Ace Combat 7" )))
        {
            off = 0;
            ERR( "[ace7-input] exposing iOS joystick to DirectInput\\n" );
        }
    }
'''
replace_once(joystick, old_pad, new_pad, "[ace7-input] exposing iOS joystick", "DirectInput pad gate")

# Keyboard: DInput8 defaults to RawInput. Madeira's frontend keys are injected
# into the Wine user-input path but Build 172 has no working HID/WM_INPUT source
# for Ace7, while drv_post_key succeeds. Let DInput consume the legacy keyboard
# hook for this title so the same injected key stream updates device_state.
keyboard = WINE / "dlls/dinput/keyboard.c"
old_kbd = '''    if (dinput->dwVersion >= 0x0800) impl->base.use_raw_input = TRUE;
'''
new_kbd = '''    if (dinput->dwVersion >= 0x0800) impl->base.use_raw_input = TRUE;
    {
        const char *cwd = getenv( "MADEIRA_INITIAL_CWD" );
        if (cwd && (strstr( cwd, "ACE COMBAT 7" ) || strstr( cwd, "Ace Combat 7" )))
        {
            impl->base.use_raw_input = FALSE;
            ERR( "[ace7-input] DirectInput keyboard using legacy hook (RawInput disabled)\\n" );
        }
    }
'''
replace_once(keyboard, old_kbd, new_kbd, "[ace7-input] DirectInput keyboard", "DirectInput keyboard route")

if not (BUILD / "Makefile").exists():
    print("ace7 input: build-arm64ec absent; source patches complete, rebuild skipped")
    raise SystemExit(0)

target = "dlls/dinput/arm64ec-windows/dinput.dll"
out = BUILD / target
if out.exists():
    out.unlink()

jobs = str(max(2, os.cpu_count() or 4))
log = ROOT / "build" / "ace7-input-dinput-arm64ec.log"
log.parent.mkdir(parents=True, exist_ok=True)
print(f"ace7 input: rebuilding dinput.dll (-j{jobs})")
with log.open("wb") as f:
    proc = subprocess.run(["make", "-C", str(BUILD), f"-j{jobs}", target],
                          cwd=ROOT, stdout=f, stderr=subprocess.STDOUT)
if proc.returncode:
    tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
    raise SystemExit("ace7 input: dinput rebuild failed (%d)\n%s" % (proc.returncode, "\n".join(tail)))
if not out.exists():
    raise SystemExit(f"ace7 input: expected output missing: {out}")

mingw_strip = ROOT / "toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin/llvm-strip"
strip = str(mingw_strip) if mingw_strip.exists() else shutil.which("llvm-strip")
if not strip:
    raise SystemExit("ace7 input: llvm-strip not found")

tmp = SHIP / "dinput.dll.ace7.tmp"
shutil.copy2(out, tmp)
subprocess.run([strip, str(tmp)], check=True)

data = tmp.read_bytes()
pe = struct.unpack_from("<I", data, 0x3C)[0]
if data[pe:pe + 4] != b"PE\0\0":
    raise SystemExit("ace7 input: rebuilt dinput.dll has invalid PE signature")
size_of_image = struct.unpack_from("<I", data, pe + 24 + 56)[0]
target_size = size_of_image + 0x10000
if len(data) < target_size:
    with tmp.open("ab") as f:
        f.write(b"\0" * (target_size - len(data)))
os.replace(tmp, SHIP / "dinput.dll")
print("ace7 input: shipped rebuilt dinput.dll")
