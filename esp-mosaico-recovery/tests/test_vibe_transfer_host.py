"""Exercise the portable sampler with irregular clocks and transfer boundaries."""
from pathlib import Path
import shutil
import subprocess


def test_payload_rate_and_progress_boundaries(tmp_path):
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    assert compiler, "A host C compiler is required"
    main = Path(__file__).resolve().parents[1] / "firmware/recovery/main"
    source = tmp_path / "transfer.c"
    source.write_text(r'''
#include <assert.h>
#include "vibe_transfer.h"
int main(void)
{
    vibe_rate_t r = {0};
    vibe_transfer_t t = {.now_ms = 9000, .received = 4096,
        .source = 1, .component_id = 1, .receiving = true};
    vibe_rate_sample(&r, &t);
    assert(!r.valid); /* Existing bytes are not attributed to this window. */
    t.now_ms += 250; t.received += 1024;
    vibe_rate_sample(&r, &t); assert(!r.valid);
    t.now_ms += 1000; t.received += 4096;
    vibe_rate_sample(&r, &t); assert(r.valid && r.bytes_per_second == 4096);
    vibe_rate_sample(&r, &t); assert(r.bytes_per_second == 4096); /* same clock */
    t.now_ms += 1000;
    vibe_rate_sample(&r, &t); assert(r.valid && r.bytes_per_second == 0);
    t.now_ms += 45000;
    vibe_rate_sample(&r, &t); assert(r.bytes_per_second == 0);
    t.now_ms += 1250; t.received += 2560;
    vibe_rate_sample(&r, &t); assert(r.bytes_per_second == 2048);
    ++t.component_id;
    vibe_rate_sample(&r, &t); assert(!r.valid);
    t.now_ms += 1000; t.received += 1024;
    vibe_rate_sample(&r, &t); assert(r.valid && r.bytes_per_second == 1024);
    --t.received;
    vibe_rate_sample(&r, &t); assert(!r.valid); /* retry */
    t.now_ms += 1000; t.received += 1024;
    vibe_rate_sample(&r, &t); assert(r.valid);
    ++t.job[15];
    vibe_rate_sample(&r, &t); assert(!r.valid); /* full operation identity */
    t.now_ms += 1000; t.received += 1024;
    vibe_rate_sample(&r, &t); assert(r.valid);
    ++t.source;
    vibe_rate_sample(&r, &t); assert(!r.valid);
    t.now_ms += 1000; t.received += 1024;
    vibe_rate_sample(&r, &t); assert(r.valid);
    --t.now_ms;
    vibe_rate_sample(&r, &t); assert(!r.valid); /* clock reset */
    t.now_ms += 1000; t.received += 1024;
    vibe_rate_sample(&r, &t); assert(r.valid);
    t.receiving = false;
    vibe_rate_sample(&r, &t); assert(!r.valid && !r.started);
    assert(vibe_transfer_permille(400, 0) == 0);
    assert(vibe_transfer_permille(0, 4096) == 0);
    assert(vibe_transfer_permille(1024, 4096) == 250);
    assert(vibe_transfer_permille(4096, 4096) == 1000);
    assert(vibe_transfer_permille(4097, 4096) == 1000);
    /* Whole-package counters must not truncate to 32 bits. */
    assert(vibe_transfer_permille(UINT64_C(4294967296), UINT64_C(8589934592)) == 500);
    return 0;
}
''', encoding="utf-8")
    executable = tmp_path / "transfer-test.exe"
    subprocess.run([compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(main),
                    str(source), str(main / "vibe_transfer.c"), "-o", str(executable)],
                   check=True, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
