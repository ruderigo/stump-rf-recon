// =============================================================================
// Stump Dual-Band RF Recon Monitor v0.3.7-DUALBAND-TDM
// Hardware: Heltec WiFi LoRa 32 V3 (ESP32-S3 Dual-Core + Semtech SX1262)
//
// Concurrent Dual-Band Architecture:
// - Core 1 (app_cpu): Exclusive owner of SX1262 Sub-GHz SPI bus, DIO1 ISR & TDM
//                     Single-Channel Probe (zero cross-core SPI contention)
// - Core 0 (pro_cpu): Exclusive owner of USB-Serial UART0 JSONL output +
//                     ESP32-S3 2.4 GHz 802.11 Promiscuous Sniffer (Ch 1-13 Hop)
// - Strictly Zero-TX: Neither SX1262 nor ESP32 802.11 ever transmits a frame.
// =============================================================================

#include <Arduino.h>
#include <RadioLib.h>
#include <WiFi.h>
#include "esp_wifi.h"
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>

#ifndef STUMP_FW_VERSION
#define STUMP_FW_VERSION "0.3.7-DUALBAND-TDM"
#endif

#ifndef STUMP_TCXO_V
#define STUMP_TCXO_V 1.8
#endif

// ---- Heltec WiFi LoRa 32 V3 SX1262 & Board Pin Map ----
#define PIN_LORA_NSS   8
#define PIN_LORA_SCK   9
#define PIN_LORA_MOSI  10
#define PIN_LORA_MISO  11
#define PIN_LORA_RST   12
#define PIN_LORA_BUSY  13
#define PIN_LORA_DIO1  14
#define PIN_LED        35  // Heltec V3 onboard white status LED

SX1262 radio = new Module(PIN_LORA_NSS, PIN_LORA_DIO1, PIN_LORA_RST, PIN_LORA_BUSY);

struct Config {
  float    freq     = 915.0; // MHz
  float    bw       = 125.0; // kHz
  uint8_t  sf       = 8;     // 7..12
  uint8_t  cr       = 5;     // 5..8 (4/5..4/8)
  uint8_t  sync     = 0x12;  // 0x12=Reticulum, 0x2B=Meshtastic, 0x34=LoRaWAN
  uint16_t preamble = 8;
  bool     payload  = true;
  bool     wifi     = true;  // 2.4 GHz 802.11 Promiscuous Mode enabled
  uint8_t  wifiCh   = 0;     // 0 = Auto-hop Ch 1..13, 1..13 = Fixed channel
} cfg;

enum Mode { MODE_SNIFF, MODE_SWEEP, MODE_SCAN, MODE_IDLE };
volatile Mode mode = MODE_SNIFF;
volatile bool running = true;
volatile bool radioReconfigPending = false;
volatile bool radioFreqOnlyPending = false;

#define SCAN_MAX 24
float scanList[SCAN_MAX] = {
  903.0, 905.0, 907.0, 909.0, 911.0, 913.0, 915.0, 917.0, 919.0, 921.0, 923.0, 925.0
};
uint8_t scanCount = 12;

// ---- LoRa Inter-Core Packet Queue (Core 1 -> Core 0) ----
struct RxPacketMsg {
  uint32_t t;
  float freq;
  float bw;
  uint8_t sf;
  uint8_t cr;
  uint8_t sync;
  int16_t len;
  float rssi;
  float snr;
  float ferr;
  bool crc_ok;
  uint8_t payload[256];
};

// ---- Sub-GHz Scan Inter-Core Queue (Core 1 -> Core 0, prevents UART interleaving) ----
struct ScanMsg {
  uint32_t t;
  float freq;
  float rssi;
};

// ---- Wi-Fi 802.11 Promiscuous Queue ----
struct WifiFrameMsg {
  uint32_t t;
  uint8_t ch;
  int8_t rssi;
  uint16_t len;
  char ftype[12];
  uint8_t srcMac[6];
  uint8_t bssid[6];
  bool laa;
  char ssid[33];
};

