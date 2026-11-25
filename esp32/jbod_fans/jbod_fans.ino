// jbod-fans: ESP32 driver for 3 x 140 mm 4-pin PWM fans cooling a JBOD's drive bays.
//
// fan-bridge (the server) reads the drive temperatures, works out a fan speed
// and sends it here every few seconds:
//
//   POST /set?pct=45&ttl=30&fs=80&t=39      header X-Token: <API_TOKEN>
//   GET  /status                            JSON (rpm, mode, probe, ...)
//   POST /selftest                          header X-Token: ~36 s wiring check, results in /status
//   GET  /setup, POST /setup                Wi-Fi / token / name (open on the setup AP, token on the LAN)
//   GET  /update, POST /update              upload a new firmware (-ota.bin) from the browser
//   GET  /                                  small status page
//
// These fans are the drives' ONLY cooling, so every failure mode fails loud:
//   - boot, or no command for `ttl` seconds -> failsafe speed (default 80 %)
//     or, if the exhaust probe is fitted, a local curve on exhaust air
//   - exhaust probe reads hotter than the server asked for -> probe wins
//   - a fan stalls -> the others go to 100 %
//   - Wi-Fi down -> fans keep running on the rules above
//
// Hardware (see esp32/README.md): ESP32 DevKit, 12 V supply, buck to 5 V,
// one 10k pull-up to 3.3 V per tach line, optional DS18B20 exhaust probe.

#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <DNSServer.h>
#include <Update.h>

// Wi-Fi, token and name are entered on the setup page (stored in flash), so
// one prebuilt binary fits everyone. A secrets.h, if present, only provides
// first-boot defaults.
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef HOSTNAME
#define HOSTNAME "jbod-fans"
#endif
#ifndef API_TOKEN
#define API_TOKEN ""
#endif
#ifndef OTA_PASSWORD
#define OTA_PASSWORD ""
#endif
#define SETUP_AP_SSID "jbod-fans-setup"
#define SETUP_AP_PASS "jbodfans"           // WPA2 needs 8+ characters
static const int BOOT_BTN = 0;               // hold 5 s: forget Wi-Fi, back to setup
static const uint32_t SETUP_AP_AFTER_MS = 120000;   // can't join Wi-Fi for 2 min -> also open the setup AP

#ifndef USE_EXHAUST_PROBE
#define USE_EXHAUST_PROBE 1          // 0 if you don't fit the DS18B20 (and skip the two libraries)
#endif
#if USE_EXHAUST_PROBE
#include <OneWire.h>
#include <DallasTemperature.h>
#endif

#define FW_VERSION "1.2.0"

// ---- pins ---------------------------------------------------------------
static const int FAN_COUNT = 3;
static const int PWM_PINS[FAN_COUNT]  = {25, 26, 27};
static const int TACH_PINS[FAN_COUNT] = {32, 33, 35};   // 35 is input-only: external pull-up required
static const int PROBE_PIN = 4;                         // DS18B20 data (4.7k pull-up to 3.3 V)
static const int LED_PIN = 2;                           // on-board LED on most DevKits

// ---- fan electrical -------------------------------------------------------
static const uint32_t PWM_FREQ = 25000;   // Intel 4-pin spec
static const uint8_t  PWM_BITS = 10;
// 1 if PWM goes through an inverting transistor (NPN/N-MOSFET open-drain
// stage). 0 = GPIO wired straight to the fan's PWM pin (fine for Noctua,
// Arctic and most modern fans; they accept 3.3 V logic).
#define PWM_INVERTED 0
static const int PULSES_PER_REV = 2;

// ---- control --------------------------------------------------------------
static const int MIN_PCT = 20;             // never below this: these are the only drive fans
static const int DEFAULT_FAILSAFE_PCT = 80;
static const uint32_t DEFAULT_TTL_S = 30;
static const int STALL_MIN_PCT = 25;       // only judge stalls when asked for at least this
static const int STALL_RPM = 150;
static const uint32_t STALL_AFTER_MS = 5000;
static const int PROBE_ONLY_MIN_PCT = 40;  // server gone, probe fine: don't trust it lower than this
// Exhaust-air curve (°C at the JBOD's rear, %). Used as a floor on top of
// the server's value, and on its own when the server is gone.
static const float PROBE_CURVE[][2] = {{30, 30}, {34, 45}, {38, 65}, {42, 85}, {45, 100}};
static const int PROBE_POINTS = sizeof(PROBE_CURVE) / sizeof(PROBE_CURVE[0]);

