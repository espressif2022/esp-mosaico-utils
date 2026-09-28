# Product integration; the engine and board dependencies are explicitly supplied.
if(NOT EXISTS "${RAYLIB_LITE_ENGINE_ROOT}/cmake/mosaico_game_sdk.cmake")
    message(FATAL_ERROR "Set RAYLIB_LITE_ENGINE_ROOT to the initialized Raylib Lite Engine checkout")
endif()
set(MOSAICO_GAME_GSPC_FETCHER "${CMAKE_CURRENT_LIST_DIR}/../tools/gsp-sim/fetch_gspc.py")
set(MOSAICO_GAME_RECOVERY_COMPONENT_DIR "${CMAKE_CURRENT_LIST_DIR}/../../esp-mosaico-recovery/components/esp_mosaico_app_recovery")
include("${RAYLIB_LITE_ENGINE_ROOT}/cmake/mosaico_game_sdk.cmake")