QueueHandle_t rxQueue = nullptr;
QueueHandle_t scanQueue = nullptr;
QueueHandle_t wifiQueue = nullptr;
volatile bool rxFlag = false;

void IRAM_ATTR onDio1Action() {
  rxFlag = true;
}

// ---- 802.11 Promiscuous Sniffer Callback (Core 0 ISR Context) ----
void IRAM_ATTR wifiPromiscuousCb(void* buf, wifi_promiscuous_pkt_type_t type) {
  if (!cfg.wifi || !running || type == WIFI_PKT_MISC || !wifiQueue) return;

  const wifi_promiscuous_pkt_t* pkt = (const wifi_promiscuous_pkt_t*)buf;
  const uint8_t* frame = pkt->payload;
  uint16_t len = pkt->rx_ctrl.sig_len;
  if (len < 24) return;

  uint8_t fc0 = frame[0];
  uint8_t subtype = (fc0 >> 4) & 0x0F;
  uint8_t ftype_bits = (fc0 >> 2) & 0x03;

  WifiFrameMsg msg;
  memset(&msg, 0, sizeof(msg));
  msg.t = millis();
  msg.ch = pkt->rx_ctrl.channel;
  msg.rssi = pkt->rx_ctrl.rssi;
  msg.len = len;

  memcpy(msg.srcMac, frame + 10, 6);
  memcpy(msg.bssid, frame + 16, 6);
  msg.laa = (msg.srcMac[0] & 0x02) != 0;

  if (ftype_bits == 0) { // Management Frame
    if (subtype == 4) strcpy(msg.ftype, "PROBE_REQ");
    else if (subtype == 8) strcpy(msg.ftype, "BEACON");
    else if (subtype == 0) strcpy(msg.ftype, "ASSOC_REQ");
    else if (subtype == 12) strcpy(msg.ftype, "DEAUTH");
    else return;

    int ieOffset = (subtype == 8) ? 36 : 24;
    if (len > ieOffset + 2 && frame[ieOffset] == 0x00) {
      uint8_t ssidLen = frame[ieOffset + 1];
      if (ssidLen > 0 && ssidLen <= 32 && ieOffset + 2 + ssidLen <= len) {
        for (uint8_t i = 0; i < ssidLen; i++) {
          char c = (char)frame[ieOffset + 2 + i];
          msg.ssid[i] = (c >= 32 && c <= 126 && c != '"' && c != '\\') ? c : '?';
        }
        msg.ssid[ssidLen] = '\0';
      }
    }
  } else if (ftype_bits == 2) {
    if (msg.rssi < -72) return;
    strcpy(msg.ftype, "DATA");
  } else {
    return;
  }

  xQueueSendFromISR(wifiQueue, &msg, NULL);
}

// Executed ONLY on Core 1 so SPI bus never collides between cores
bool applyRadioConfigCore1() {
  int st = radio.begin(
    cfg.freq, cfg.bw, cfg.sf, cfg.cr, cfg.sync,
    10 /* dummy power, zero TX */, cfg.preamble,
    (float)STUMP_TCXO_V, false
  );
  if (st != RADIOLIB_ERR_NONE) return false;
  radio.setRxBoostedGainMode(true);
  radio.setDio1Action(onDio1Action);
  if (running && mode != MODE_IDLE) {
    radio.startReceive();
  } else {
    radio.standby();
  }
  return true;
}

void pushScanPoint(float freq, float rssi) {
  if (!scanQueue) return;
  if (rssi >= -15.0f || rssi <= -140.0f) rssi = -116.0f;
  ScanMsg sm;
  sm.t = millis();
  sm.freq = freq;
  sm.rssi = rssi;
  xQueueSend(scanQueue, &sm, 0);
}