// ---- state ----------------------------------------------------------------
struct Tach { volatile uint32_t pulses; volatile uint32_t lastUs; };
static Tach tach[FAN_COUNT];
static int rpm[FAN_COUNT];
static bool stalled[FAN_COUNT];
static uint32_t lowSinceMs[FAN_COUNT];

static int cmdPct = -1;                    // last value from the server (-1 = none yet)
static uint32_t cmdAtMs = 0, ttlMs = DEFAULT_TTL_S * 1000;
static float serverTempC = NAN;
static int failsafePct = DEFAULT_FAILSAFE_PCT;
static int appliedPct = 100;
static const char *mode = "boot";
static float probeC = NAN;
static uint32_t cmdCount = 0, authFails = 0;

struct Config { String ssid, pass, token, host, ota; };
static Config cfg;
static bool apOn = false;
DNSServer dns;

// ---- self-test: proves tach, PWM and fan<->pin mapping in ~36 s -----------
// Steps 0-2: all fans at 30/60/100 %. Steps 3-5: fan i at 100 %, the others
// at 30 %, which shows whether PWM pin i really drives the fan on tach pin i.
static const int ST_LEVELS[3] = {30, 60, 100};
static const int ST_STEPS = 3 + FAN_COUNT;
static const uint32_t ST_STEP_MS = 6000;
static int stStep = -1;                    // -1 idle
static uint32_t stStepAt = 0;
static bool stDone = false;
static int stRpm[3][FAN_COUNT];            // per level
static int stOwn[FAN_COUNT];               // fan i alone at 100 %

static int stPct(int step, int fan) {
  if (step < 3) return ST_LEVELS[step];
  return (step - 3 == fan) ? 100 : 30;
}

static const char *stVerdict(int i) {
  int r30 = stRpm[0][i], r100 = stRpm[2][i];
  if (r30 == 0 && r100 == 0) return "no tach signal: fan not spinning, or tach wire / 10k pull-up to 3.3 V missing";
  if (r30 > r100 * 1.2) return "speed falls as PWM rises: PWM is inverted, set PWM_INVERTED 1";
  if (r100 < r30 * 1.2) return "speed ignores PWM: check the PWM wire (or drive it through a transistor, PWM_INVERTED 1)";
  if (stOwn[i] < r100 * 0.8) return "this fan's PWM wire goes to a different fan: swap PWM wires";
  if (r30 < STALL_RPM * 2) return "ok, but very slow at 30 %: check it never stalls at the minimum";
  return "ok";
}

WebServer http(80);
Preferences prefs;
#if USE_EXHAUST_PROBE
OneWire oneWire(PROBE_PIN);
DallasTemperature probe(&oneWire);
static bool probePresent = false;
#endif

// ---- PWM (Arduino-ESP32 core 2.x and 3.x) ---------------------------------
static void pwmAttach(int i) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PWM_PINS[i], PWM_FREQ, PWM_BITS);
#else
  ledcSetup(i, PWM_FREQ, PWM_BITS);
  ledcAttachPin(PWM_PINS[i], i);
#endif
}

static void pwmWrite(int i, int pct) {
  uint32_t max = (1u << PWM_BITS) - 1;
  uint32_t duty = (uint32_t)constrain(pct, 0, 100) * max / 100;
  if (PWM_INVERTED) duty = max - duty;
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(PWM_PINS[i], duty);
#else
  ledcWrite(i, duty);
#endif
}

static void IRAM_ATTR onTach(void *arg) {
  Tach *t = (Tach *)arg;
  uint32_t now = micros();
  if (now - t->lastUs > 1500) {        // ignore edge noise (a 3000 rpm fan pulses every 10 ms)
    t->pulses = t->pulses + 1;
    t->lastUs = now;
  }
}

