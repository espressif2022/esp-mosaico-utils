# Application integration

The tools repository owns `templates/hello_world/mosaico-template.json` and the
public `mosaico.py project init` / `project sim` commands. Creation needs Python
3.8+ and this utilities checkout, with no IDF, BSP or device requirement.

Template schema v1 provides `workspace`, `template`, `utils`, `tools`, `bsp`,
`esp_iris`, and `engine` path anchors. Paths are rendered relative to each
output file. `workspace.init_template` remains configurable. `game create/new`
uses BSP `examples/<game>/mosaico-template.json`, with the same exclusive writer,
name validation, dry-run and rollback behavior. Configure `dependencies.raylib`
for the engine location; it is only required for games.

Normal apps include `esp-mosaico-recovery/cmake/mosaico_idf_project.cmake`
before project(), supplying MOSAICO_BSP_ROOT for the board-owned splash handoff.
Add `esp-mosaico-recovery/components/esp_mosaico_app_recovery` to
EXTRA_COMPONENT_DIRS and call iris_ota_support_start(). This component has no
GSP, BSP or display dependency. Its configure gate validates the effective
configuration, including existing sdkconfig files; the OTA writer stays in Recovery.

Optional GSP components are `mosaico-tools/components/esp_mosaico_gsp_bundle`
(ui_bundle_open) and `esp_mosaico_gsp_iris` (iris_screen_mirror_init/attach).
The latter owns the display-presenter link wrapper. Application display and touch
policy stays in the template's board_display.c. Both components retain ESP-GSP
1.4.0 and the MOSGSP resource format.

Use `cmake/gsp_compiler.cmake` before IDF to resolve the pinned GSPC. Include
`cmake/gsp_bundle.cmake` in the application component, then call
`mosaico_gsp_add_ui_bundle(${COMPONENT_LIB} "../ui/main.json")`.
Include `cmake/system_update.cmake` after project() to declare System Update
artifacts and reject unsafe direct IDF flash/app-flash targets. Other resources
use MOSAICO_SYSTEM_UPDATE_DATA_LABELS and per-label IMAGE/TARGET global properties.
No normal application bundle replaces the retained bootloader.

Recovery's `product_contract.json` owns host identity and fixed partition values.
The C ABI remains in `esp-mosaico-recovery/include/mosaico_recovery_contract.h`;
contract tests compare the product manifest, configuration and partition tables.

Validation: `python -m pytest mosaico-tools/tests esp-mosaico-recovery/tests`.
No consumer workspace source is needed. Old workspace paths have no forwarding
layer; consumers migrate to these public entry points and rebuild.
