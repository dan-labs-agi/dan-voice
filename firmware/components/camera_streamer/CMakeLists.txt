idf_component_register(SRCS "camera.cpp" "wifi_manager.cpp" "protocol.cpp" "streamer.cpp" "camera_streamer.cpp"
                       INCLUDE_DIRS "."
                       REQUIRES esp32-camera esp_wifi esp_netif esp_event lwip driver esp_timer)