// ---- helpers ----------------------------------------------------------------
static int curvePct(float c) {
  if (c <= PROBE_CURVE[0][0]) return (int)PROBE_CURVE[0][1];
  for (int i = 0; i < PROBE_POINTS - 1; i++) {
    if (c < PROBE_CURVE[i + 1][0]) {
      float f = (c - PROBE_CURVE[i][0]) / (PROBE_CURVE[i + 1][0] - PROBE_CURVE[i][0]);
      return (int)ceilf(PROBE_CURVE[i][1] + f * (PROBE_CURVE[i + 1][1] - PROBE_CURVE[i][1]));
    }
  }
  return (int)PROBE_CURVE[PROBE_POINTS - 1][1];
}

static bool authorized() {
  if (cfg.token.length() == 0) return true;
  String tok = http.hasHeader("X-Token") ? http.header("X-Token") : http.arg("token");
  return tok == cfg.token;
}

static bool viaSetupAp() { return apOn && http.client().localIP() == WiFi.softAPIP(); }

static String statusJson() {
  uint32_t now = millis();
  String s = "{";
  s += "\"fw\":\"" FW_VERSION "\",\"mode\":\"" + String(mode) + "\"";
  s += ",\"pct\":" + String(appliedPct);
  s += ",\"cmd_pct\":" + String(cmdPct);
  s += ",\"cmd_age_s\":" + (cmdPct < 0 ? String("null") : String((now - cmdAtMs) / 1000.0f, 1));
  s += ",\"ttl_s\":" + String(ttlMs / 1000);
  s += ",\"failsafe_pct\":" + String(failsafePct);
  s += ",\"rpm\":[";
  for (int i = 0; i < FAN_COUNT; i++) s += (i ? "," : "") + String(rpm[i]);
  s += "],\"stalled\":[";
  for (int i = 0; i < FAN_COUNT; i++) s += (i ? "," : "") + String(stalled[i] ? "true" : "false");
  s += "],\"probe_c\":" + (isnan(probeC) ? String("null") : String(probeC, 1));
  s += ",\"server_temp_c\":" + (isnan(serverTempC) ? String("null") : String(serverTempC, 1));
  s += ",\"uptime_s\":" + String(now / 1000);
  s += ",\"rssi\":" + String(WiFi.RSSI());
  s += ",\"setup_ap\":" + String(apOn ? "true" : "false");
  s += ",\"commands\":" + String(cmdCount) + ",\"auth_failures\":" + String(authFails);
  s += ",\"selftest\":{\"state\":\"" + String(stStep >= 0 ? "running" : stDone ? "done" : "idle") + "\"";
  if (stStep >= 0) s += ",\"step\":" + String(stStep + 1) + ",\"of\":" + String(ST_STEPS);
  if (stDone) {
    s += ",\"fans\":[";
    for (int i = 0; i < FAN_COUNT; i++) {
      s += String(i ? "," : "") + "{\"rpm30\":" + String(stRpm[0][i]) + ",\"rpm60\":" + String(stRpm[1][i]) +
           ",\"rpm100\":" + String(stRpm[2][i]) + ",\"alone\":" + String(stOwn[i]) +
           ",\"verdict\":\"" + String(stVerdict(i)) + "\"}";
    }
    s += "]";
  }
  s += "}";
  s += "}";
  return s;
}

// ---- HTTP -------------------------------------------------------------------
static void handleSet() {
  if (!authorized()) { authFails++; http.send(401, "application/json", "{\"error\":\"bad token\"}"); return; }
  if (!http.hasArg("pct")) { http.send(400, "application/json", "{\"error\":\"pct required\"}"); return; }
  cmdPct = constrain(http.arg("pct").toInt(), 0, 100);
  if (http.hasArg("ttl")) ttlMs = (uint32_t)constrain(http.arg("ttl").toInt(), 5, 600) * 1000;
  if (http.hasArg("t")) serverTempC = http.arg("t").toFloat();
  if (http.hasArg("fs")) {
    int fs = constrain(http.arg("fs").toInt(), 30, 100);
    if (fs != failsafePct) { failsafePct = fs; prefs.putInt("fs", fs); }
  }
  cmdAtMs = millis();
  cmdCount++;
  http.send(200, "application/json", statusJson());
}

