#!/usr/bin/env python3
"""Repair FH4's observed startup writer/poll spin in the iOS ARM64EC FEX JIT.

Build 44 identified a stable generated ARM64 sequence at
ForzaHorizon4.exe+0x3049780. The original generated path is:

    cmp w20,#0
    b.ne wait
    local_flag = 1
    global_flag = 1
wait:
    while (!local_flag) {}
    subs w9,w20,#0

On Madeira the w20!=0 path can wait forever because it skips both stores and
then polls x29+0x2b0. An earlier workaround moved a synthetic local store in
front of the branch. The 2026-10-05 FH4 log proved that workaround unsafe: the
synthetic STRB itself repeatedly faults with JIT SIGBUS at the same translated
PC because x29+0x2b0 is not writable on that path.

For FH4 only, keep the w20==0 path byte-for-byte unchanged. For w20!=0, retarget
the existing B.NE from the wait loop to the first instruction after the loop
(the existing `subs w9,w20,#0`). This treats the unsatisfied local wait as an
already-completed synchronization on the path that never publishes the local
byte, without adding stores, changing the global publication, or widening page
permissions.
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: FH4 branch-over-wait repair"
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
    /* madeira-bcd: FH4 branch-over-wait repair.
     *
     * Exact sequence observed in FH4:
     *
     *   I+0   cmp   w20,#0
     *   I+1   b.ne  I+9               // broken path enters wait
     *   I+2   mov   w6,#1
     *   I+3   add   x7,x29,#0x2b0
     *   I+4   stlrb w6,[x7]           // local wake byte
     *   I+5..7 load global address
     *   I+8   stlrb w6,[x7]           // global publication
     *   I+9   add   x6,x29,#0x2b0     // wait:
     *   I+10  ldarb/ldaprb w8,[x6]
     *   I+11  tst   w8,#0xff
     *   I+12  b.eq  I+9
     *   I+13  subs  w9,w20,#0          // continuation
     *
     * The prior workaround moved a STRB to the w20!=0 path. FH4's ml1154 log
     * shows that exact synthetic STRB looping in SIGBUS. Do not manufacture a
     * local write on this path. Instead retarget B.NE directly to I+13, which
     * skips the stores and the wait together. w20==0 still executes the exact
     * original local/global release stores and poll.
     *
     * B.cond immediate is relative to the branch instruction. I+1 -> I+13 is
     * +12 instructions = +0x30 bytes, so B.NE encodes as 0x54000181. */
    if (const char* FH4SpinFix = std::getenv("MADEIRA_FEX_FH4_SPIN_FIX");
        FH4SpinFix && FH4SpinFix[0] == '1') {
      auto* Words = reinterpret_cast<uint32_t*>(TempCodeBuffer);
      const size_t WordCount = TempSize / sizeof(uint32_t);

      for (size_t I = 0; I + 14 < WordCount; ++I) {
        const bool AcquireByte =
          Words[I + 10] == 0x08dffcc8u || /* ldarb  w8,[x6] */
          Words[I + 10] == 0x38bfc0c8u;   /* ldaprb w8,[x6] */

        if (Words[I + 0] == 0x7100029fu && /* cmp w20,#0 */
            Words[I + 1] == 0x54000101u && /* b.ne +0x20 -> I+9 */
            Words[I + 2] == 0x52800026u && /* mov w6,#1 */
            Words[I + 3] == 0x910ac3a7u && /* add x7,x29,#0x2b0 */
            Words[I + 4] == 0x089ffce6u && /* stlrb w6,[x7], local */
            Words[I + 8] == 0x089ffce6u && /* stlrb w6,[x7], global */
            Words[I + 9] == 0x910ac3a6u && /* add x6,x29,#0x2b0 */
            AcquireByte &&
            Words[I + 11] == 0x72001d1fu && /* tst w8,#0xff */
            Words[I + 12] == 0x54ffffa0u && /* b.eq -0xc -> I+9 */
            Words[I + 13] == 0x71000289u) { /* subs w9,w20,#0 */
          Words[I + 1] = 0x54000181u; /* b.ne +0x30 -> I+13 */

          static volatile uint32_t FH4SpinPatchCount = 0;
          const uint32_t N = __sync_add_and_fetch(&FH4SpinPatchCount, 1);
          if (N <= 16) {
            LogMan::Msg::EFmt(
              "[fh4-spin] branch-over-wait repair #{} entry={:x} temp+0x{:x}: "
              "w20!=0 skips local/global stores and broken poll rev=ml1155",
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
