// Copy to secrets.h (git-ignored) and fill in.
#pragma once
#define WIFI_SSID     "your-ssid"
#define WIFI_PASSWORD "your-password"
#define HOSTNAME      "jbod-fans"      // reachable as jbod-fans.local
// Shared with fan-bridge (env JBOD_TOKEN). Empty = no auth (LAN only!).
#define API_TOKEN     "change-me"
// Password for over-the-air updates (ArduinoOTA). Empty = OTA off.
#define OTA_PASSWORD  "change-me-too"