static void handleSelftest() {
  if (!authorized()) { authFails++; http.send(401, "application/json", "{\"error\":\"bad token\"}"); return; }
  if (stStep < 0) { stStep = 0; stStepAt = millis(); stDone = false; }
  http.send(200, "application/json", statusJson());
}

// ---- Wi-Fi setup page ---------------------------------------------------------
static String esc(const String &v) {
  String o; for (char c : v) { if (c == '"') o += "&quot;"; else if (c == '<') o += "&lt;"; else if (c == '&') o += "&amp;"; else o += c; }
  return o;
}

static void handleSetupGet() {
  // Open on the setup AP; on your LAN it needs the token (?token=...).
  if (!viaSetupAp() && !authorized()) { http.send(401, "text/plain", "add ?token=YOUR_TOKEN"); return; }
  int n = WiFi.scanComplete();
  String opts;
  if (n >= 0) {
    for (int i = 0; i < n; i++) opts += "<option value=\"" + esc(WiFi.SSID(i)) + "\">" + String(WiFi.RSSI(i)) + " dBm</option>";
    WiFi.scanDelete();
  }
  if (n != WIFI_SCAN_RUNNING) WiFi.scanNetworks(true);   // refresh in the background for next time
  String h = F("<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
               "<title>JBOD fans setup</title><style>body{font:16px system-ui,sans-serif;margin:16px;max-width:460px;background:#111;color:#eee}"
               "label{display:block;margin:12px 0 4px;color:#bbb}input{width:100%;box-sizing:border-box;padding:10px;font:inherit;border-radius:8px;border:1px solid #444;background:#1b1b1b;color:#eee}"
               "button{margin-top:18px;padding:12px 18px;font:inherit;border:0;border-radius:8px;background:#3987e5;color:#fff;width:100%}small{color:#888}</style></head><body>"
               "<h2>JBOD fans setup</h2><p><small>The fans keep running at the failsafe speed while you do this.</small></p><form method=post action=/setup>");
  h += "<label>Wi-Fi network</label><input name=ssid list=nets required value=\"" + esc(cfg.ssid) + "\"><datalist id=nets>" + opts + "</datalist>";
  h += "<label>Wi-Fi password</label><input name=pass type=password placeholder=\"" + String(cfg.pass.length() ? "(unchanged)" : "") + "\">";
  h += "<label>API token <small>(same as JBOD_TOKEN in docker-compose.yml)</small></label><input name=token value=\"" + esc(cfg.token) + "\">";
  h += "<label>Name <small>(reachable as NAME.local)</small></label><input name=host value=\"" + esc(cfg.host) + "\">";
  h += "<label>Update password <small>(over-the-air firmware updates; empty = off)</small></label><input name=ota type=password placeholder=\"" + String(cfg.ota.length() ? "(unchanged)" : "") + "\">";
  h += F("<button>Save and restart</button></form><p><small>Rescan: reload this page.</small></p></body></html>");
  http.send(200, "text/html", h);
}

static void handleSetupPost() {
  if (!viaSetupAp() && !authorized()) { http.send(401, "text/plain", "bad token"); return; }
  String ssid = http.arg("ssid"); ssid.trim();
  if (!ssid.length()) { http.send(400, "text/plain", "Wi-Fi network is required"); return; }
  prefs.putString("ssid", ssid);
  if (http.arg("pass").length()) prefs.putString("pass", http.arg("pass"));
  prefs.putString("token", http.arg("token"));
  String host = http.arg("host"); host.trim();
  prefs.putString("host", host.length() ? host : String("jbod-fans"));
  if (http.arg("ota").length()) prefs.putString("ota", http.arg("ota"));
  http.send(200, "text/html", "<meta name=viewport content='width=device-width'><body style='font:16px system-ui;background:#111;color:#eee;margin:16px'>"
                              "Saved. Restarting and joining <b>" + esc(ssid) + "</b>&hellip;<br><br>Then open <b>http://" + esc(host.length() ? host : String("jbod-fans")) + ".local/</b></body>");
  delay(800);
  ESP.restart();
}