void drainLoRaPacketIfReady(float currentFreq) {
  if (!rxFlag) return;
  rxFlag = false;

  int len = radio.getPacketLength();
  if (len < 0) len = 0;
  if (len > 256) len = 256;

  RxPacketMsg msg;
  memset(&msg, 0, sizeof(msg));
  msg.t = millis();
  msg.freq = currentFreq;
  msg.bw = cfg.bw;
  msg.sf = cfg.sf;
  msg.cr = cfg.cr;
  msg.sync = cfg.sync;
  msg.len = len;

  int st = RADIOLIB_ERR_NONE;
  if (len > 0) {
    st = radio.readData(msg.payload, len);
  }
  msg.crc_ok = (st == RADIOLIB_ERR_NONE);
  msg.rssi = radio.getRSSI(true);
  msg.snr = radio.getSNR();
  msg.ferr = radio.getFrequencyError();

  // Emit packet if valid payload OR valid latched LoRa RF signature
  if ((len > 0 || (msg.rssi > -128.0f && msg.rssi < -10.0f)) && rxQueue) {
    xQueueSend(rxQueue, &msg, 0);
  }
  radio.startReceive();
}

// ---- Core 1 Task: Exclusive SX1262 Sub-GHz Driver ----
void radioCore1Task(void* pvParameters) {
  SPI.begin(PIN_LORA_SCK, PIN_LORA_MISO, PIN_LORA_MOSI, PIN_LORA_NSS);
  applyRadioConfigCore1();

  uint32_t sweepLast = 0;
  uint32_t probeLast = 0;
  uint8_t scanIdx = 0;
  float activeFreq = cfg.freq;

  for (;;) {
    if (radioReconfigPending) {
      radioReconfigPending = false;
      radioFreqOnlyPending = false;
      applyRadioConfigCore1();
      activeFreq = cfg.freq;
    } else if (radioFreqOnlyPending) {
      radioFreqOnlyPending = false;
      activeFreq = cfg.freq;
      radio.setFrequency(activeFreq);
      if (running && mode != MODE_IDLE) radio.startReceive();
    }

    if (!running || mode == MODE_IDLE) {
      vTaskDelay(pdMS_TO_TICKS(15));
      continue;
    }

    // 1. Always prioritize incoming LoRa / Reticulum packet reception
    drainLoRaPacketIfReady(activeFreq);

    if (mode == MODE_SNIFF) {
      activeFreq = cfg.freq;
      // Single-channel 5ms TDM probe every 340ms (>98.5% continuous listen on cfg.freq!)
      if (millis() - probeLast >= 340) {
        probeLast = millis();
        float homeRssi = radio.getRSSI(false);
        // If home channel has an active carrier (> -94 dBm) or rxFlag, stay locked on home channel!
        if (rxFlag || (homeRssi > -94.0f && homeRssi < -15.0f)) {
          pushScanPoint(activeFreq, homeRssi);
          drainLoRaPacketIfReady(activeFreq);
        } else {
          float f = scanList[scanIdx];
          scanIdx = (scanIdx + 1) % scanCount;
          if (fabs(f - activeFreq) < 0.2f) {
            pushScanPoint(activeFreq, homeRssi);
          } else {
            radio.setFrequency(f);
            radio.startReceive();
            vTaskDelay(pdMS_TO_TICKS(5));
            float r = radio.getRSSI(false);
            pushScanPoint(f, r);
            drainLoRaPacketIfReady(f);
            // Immediately return to home frequency (915.000 MHz)
            radio.setFrequency(activeFreq);
            radio.startReceive();
          }
        }
      }
    } else if (mode == MODE_SWEEP) {
      // Active RX frequency sweep across 903.000..925.000 MHz (dwells 650ms on cfg.freq, 220ms on others)
      uint32_t dwellMs = (fabs(activeFreq - cfg.freq) < 0.6f) ? 650 : 220;
      if (millis() - sweepLast >= dwellMs) {
        sweepLast = millis();
        scanIdx = (scanIdx + 1) % scanCount;
        activeFreq = scanList[scanIdx];
        radio.setFrequency(activeFreq);
        radio.startReceive();
        vTaskDelay(pdMS_TO_TICKS(6));
        float r = radio.getRSSI(false);
        pushScanPoint(activeFreq, r);
      }
    } else if (mode == MODE_SCAN) {
      float f = scanList[scanIdx];
      activeFreq = f;
      radio.setFrequency(f);
      radio.startReceive();
      vTaskDelay(pdMS_TO_TICKS(7));
      drainLoRaPacketIfReady(activeFreq);
      float r = radio.getRSSI(false);
      pushScanPoint(f, r);
      scanIdx = (scanIdx + 1) % scanCount;
      // After each 12-channel sweep pass, dwell 380ms on cfg.freq (915.000 MHz) to decode Reticulum packets
      if (scanIdx == 0) {
        activeFreq = cfg.freq;
        radio.setFrequency(activeFreq);
        radio.startReceive();
        for (int w = 0; w < 19; w++) {
          vTaskDelay(pdMS_TO_TICKS(20));
          drainLoRaPacketIfReady(activeFreq);
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

void emitStatus(const char* note) {
  Serial.printf(
    "{\"ev\":\"status\",\"t\":%lu,\"fw\":\"%s\",\"mode\":\"%s\",\"run\":%s,"
    "\"freq\":%.3f,\"bw\":%.1f,\"sf\":%u,\"cr\":%u,\"sync\":%u,\"wifi\":%s,\"wifich\":%u,\"payload\":%s",
    millis(), STUMP_FW_VERSION,
    mode == MODE_SNIFF ? "sniff" : mode == MODE_SWEEP ? "sweep" : mode == MODE_SCAN ? "scan" : "idle",
    running ? "true" : "false",
    cfg.freq, cfg.bw, cfg.sf, cfg.cr, cfg.sync,
    cfg.wifi ? "true" : "false", cfg.wifiCh,
    cfg.payload ? "true" : "false"
  );
  if (note) Serial.printf(",\"note\":\"%s\"", note);
  Serial.println("}");
}

void emitStatus() {
  emitStatus(nullptr);
}

String serialInputLine = "";

void parseHostCommand(String s) {
  s.trim();
  if (s.length() == 0) return;
  int sp = s.indexOf(' ');
  String cmd = (sp < 0) ? s : s.substring(0, sp);
  String arg = (sp < 0) ? "" : s.substring(sp + 1);
  cmd.toLowerCase();

  if (cmd == "help") {
    Serial.println("{\"ev\":\"help\",\"cmds\":[\"status\",\"freq <MHz>\",\"bw <kHz>\",\"sf <7-12>\",\"cr <5-8>\",\"sync <0xNN>\",\"wifi on|off\",\"wifich <1-13|hop>\",\"mode sniff|sweep|scan|idle\",\"start\",\"stop\"]}");
    return;
  }
  if (cmd == "status")   { emitStatus(); return; }
  if (cmd == "start")    { running = true; radioReconfigPending = true; emitStatus("start"); return; }
  if (cmd == "stop")     { running = false; radioReconfigPending = true; emitStatus("stop"); return; }
  if (cmd == "freq") {
    float nf = arg.toFloat();
    if (nf >= 150.0f && nf <= 960.0f) {
      cfg.freq = nf;
      radioFreqOnlyPending = true;
    }
    emitStatus("freq");
    return;
  }
  if (cmd == "bw")       { cfg.bw = arg.toFloat(); radioReconfigPending = true; emitStatus("bw"); return; }
  if (cmd == "sf") {
    uint8_t nsf = constrain(arg.toInt(), 7, 12);
    if (nsf != cfg.sf) { cfg.sf = nsf; radioReconfigPending = true; }
    emitStatus("sf");
    return;
  }
  if (cmd == "cr") {
    uint8_t ncr = constrain(arg.toInt(), 5, 8);
    if (ncr != cfg.cr) { cfg.cr = ncr; radioReconfigPending = true; }
    emitStatus("cr");
    return;
  }
  if (cmd == "sync") {
    uint8_t nsy = (uint8_t) strtol(arg.c_str(), nullptr, 0);
    if (nsy != cfg.sync) { cfg.sync = nsy; radioReconfigPending = true; }
    emitStatus("sync");
    return;
  }
  if (cmd == "payload")  { cfg.payload = (arg == "on" || arg == "1" || arg == "true"); emitStatus("payload"); return; }
  if (cmd == "wifi") {
    cfg.wifi = (arg == "on" || arg == "1" || arg == "true");
    esp_wifi_set_promiscuous(cfg.wifi);
    emitStatus("wifi");
    return;
  }
  if (cmd == "wifich") {
    if (arg == "hop" || arg == "0") cfg.wifiCh = 0;
    else {
      cfg.wifiCh = constrain(arg.toInt(), 1, 13);
      esp_wifi_set_channel(cfg.wifiCh, WIFI_SECOND_CHAN_NONE);
    }
    emitStatus("wifich");
    return;
  }
  if (cmd == "mode") {
    Mode prev = mode;
    if (arg == "sniff") mode = MODE_SNIFF;
    else if (arg == "sweep") mode = MODE_SWEEP;
    else if (arg == "scan")  mode = MODE_SCAN;
    else if (arg == "idle")  mode = MODE_IDLE;
    if (prev == MODE_IDLE || mode == MODE_IDLE) {
      radioReconfigPending = true;
    } else {
      radioFreqOnlyPending = true;
    }
    emitStatus("mode");
    return;
  }
}

uint32_t lastWifiHop = 0;
uint32_t lastWifiEmit = 0;
uint8_t lastBeaconMac5 = 0;
uint8_t currentHopCh = 1;
uint32_t ledOffTime = 0;
char hexBuf[520];

void setup() {
  pinMode(PIN_LED, OUTPUT);
  for (int i = 0; i < 3; i++) {
    digitalWrite(PIN_LED, HIGH);
    delay(60);
    digitalWrite(PIN_LED, LOW);
    delay(60);
  }

  Serial.begin(115200);
  rxQueue = xQueueCreate(16, sizeof(RxPacketMsg));
  scanQueue = xQueueCreate(24, sizeof(ScanMsg));
  wifiQueue = xQueueCreate(12, sizeof(WifiFrameMsg));

  WiFi.mode(WIFI_MODE_NULL);
  wifi_init_config_t wcfg = WIFI_INIT_CONFIG_DEFAULT();
  esp_wifi_init(&wcfg);
  esp_wifi_set_storage(WIFI_STORAGE_RAM);
  esp_wifi_set_mode(WIFI_MODE_NULL);
  esp_wifi_start();
  esp_wifi_set_promiscuous_rx_cb(&wifiPromiscuousCb);
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);

  xTaskCreatePinnedToCore(radioCore1Task, "RadioCore1", 8192, NULL, configMAX_PRIORITIES - 1, NULL, 1);

  delay(200);
  Serial.println("{\"ev\":\"boot\",\"fw\":\"" STUMP_FW_VERSION "\",\"role\":\"dualband-lora-wifi-passive\"}");
  emitStatus("init");
}

void loop() {
  if (ledOffTime > 0 && millis() >= ledOffTime) {
    digitalWrite(PIN_LED, LOW);
    ledOffTime = 0;
  }

  // 1. Drain Sub-GHz LoRa Packet Queue (Atomic single-line JSON print on Core 0)
  RxPacketMsg msg;
  while (rxQueue && xQueueReceive(rxQueue, &msg, 0) == pdTRUE) {
    digitalWrite(PIN_LED, HIGH);
    ledOffTime = millis() + 25;
    int n = 0;
    if (cfg.payload && msg.len > 0) {
      for (int i = 0; i < msg.len && i < 256; i++) {
        snprintf(hexBuf + (i * 2), 3, "%02x", msg.payload[i]);
      }
      n = msg.len > 256 ? 256 : msg.len;
    }
    hexBuf[n * 2] = '\0';
    Serial.printf(
      "{\"ev\":\"rx\",\"t\":%lu,\"freq\":%.3f,\"bw\":%.1f,\"sf\":%u,\"cr\":%u,\"sync\":%u,"
      "\"len\":%d,\"rssi\":%.1f,\"snr\":%.2f,\"ferr\":%.0f,\"crc_ok\":%s,\"payload\":\"%s\"}\n",
      msg.t, msg.freq, msg.bw, msg.sf, msg.cr, msg.sync,
      msg.len, msg.rssi, msg.snr, msg.ferr, msg.crc_ok ? "true" : "false", hexBuf
    );
  }

  // 2. Drain Sub-GHz Scan Queue (Atomic single-line JSON print on Core 0)
  ScanMsg smsg;
  while (scanQueue && xQueueReceive(scanQueue, &smsg, 0) == pdTRUE) {
    Serial.printf("{\"ev\":\"scan\",\"t\":%lu,\"freq\":%.3f,\"rssi\":%.1f}\n", smsg.t, smsg.freq, smsg.rssi);
  }

  // 3. Drain 2.4 GHz 802.11 Promiscuous Queue
  WifiFrameMsg wmsg;
  if (wifiQueue && xQueueReceive(wifiQueue, &wmsg, 0) == pdTRUE) {
    bool isBeacon = (strcmp(wmsg.ftype, "BEACON") == 0);
    uint32_t minInterval = isBeacon ? 450 : 120;
    if ((millis() - lastWifiEmit >= minInterval) && (!isBeacon || wmsg.srcMac[5] != lastBeaconMac5)) {
      lastWifiEmit = millis();
      if (isBeacon) lastBeaconMac5 = wmsg.srcMac[5];
      digitalWrite(PIN_LED, HIGH);
      ledOffTime = millis() + 10;
      Serial.printf(
        "{\"ev\":\"wifi\",\"t\":%lu,\"ch\":%u,\"rssi\":%d,\"ftype\":\"%s\","
        "\"mac\":\"%02x:%02x:%02x:%02x:%02x:%02x\","
        "\"bssid\":\"%02x:%02x:%02x:%02x:%02x:%02x\","
        "\"laa\":%s,\"ssid\":\"%s\",\"len\":%u}\n",
        wmsg.t, wmsg.ch, wmsg.rssi, wmsg.ftype,
        wmsg.srcMac[0], wmsg.srcMac[1], wmsg.srcMac[2], wmsg.srcMac[3], wmsg.srcMac[4], wmsg.srcMac[5],
        wmsg.bssid[0], wmsg.bssid[1], wmsg.bssid[2], wmsg.bssid[3], wmsg.bssid[4], wmsg.bssid[5],
        wmsg.laa ? "true" : "false", wmsg.ssid, wmsg.len
      );
    }
  }

  // 4. Auto-hop 2.4 GHz Wi-Fi Channels 1..13 every 250ms
  if (cfg.wifi && cfg.wifiCh == 0 && (millis() - lastWifiHop > 250)) {
    lastWifiHop = millis();
    currentHopCh = (currentHopCh % 13) + 1;
    esp_wifi_set_channel(currentHopCh, WIFI_SECOND_CHAN_NONE);
  }

  // 5. Poll Host Serial Commands
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      parseHostCommand(serialInputLine);
      serialInputLine = "";
    } else if (serialInputLine.length() < 128) {
      serialInputLine += c;
    }
  }

  vTaskDelay(pdMS_TO_TICKS(2));
}
