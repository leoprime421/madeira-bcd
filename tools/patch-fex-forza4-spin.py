#!/usr/bin/env python3
"""Repair FH4's observed startup writer/poll spin in the iOS ARM64EC FEX JIT.

Build 44 identified a stable generated ARM64 sequence at
ForzaHorizon4.exe+0x3049780. The original guest path does this:

    cmp w20,#0
    b.ne wait
    local_flag = 1
    global_flag = 1
wait:
    while (!local_flag) {}

On Madeira the w20!=0 path can wait forever because no peer publishes the
local byte. The first workaround NOPed B.NE, which also executed the global
store on the w20!=0 path. Build 45 escaped the spin but later jumped into the
RW .detourd section, so that extra global side effect is too risky.

For FH4 only, rewrite the same five instructions in-place so both paths publish
the local wake byte, while the global store remains restricted to the original
w20==0 path. The polling loop stays intact and no code size changes are made.
"""
import sys

path = sys.argv[1]
src = open(path).read()
marker = "madeira-bcd: FH4 guarded-local spin repair"
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
    /* madeira-bcd: FH4 guarded-local spin repair.
     *
     * Exact sequence observed in build 44:
     *
     *   I+0   cmp   w20,#0
     *   I+1   b.ne  wait
     *   I+2   mov   w6,#1
     *   I+3   add   x7,x29,#0x2b0
     *   I+4   stlrb w6,[x7]          // local wake byte
     *   I+5..7 load global address
     *   I+8   stlrb w6,[x7]          // global side effect
     *   I+9   add   x6,x29,#0x2b0    // wait:
     *   I+10  ldarb/ldaprb w8,[x6]
     *   I+11  tst   w8,#0xff
     *   I+12  b.eq  wait
     *
     * Build 45 proved that deleting I+1 breaks the deadlock, but doing so also
     * made w20!=0 execute the global store at I+8. Shortly afterwards FH4
     * reached a bogus RIP in the RW .detourd section. Do not widen executable
     * permissions to hide that symptom; instead keep the original global-store
     * predicate and only make the local wake publication unconditional.
     *
     * Rewrite I+1..I+4 as:
     *
     *   mov   w6,#1
     *   add   x7,x29,#0x2b0
     *   stlrb w6,[x7]
     *   b.ne  wait                  // +0x14 from I+4 to I+9
     *
     * MOV/ADD/STLRB do not modify NZCV, so B.NE still consumes the flags from
     * the original CMP. w20==0 therefore falls through to the original global
     * writer; w20!=0 skips it and goes straight to the unchanged poll. */
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
            Words[I + 4] == 0x089ffce6u && /* stlrb w6,[x7] */
            Words[I + 8] == 0x089ffce6u && /* stlrb w6,[x7], global */
            Words[I + 9] == 0x910ac3a6u && /* add x6,x29,#0x2b0 */
            AcquireByte &&
            Words[I + 11] == 0x72001d1fu && /* tst w8,#0xff */
            Words[I + 12] == 0x54ffffa0u && /* b.eq -0xc */
            Words[I + 13] == 0x71000289u) { /* subs w9,w20,#0 */
          /* Move the local writer in front of the conditional branch. */
          Words[I + 1] = 0x52800026u; /* mov   w6,#1 */
          Words[I + 2] = 0x910ac3a7u; /* add   x7,x29,#0x2b0 */
          Words[I + 3] = 0x089ffce6u; /* stlrb w6,[x7] */
          Words[I + 4] = 0x540000a1u; /* b.ne  +0x14 -> I+9 (wait) */

          static volatile uint32_t FH4SpinPatchCount = 0;
          const uint32_t N = __sync_add_and_fetch(&FH4SpinPatchCount, 1);
          if (N <= 16) {
            LogMan::Msg::EFmt(
              "[fh4-spin] guarded-local repair #{} entry={:x} temp+0x{:x}: "
              "local wake unconditional; global writer remains w20==0 rev=ml1152",
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