// ---- firmware update from the browser ---------------------------------------
static bool updAllowed = false;
static void handleUpdateGet() {
  if (!viaSetupAp() && !authorized()) { http.send(401, "text/plain", "add ?token=YOUR_TOKEN"); return; }
  String tok = http.arg("token");
  http.send(200, "text/html", "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'><body style='font:16px system-ui;background:#111;color:#eee;margin:16px;max-width:460px'>"
            "<h2>Firmware update</h2><p>Running " FW_VERSION ". Pick a <b>jbod-fans-*-ota.bin</b> file. The fans stay at their current speed during the upload, then it restarts (100 % for 2 s).</p>"
            "<form method=post enctype=multipart/form-data action='/update?token=" + esc(tok) + "'><input type=file name=fw accept=.bin required><br><br>"
            "<button style='padding:12px 18px;font:inherit;border:0;border-radius:8px;background:#3987e5;color:#fff'>Upload and install</button></form></body>");
}
static void handleUpdateUpload() {
  HTTPUpload &u = http.upload();
  if (u.status == UPLOAD_FILE_START) {
    updAllowed = viaSetupAp() || authorized();
    if (updAllowed) { Serial.printf("update: %s\n", u.filename.c_str()); Update.begin(UPDATE_SIZE_UNKNOWN); }
  } else if (u.status == UPLOAD_FILE_WRITE && updAllowed) {
    Update.write(u.buf, u.currentSize);
  } else if (u.status == UPLOAD_FILE_END && updAllowed) {
    Update.end(true);
  } else if (u.status == UPLOAD_FILE_ABORTED && updAllowed) {
    Update.abort();
  }
}
static void handleUpdateDone() {
  if (!updAllowed) { http.send(401, "text/plain", "bad token"); return; }
  bool ok = !Update.hasError();
  http.send(ok ? 200 : 500, "text/html", String("<body style='font:16px system-ui;background:#111;color:#eee;margin:16px'>") +
            (ok ? "Installed. Restarting&hellip;" : "Update failed: " + String(Update.errorString())) + "</body>");
  if (ok) { delay(800); ESP.restart(); }
}

static void startSetupAp() {
  if (apOn) return;
  WiFi.mode(cfg.ssid.length() ? WIFI_AP_STA : WIFI_AP);
  WiFi.softAP(SETUP_AP_SSID, SETUP_AP_PASS);
  dns.start(53, "*", WiFi.softAPIP());       // captive portal: every name -> the setup page
  WiFi.scanNetworks(true);
  apOn = true;
  Serial.printf("setup AP \"%s\" (password %s) at http://%s/setup\n", SETUP_AP_SSID, SETUP_AP_PASS, WiFi.softAPIP().toString().c_str());
}

static void stopSetupAp() {
  if (!apOn) return;
  dns.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  apOn = false;
  Serial.println("joined Wi-Fi, setup AP closed");
}

static void loadConfig() {
  cfg.ssid = prefs.getString("ssid", WIFI_SSID);
  cfg.pass = prefs.getString("pass", WIFI_PASSWORD);
  cfg.token = prefs.getString("token", API_TOKEN);
  cfg.host = prefs.getString("host", HOSTNAME);
  cfg.ota = prefs.getString("ota", OTA_PASSWORD);
  if (!cfg.host.length()) cfg.host = "jbod-fans";
}

static void checkBootButton() {
  static uint32_t downAt = 0;
  if (digitalRead(BOOT_BTN) == HIGH) { downAt = 0; return; }
  if (!downAt) downAt = millis();
  if (millis() - downAt > 5000) {
    Serial.println("BOOT held 5 s: forgetting Wi-Fi");
    for (const char *k : {"ssid", "pass"}) prefs.remove(k);
    for (int i = 0; i < 6; i++) { digitalWrite(LED_PIN, i % 2); delay(150); }
    ESP.restart();
  }
}

