cmake_minimum_required(VERSION 3.16)
get_filename_component(MOSAICO_UTILS_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../../.." ABSOLUTE)
set(MOSAICO_TEST_APPLICATION "" CACHE PATH "Generated application whose real UI is measured")
if(NOT EXISTS "${MOSAICO_TEST_APPLICATION}/main/CMakeLists.txt")
    message(FATAL_ERROR "Set MOSAICO_TEST_APPLICATION to a generated GSP application")
endif()
set(SDKCONFIG "${CMAKE_BINARY_DIR}/sdkconfig")
set(SDKCONFIG_DEFAULTS "${MOSAICO_TEST_APPLICATION}/sdkconfig.defaults;${MOSAICO_TEST_APPLICATION}/sdkconfig.application.defaults;${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults")
set(EXTRA_COMPONENT_DIRS
    "${MOSAICO_TEST_APPLICATION}/main"
    "${MOSAICO_UTILS_ROOT}/esp-mosaico-recovery/components/esp_mosaico_app_recovery"
    "${MOSAICO_UTILS_ROOT}/mosaico-tools/components/esp_mosaico_gsp_bundle"
    "${MOSAICO_UTILS_ROOT}/mosaico-tools/components/esp_mosaico_gsp_iris")
include("${MOSAICO_UTILS_ROOT}/mosaico-tools/cmake/gsp_compiler.cmake")
include("${MOSAICO_UTILS_ROOT}/esp-mosaico-recovery/cmake/mosaico_idf_project.cmake")
project(iris_internal_budget VERSION 1.0.0)
idf_component_get_property(app_main main COMPONENT_LIB)
target_sources(${app_main} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/budget.c")
foreach(symbol app_main iris_ota_support_start esp_iris_start esp_iris_boot_probe
        esp_iris_mark_healthy esp_iris_rpc_register esp_iris_screen_register esp_ota_set_boot_partition)
    target_link_options(${app_main} INTERFACE "-Wl,--wrap=${symbol}")
endforeach()
include("${MOSAICO_UTILS_ROOT}/mosaico-tools/cmake/system_update.cmake")
