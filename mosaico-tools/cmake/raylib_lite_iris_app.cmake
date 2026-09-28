# Build one Raylib Lite Engine native game as a managed ESP-Iris application.
# Include from a project CMakeLists.txt before project():
#
#   cmake_minimum_required(VERSION 3.16)
#   set(RAYLIB_LITE_GAME sky_hop)
#   include(/path/to/mosaico-tools/cmake/raylib_lite_iris_app.cmake)
#   project(${RAYLIB_LITE_GAME} VERSION 1.0.0)
#   include("${MOSAICO_SYSTEM_UPDATE_CMAKE}")
#
# The project directory must contain templates/raylib_lite_iris/partitions.csv.
# `mosaico.py game build --target iris <game>` generates such a project.
# RAYLIB_LITE_ENGINE_ROOT and MOSAICO_BSP_ROOT are CMake or environment inputs.
include_guard(GLOBAL)

get_filename_component(_raylib_iris_tools "${CMAKE_CURRENT_LIST_DIR}/.." ABSOLUTE)
get_filename_component(_raylib_iris_utils "${_raylib_iris_tools}/.." ABSOLUTE)
set(_raylib_iris_template "${_raylib_iris_tools}/templates/raylib_lite_iris")

if(NOT RAYLIB_LITE_ENGINE_ROOT AND DEFINED ENV{RAYLIB_LITE_ENGINE_ROOT})
    set(RAYLIB_LITE_ENGINE_ROOT "$ENV{RAYLIB_LITE_ENGINE_ROOT}")
endif()
if(NOT EXISTS "${RAYLIB_LITE_ENGINE_ROOT}/components/mosaico_game_app/include/raylib_lite_native_hooks.h")
    message(FATAL_ERROR
        "RAYLIB_LITE_ENGINE_ROOT must name a Raylib Lite Engine checkout with "
        "native lifecycle hooks: '${RAYLIB_LITE_ENGINE_ROOT}'")
endif()
get_filename_component(RAYLIB_LITE_ENGINE_ROOT "${RAYLIB_LITE_ENGINE_ROOT}" ABSOLUTE)

if(NOT RAYLIB_LITE_GAME)
    message(FATAL_ERROR "Set RAYLIB_LITE_GAME to an engine game; list them with "
        "'python3 ${RAYLIB_LITE_ENGINE_ROOT}/tools/game_cli.py list --target native'")
endif()
set(_raylib_iris_game "${RAYLIB_LITE_ENGINE_ROOT}/examples/${RAYLIB_LITE_GAME}")
if(NOT EXISTS "${_raylib_iris_game}/main/CMakeLists.txt")
    message(FATAL_ERROR "${RAYLIB_LITE_GAME} is not a native engine game: ${_raylib_iris_game}")
endif()
if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/partitions.csv")
    message(FATAL_ERROR "Copy ${_raylib_iris_template}/partitions.csv into ${CMAKE_CURRENT_SOURCE_DIR}")
endif()

if(NOT SDKCONFIG)
    set(SDKCONFIG "${CMAKE_BINARY_DIR}/sdkconfig")
endif()
set(SDKCONFIG_DEFAULTS
    "${_raylib_iris_template}/sdkconfig.defaults"
    "${_raylib_iris_template}/sdkconfig.application.defaults")

include("${RAYLIB_LITE_ENGINE_ROOT}/cmake/raylib_lite_native_project.cmake")

set(MOSAICO_ESP_IRIS_ROOT "${_raylib_iris_utils}/ESP-Iris")
list(APPEND EXTRA_COMPONENT_DIRS
    "${_raylib_iris_game}/main"
    "${MOSAICO_ESP_IRIS_ROOT}/components/esp_iris"
    "${_raylib_iris_utils}/esp-mosaico-recovery/components/esp_mosaico_app_recovery"
    "${_raylib_iris_utils}/esp-mosaico-recovery/components/mosaico_iris_ota_size_check"
    "${_raylib_iris_template}/components/raylib_lite_iris_hooks")
set(MOSAICO_SYSTEM_UPDATE_CMAKE "${_raylib_iris_tools}/cmake/system_update.cmake")
include("${_raylib_iris_utils}/esp-mosaico-recovery/cmake/mosaico_idf_project.cmake")
