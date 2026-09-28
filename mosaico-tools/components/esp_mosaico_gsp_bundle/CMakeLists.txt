idf_component_register(SRCS "ui_bundle.c" INCLUDE_DIRS "include"
    REQUIRES esp-gsp esp_partition
    PRIV_REQUIRES mbedtls)
