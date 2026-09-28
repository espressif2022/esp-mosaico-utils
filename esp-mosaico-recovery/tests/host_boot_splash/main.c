// SPDX-License-Identifier: Apache-2.0
#include <stdlib.h>
#include "sdk.h"
#include "splash_under_test.c"
int main(int argc, char **argv) {
    assert(argc == 3);
    regs[1] = strtoul(argv[1], NULL, 0);
    fail_at = strtoul(argv[2], NULL, 0);
    regs[0] = 0x4D4C4344; /* stale handoff must not survive a failed splash */
    const uint16_t version = regs[1];
    const bool supported = version >= 0x100 && version <= 0x102;
    const bool shown = mosaico_boot_splash_show();
    assert(shown == (supported && !fail_at));
    assert(regs[0] == (shown ? 0x4D4C4344U : 0));
    assert(motor_level == 0);
    if (supported) {
        assert(clock_pin == (version == 0x100 ? 44 : 42));
        assert(reset_pin == (version == 0x100 ? 42 : 44));
        assert(motor_off_us - motor_on_us == 60000);
        if (fail_at) assert(transfers == fail_at && elapsed_us < 1000000);
    } else {
        assert(transfers == 0 && elapsed_us == 0 && clock_pin == 0);
    }
    return 0;
}
