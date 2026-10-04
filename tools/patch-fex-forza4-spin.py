#!/usr/bin/env python3
"""Repair FH4's observed startup writer/poll spin in the iOS ARM64EC FEX JIT.

The build-44 trace is stable at ForzaHorizon4.exe+0x3049780.  Its translated
ARM64 sequence branches around the only writer that sets the polled byte, then
spins forever waiting for that byte.  For FH4 only, recognize the exact emitted
sequence and NOP that one B.NE so the game's own release-store writer executes.

This does NOT delete the polling loop and does not alter other games.
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: FH4 writer-path spin repair"
if marker in src:
    print("already patched")
    sys.exit(0)

if "#include <cstdlib>\n" not in src:
    include_anchor = "#include <cstring>\n"
    if src.count(include_anchor) != 1:
        sys.exit("patch-fex-forza4-spin: include anchor not found exactly once")
    src = src.replace(include_anchor, include_anchor + "#include <cstdlib>\n", 1)

anchor = "    memcpy(WritePtr(GetCursorAddress<uint8_t*>()), TempCodeBuffer, TempSize);\n"
if src.count(anchor) != 1:
    sys.exit("patch-fex-forza4-spin: JIT memcpy anchor not found exactly once")

injected = r'''#ifdef FEX_IOS_HOST
    /* madeira-bcd: FH4 writer-path spin repair.
     *
     * Exact sequence observed in build 44:
     *   cmp   w20,#0
     *   b.ne  wait
     *   mov   w6,#1
     *   add   x7,x29,#0x2b0
     *   stlrb w6,[x7]
     *   ... load a global byte address ...
     *   stlrb w6,[x7]
     * wait:
     *   add   x6,x29,#0x2b0
     *   ldarb/ldaprb w8,[x6]
     *   tst   w8,#0xff
     *   b.eq  wait
     *
     * The bad startup takes B.NE and nobody executes the writer.  NOP only
     * that branch, preserving both release stores and the poll itself. */
    if (const char* FH4SpinFix = std::getenv("MADEIRA_FEX_FH4_SPIN_FIX");
        FH4SpinFix && FH4SpinFix[0] == '1') {
      auto* Words = reinterpret_cast<uint32_t*>(TempCodeBuffer);
      const size_t WordCount = TempSize / sizeof(uint32_t);

      for (size_t I = 0; I + 14 < WordCount; ++I) {
        const bool AcquireByte =
          Words[I + 10] == 0x08dffcc8u || /* ldarb  w8,[x6] */
          Words[I + 10] == 0x38bfc0c8u;   /* ldaprb w8,[x6] */

        if (Words[I + 0] == 0x7100029fu && /* cmp w20,#0 */
            Words[I + 1] == 0x54000101u && /* b.ne +0x20 */
            Words[I + 2] == 0x52800026u && /* mov w6,#1 */
            Words[I + 3] == 0x910ac3a7u && /* add x7,x29,#0x2b0 */
            Words[I + 4] == 0x089ffce6u && /* stlrb w6,[x7] */
            Words[I + 8] == 0x089ffce6u && /* stlrb w6,[x7] */
            Words[I + 9] == 0x910ac3a6u && /* add x6,x29,#0x2b0 */
            AcquireByte &&
            Words[I + 11] == 0x72001d1fu && /* tst w8,#0xff */
            Words[I + 12] == 0x54ffffa0u && /* b.eq -0xc */
            Words[I + 13] == 0x71000289u) { /* subs w9,w20,#0 */
          Words[I + 1] = 0xd503201fu;       /* NOP: run the normal writer path */

          static volatile uint32_t FH4SpinPatchCount = 0;
          const uint32_t N = __sync_add_and_fetch(&FH4SpinPatchCount, 1);
          if (N <= 16) {
            LogMan::Msg::EFmt(
              "[fh4-spin] writer-path repair #{} entry={:x} temp+0x{:x}: "
              "B.NE -> NOP; writer+poll preserved rev=ml1148",
              N, Entry, I * sizeof(uint32_t));
          }
        }
      }
    }
#endif
''' + anchor;

src = src.replace(anchor, injected, 1)
open(path, "w").write(src)
print("patched " + path)
