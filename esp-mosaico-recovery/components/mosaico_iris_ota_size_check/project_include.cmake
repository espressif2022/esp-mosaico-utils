# IDF's default app_check_size compares an OTA application with the smallest
# app partition. On retained-Recovery layouts that is the Recovery factory
# slot, not the OTA slot that receives this application. Validate ota_0.
function(partition_table_add_check_size_target target_name)
    set(args BINARY_PATH PARTITION_TYPE PARTITION_SUBTYPE)
    cmake_parse_arguments(CMD "" "${args}" "DEPENDS" ${ARGN})
    if(target_name STREQUAL "app_check_size" AND CMD_PARTITION_TYPE STREQUAL "app")
        set(CMD_PARTITION_SUBTYPE ota_0)
    endif()
    idf_build_get_property(python PYTHON)
    idf_build_get_property(table_bin PARTITION_TABLE_BIN_PATH)
    if(CMD_PARTITION_SUBTYPE)
        set(subtype_arg --subtype ${CMD_PARTITION_SUBTYPE})
    endif()
    add_custom_target(${target_name}
        COMMAND ${python} ${PARTITION_TABLE_CHECK_SIZES_TOOL_PATH}
            --offset ${PARTITION_TABLE_OFFSET}
            partition --type ${CMD_PARTITION_TYPE} ${subtype_arg}
            ${table_bin} ${CMD_BINARY_PATH}
        DEPENDS ${CMD_DEPENDS} partition_table_bin)
endfunction()
