/**
 * wifi_manager.h
 *
 * Owns all Wi-Fi lifecycle concerns: initial connect, link-loss detection,
 * and automatic reconnect with backoff. Runs its own low-priority FreeRTOS
 * task so the camera and network tasks never have to poll Wi-Fi state
 * themselves — they just call WifiManager::isConnected().
 */

#ifndef WIFI_MANAGER_H
#define WIFI_MANAGER_H

#include <Arduino.h>

namespace WifiManager {

// Starts Wi-Fi, performs the initial blocking connect (bounded by
// WIFI_CONNECT_TIMEOUT_MS), and spawns the background monitor task that
// handles reconnects for the remainder of the program's life.
// Returns true if the initial connection succeeded.
bool begin();

// Non-blocking check used by other modules/tasks to decide whether it's
// safe to attempt network I/O.
bool isConnected();

// Returns the current IP address as a string, or "0.0.0.0" if not connected.
String localIP();

// Returns the number of reconnect events since boot (diagnostic counter).
uint32_t reconnectCount();

} // namespace WifiManager

#endif // WIFI_MANAGER_H
