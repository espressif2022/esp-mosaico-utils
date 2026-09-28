idf_component_register(SRCS "iris_screen_mirror.c" INCLUDE_DIRS "include"
    REQUIRES esp-gsp esp_iris
    PRIV_REQUIRES esp-mosaico-bsp esp_display_present esp_lcd freertos)
target_link_options(${COMPONENT_LIB} INTERFACE
    "-Wl,--wrap=esp_display_presenter_submit_buffer")
