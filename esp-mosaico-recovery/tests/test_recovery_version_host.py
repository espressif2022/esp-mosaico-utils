from __future__ import annotations

import pathlib
import shutil
import subprocess


def test_recovery_version_transition(tmp_path: pathlib.Path) -> None:
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    assert compiler, "A host C compiler is required"
    source_dir = pathlib.Path(__file__).parents[1] / "firmware" / "recovery" / "main"
    source = tmp_path / "version.c"
    source.write_text(
        r'''
#include <assert.h>
#include "factory_recovery_version.h"

int main(void)
{
    const char *accepted[] = {
        "0.1", "0.1.0", "0.1.0-recovery", "0.1.1", "0.1.1-recovery",
        "0.1.2", "0.1.2-recovery"
    };
    for (size_t i = 0; i < sizeof(accepted) / sizeof(accepted[0]); ++i) {
        assert(factory_recovery_version_satisfies("0.1.2", accepted[i]));
    }
    const char *rejected[] = {
        "0.1.3", "0.2.0", "1.0.0", "2.5.0-recovery", "2.7.0-recovery",
        "2.8.5-recovery", "2.8.6-recovery", "3.0.0",
        "", "0", "0.", "0.1.", "0.1.x", "0.1.0.1", "-1.0.0",
        "4294967296.0.0", "0.4294967296.0", "0.1.4294967296"
    };
    for (size_t i = 0; i < sizeof(rejected) / sizeof(rejected[0]); ++i) {
        assert(!factory_recovery_version_satisfies("0.1.2", rejected[i]));
    }
    assert(!factory_recovery_version_satisfies("0.1.1", NULL));
    assert(!factory_recovery_version_satisfies(NULL, "0.1.1"));
    assert(!factory_recovery_version_satisfies("bad", "0.1.1"));
    assert(factory_recovery_version_satisfies("2.8.5-recovery", "2.7.0-recovery"));
    assert(!factory_recovery_version_satisfies("2.4.0-recovery", "2.5.0-recovery"));
    assert(!factory_recovery_version_satisfies("0.1.1", "2.5.0-recovery"));
    assert(factory_recovery_version_can_replace("0.1.1", "0.1.1"));
    assert(factory_recovery_version_can_replace("0.1.1", "0.1.2"));
    assert(!factory_recovery_version_can_replace("0.1.1", "0.1.0"));
    assert(!factory_recovery_version_can_replace("0.1.1", "2.8.5-recovery"));
    assert(!factory_recovery_version_can_replace("2.8.5-recovery", "0.1"));
    assert(!factory_recovery_version_can_replace("2.8.5-recovery", "2.7.0-recovery"));
    assert(factory_recovery_version_satisfies("0.1.2", "0.1.1"));
    assert(!factory_recovery_version_satisfies("0.1.1", "0.1.2"));
    assert(factory_recovery_version_can_replace("0.1.2", "0.1.2"));
    assert(factory_recovery_version_can_replace("0.1.2", "0.1.3"));
    assert(!factory_recovery_version_can_replace("0.1.2", "0.1.1"));
    return 0;
}
''',
        encoding="utf-8",
    )
    executable = tmp_path / "version-test.exe"
    subprocess.run(
        [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(source_dir),
         str(source), "-o", str(executable)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
