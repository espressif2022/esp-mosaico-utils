idf_component_register(
    SRCS
        "main.c"
        "factory_bridge.c"
        "factory_recovery_control.c"
        "factory_nand_update.c"
        "factory_network.c"
        "factory_system_inventory.c"
        "factory_system_metadata.c"
        "factory_system_update.c"
        "factory_ui.c"
        "vibe_ui.c"
        "vibe_transfer.c"
        "vibe_bundle.c"
        "board_display.c"
        "factory_ui_input.c"
        "recovery_ota_support.c"
        "iris_screen_mirror.c"
    INCLUDE_DIRS "." "../../../include"
    REQUIRES
        app_update
        bootloader_support
        esp_app_format
        esp_event
        esp_http_client
        esp_iris
        iris_bridge
        esp-mosaico-bsp
        esp_netif
        esp_partition
        esp_wifi
        freertos
        espressif__cjson
        lwip
        mdns
        mbedtls
        esp-gsp
        esp_display_present
        esp_lcd
        esp_timer
        nvs_flash
        spi_flash
)

# Recovery is self-contained: scenes and subset fonts live in factory.bin.
gsp_add_bundle(${COMPONENT_LIB}
    SCENES "../ui/main.json"
    PROFILE "../ui/profile.yaml"
    PIXEL_FORMAT rgb565
    SYMBOL bundle)
target_link_options(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=esp_display_presenter_submit_buffer")

# Store a compressed copy instead of the helper's uncompressed embed source.
set(vibe_bundle_dir "${CMAKE_CURRENT_BINARY_DIR}/gsp_gen_bundle")
set_source_files_properties("${vibe_bundle_dir}/bundle_embed.S"
    PROPERTIES HEADER_FILE_ONLY TRUE)
idf_build_get_property(vibe_python PYTHON)
set(vibe_packed "${CMAKE_CURRENT_BINARY_DIR}/vibe_bundle_data.c")
add_custom_command(OUTPUT "${vibe_packed}"
    COMMAND "${vibe_python}" "${CMAKE_CURRENT_LIST_DIR}/../tools/pack_vibe_bundle.py"
        "${vibe_bundle_dir}/bundle.gspb" "${vibe_packed}"
    DEPENDS "${vibe_bundle_dir}/bundle.gspb"
        "${CMAKE_CURRENT_LIST_DIR}/../tools/pack_vibe_bundle.py"
    VERBATIM)
target_sources(${COMPONENT_LIB} PRIVATE "${vibe_packed}")
