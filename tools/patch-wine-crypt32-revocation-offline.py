#!/usr/bin/env python3
"""crypt32: an unreachable revocation server is not a chain error when
MADEIRA_REVOCATION_SOFTFAIL=1 (set by the app for Forza Horizon 4 only).

Forza Horizon 4, device log 2026-10-05 21:19 (build 81): after Enter on Continue
(+86 s) the game's own process opens an HTTPS connection (+89 s); half a second
later crypt32 asks ocsp.digicert.com / crl3.digicert.com for revocation data,
both resolve to 0.0.0.0 and the fetch fails (wininet 12029). The game retries
with a growing delay (+91, +96, +105, +122, +148 s, again from +189 s) and stays
on "Please wait".

CRYPT_VerifyChainRevocation maps CRYPT_E_REVOCATION_OFFLINE (server could not be
reached) to CERT_TRUST_REVOCATION_STATUS_UNKNOWN | CERT_TRUST_IS_OFFLINE_REVOCATION.
With the switch on, only that case adds no error bits. A revoked certificate
(CRYPT_E_REVOKED), an untrusted root, an expired certificate or a wrong host name
still fail exactly as before; without the switch nothing changes.

Usage: patch-wine-crypt32-revocation-offline.py wine/dlls/crypt32/chain.c
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: MADEIRA_REVOCATION_SOFTFAIL"
if marker in src:
    print("already patched")
    sys.exit(0)

anchor = "WINE_DEFAULT_DEBUG_CHANNEL(crypt);\n"
case = ("                    case CRYPT_E_REVOCATION_OFFLINE:\n"
        "                    case CRYPT_E_NO_REVOCATION_CHECK:\n")
if src.count(anchor) != 1 or src.count(case) != 1:
    sys.exit("patch-wine-crypt32-revocation-offline: anchors not found exactly once")

helper = anchor + '''
/* ''' + marker + ''': see tools/patch-wine-crypt32-revocation-offline.py. */
static BOOL madeira_revocation_softfail(void)
{
    static LONG state = -1;
    if (state < 0)
    {
        char value[4];
        DWORD n = GetEnvironmentVariableA( "MADEIRA_REVOCATION_SOFTFAIL", value, sizeof(value) );
        state = (n && n < sizeof(value) && value[0] == '1');
    }
    return state;
}
'''
src = src.replace(anchor, helper, 1)
src = src.replace(case, (
    "                    case CRYPT_E_REVOCATION_OFFLINE:\n"
    "                        if (madeira_revocation_softfail())\n"
    "                        {\n"
    "                            WARN(\"revocation server unreachable; not an error (MADEIRA_REVOCATION_SOFTFAIL)\\n\");\n"
    "                            error = 0;\n"
    "                            break;\n"
    "                        }\n"
    "                        /* fall through */\n"
    "                    case CRYPT_E_NO_REVOCATION_CHECK:\n"), 1)
open(path, "w").write(src)
print("patched " + path)