static const char PAGE[] PROGMEM = R"HTML(<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>JBOD fans</title>
<style>body{font:15px system-ui,sans-serif;margin:16px;background:#111;color:#eee}pre{background:#1b1b1b;padding:12px;border-radius:8px}</style>
</head><body><h3>JBOD fans</h3><pre id="s">loading…</pre><p><a style="color:#8ab4f8" href="/setup">Wi-Fi setup</a> · <a style="color:#8ab4f8" href="/update">firmware update</a> (add ?token=… on your network)</p>
<script>async function t(){try{const r=await fetch('/status');document.getElementById('s').textContent=JSON.stringify(await r.json(),null,2)}catch(e){}}t();setInterval(t,2000)</script>
</body></html>)HTML";

// ---- control loop -----------------------------------------------------------
static void readProbe() {
#if USE_EXHAUST_PROBE
  static uint32_t last = 0;
  if (!probePresent || millis() - last < 2000) return;
  last = millis();
  float c = probe.getTempCByIndex(0);          // result of the previous request
  probeC = (c <= -100 || c >= 125) ? NAN : c;  // -127 = disconnected
  probe.requestTemperatures();                 // async; read next time round
#endif
}

static void updateRpm() {
  static uint32_t lastMs = 0;
  uint32_t now = millis();
  if (now - lastMs < 1000) return;
  uint32_t dt = now - lastMs;
  lastMs = now;
  for (int i = 0; i < FAN_COUNT; i++) {
    noInterrupts();
    uint32_t p = tach[i].pulses;
    tach[i].pulses = 0;
    interrupts();
    rpm[i] = (int)(p * 60000UL / PULSES_PER_REV / dt);
    // Stall: asked to spin, isn't. Ignore the first seconds after boot.
    if (stStep < 0 && appliedPct >= STALL_MIN_PCT && rpm[i] < STALL_RPM && now > 10000) {
      if (!lowSinceMs[i]) lowSinceMs[i] = now;
      stalled[i] = now - lowSinceMs[i] >= STALL_AFTER_MS;
    } else {
      lowSinceMs[i] = 0;
      stalled[i] = false;
    }
  }
}

static void runSelftest(uint32_t now) {
  for (int i = 0; i < FAN_COUNT; i++) pwmWrite(i, stPct(stStep, i));
  if (now - stStepAt < ST_STEP_MS) return;
  // rpm[] is refreshed every second, so after 6 s it reflects the settled speed
  for (int i = 0; i < FAN_COUNT; i++) {
    if (stStep < 3) stRpm[stStep][i] = rpm[i];
    else if (stStep - 3 == i) stOwn[i] = rpm[i];
  }
  if (++stStep >= ST_STEPS) { stStep = -1; stDone = true; appliedPct = -1; }  // -1 forces a rewrite below
  stStepAt = now;
}

static void control() {
  uint32_t now = millis();
  if (stStep >= 0) {
    mode = "selftest";
    runSelftest(now);
    digitalWrite(LED_PIN, (now / 300) % 2);
    if (stStep >= 0) return;
  }
  bool fresh = cmdPct >= 0 && now - cmdAtMs < ttlMs;
  int local = isnan(probeC) ? -1 : curvePct(probeC);
  int pct;
  if (now < 2000) {                 // spin-up kick at boot
    pct = 100; mode = "boot";
  } else if (fresh) {
    pct = cmdPct; mode = "server";
    if (local > pct) { pct = local; mode = "server+probe"; }
  } else if (local >= 0) {
    pct = max(local, PROBE_ONLY_MIN_PCT); mode = "probe (server lost)";
  } else {
    pct = failsafePct; mode = "failsafe";
  }
  bool anyStall = false;
  for (int i = 0; i < FAN_COUNT; i++) anyStall |= stalled[i];
  if (anyStall) { pct = 100; mode = "stall: others at 100%"; }
  pct = constrain(pct, MIN_PCT, 100);
  if (pct != appliedPct) {
    appliedPct = pct;
    for (int i = 0; i < FAN_COUNT; i++) pwmWrite(i, pct);
  }
  // LED: solid = server in charge, slow blink = failsafe/probe, fast blink = stall
  bool on = anyStall ? (now / 150) % 2 : (fresh ? true : (now / 700) % 2);
  digitalWrite(LED_PIN, on);
}

