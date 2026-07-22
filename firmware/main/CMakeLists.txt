idf_component_register(SRCS "main.c" "gatt_svr.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES bt nvs_flash camera_streamer)
