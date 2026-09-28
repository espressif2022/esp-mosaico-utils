cmake_minimum_required(VERSION 3.16)
get_filename_component(MOSAICO_UTILS_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../../.." ABSOLUTE)

if(NOT SDKCONFIG)
    set(SDKCONFIG "${CMAKE_BINARY_DIR}/sdkconfig")
endif()

if(NOT SDKCONFIG_DEFAULTS)
    set(SDKCONFIG_DEFAULTS
        "sdkconfig.defaults;sdkconfig.application.defaults")
endif()

set(EXTRA_COMPONENT_DIRS
    "${CMAKE_CURRENT_LIST_DIR}/../../../components/esp_mosaico_app_recovery")

include("${MOSAICO_UTILS_ROOT}/esp-mosaico-recovery/cmake/mosaico_idf_project.cmake")
project(iris_crash VERSION 1.0.0)

include("${MOSAICO_UTILS_ROOT}/mosaico-tools/cmake/system_update.cmake")
