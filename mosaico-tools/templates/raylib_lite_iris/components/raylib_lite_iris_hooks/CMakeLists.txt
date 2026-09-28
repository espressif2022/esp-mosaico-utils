# Strong definitions replace the engine's weak native lifecycle hooks.
idf_component_register(
    SRCS "raylib_lite_iris_hooks.c"
    PRIV_REQUIRES mosaico_game_app esp_iris esp_mosaico_app_recovery nvs_flash
    WHOLE_ARCHIVE)