static void keepWifi() {
  static uint32_t downSince = 0, retryAt = 0;
  if (apOn) dns.processNextRequest();
  if (!cfg.ssid.length()) return;                    // setup AP only
  static bool wasUp = false;
  if (WiFi.status() == WL_CONNECTED) {
    if (!wasUp) Serial.printf("Wi-Fi up: http://%s/  (%s.local)\n", WiFi.localIP().toString().c_str(), cfg.host.c_str());
    wasUp = true; downSince = 0; stopSetupAp(); return;
  }
  wasUp = false;
  uint32_t now = millis();
  if (!downSince) downSince = retryAt = now;
  if (now - downSince > SETUP_AP_AFTER_MS) startSetupAp();   // keep trying, but let you fix it
  if (now - retryAt > 30000) { WiFi.disconnect(); WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str()); retryAt = now; }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  pinMode(BOOT_BTN, INPUT_PULLUP);
  for (int i = 0; i < FAN_COUNT; i++) {
    pwmAttach(i);
    pwmWrite(i, 100);
    pinMode(TACH_PINS[i], TACH_PINS[i] >= 34 ? INPUT : INPUT_PULLUP);
    attachInterruptArg(TACH_PINS[i], onTach, &tach[i], FALLING);
  }
  prefs.begin("jbodfans", false);
  failsafePct = constrain(prefs.getInt("fs", DEFAULT_FAILSAFE_PCT), 30, 100);
  loadConfig();

#if USE_EXHAUST_PROBE
  probe.begin();
  probePresent = probe.getDeviceCount() > 0;
  probe.setWaitForConversion(false);
  if (probePresent) probe.requestTemperatures();
  Serial.printf("exhaust probe: %s\n", probePresent ? "found" : "none");
#endif

  WiFi.setHostname(cfg.host.c_str());
  if (cfg.ssid.length()) {
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
    Serial.printf("joining Wi-Fi \"%s\" as %s.local\n", cfg.ssid.c_str(), cfg.host.c_str());
  } else {
    startSetupAp();                // first boot: no Wi-Fi saved yet
  }

  if (cfg.ota.length()) {
    ArduinoOTA.setHostname(cfg.host.c_str());
    ArduinoOTA.setPassword(cfg.ota.c_str());
    ArduinoOTA.begin();            // also starts mDNS
  } else {
    MDNS.begin(cfg.host.c_str());
  }
  MDNS.addService("http", "tcp", 80);

  const char *hdrs[] = {"X-Token"};
  http.collectHeaders(hdrs, 1);
  http.on("/", HTTP_GET, [] { http.send_P(200, "text/html", PAGE); });
  http.on("/status", HTTP_GET, [] { http.send(200, "application/json", statusJson()); });
  http.on("/set", HTTP_POST, handleSet);
  http.on("/selftest", HTTP_POST, handleSelftest);
  http.on("/setup", HTTP_GET, handleSetupGet);
  http.on("/setup", HTTP_POST, handleSetupPost);
  http.on("/update", HTTP_GET, handleUpdateGet);
  http.on("/update", HTTP_POST, handleUpdateDone, handleUpdateUpload);
  http.onNotFound([] {
    if (viaSetupAp()) { http.sendHeader("Location", "http://" + WiFi.softAPIP().toString() + "/setup"); http.send(302, "text/plain", ""); return; }
    http.send(404, "application/json", "{\"error\":\"not found\"}");
  });
  http.begin();
  Serial.printf("jbod-fans %s, failsafe %d%%\n", FW_VERSION, failsafePct);
}

void loop() {
  http.handleClient();
  if (cfg.ota.length()) ArduinoOTA.handle();
  checkBootButton();
  readProbe();
  updateRpm();
  control();
  keepWifi();
  delay(5);
}
