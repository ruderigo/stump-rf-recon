# STUMP Dual-Band RF Recon Monitor (`v0.3.7`)

A 100% passive, zero-transmit dual-band (**Sub-GHz LoRa 902–928 MHz** + **2.4 GHz 802.11 Wi-Fi**) reconnaissance and protocol dissector system built for the **Heltec WiFi LoRa 32 V3** (`ESP32-S3 + Semtech SX1262`) paired with the **XiFan R36MAX** handheld (`720×720` display) or the **Web Serial Recon Console**.

---

## 1. What It Can Sniff (Passive Dual-Band RF Capture)

All radio operations are **strictly receive-only (Zero-TX)**. Neither the SX1262 Sub-GHz transceiver nor the ESP32-S3 2.4 GHz Wi-Fi radio ever transmits a frame.

### Sub-GHz LoRa & Mesh (`902–928 MHz ISM` · ESP32-S3 Core 1 + SX1262)
- **Reticulum Network Stack (RNS / RNode `0x12`)**:
  - Captures `ANNOUNCE`, `DATA`, `LINK_REQ`, and `PROOF` frames.
  - Extracts 16-byte Destination Hashes, hop counts, header types (`HEADER_1` / `HEADER_2`), and IFAC flags.
- **Meshtastic (`0x2B`)**:
  - Extracts sender Node ID (`!from`), destination Node ID (`!to`), hop limit, ACK flags, and channel hash.
- **Raw / Custom LoRa (`0x34` / Proprietary) & Active RF Carrier Bursts**:
  - Captures arbitrary LoRa payloads (`SF7–SF12`, `BW 62.5–500 kHz`, `CR 4/5–4/8`) and detects unmodulated or cross-SF Sub-GHz carrier bursts (`>= 11.5 dB` above noise median) during TDM sweeps.
- **Physical-Layer Hardware Disambiguation**:
  - Measures per-packet **Crystal Frequency Error (`Ferr` in Hz)**, **RSSI (`dBm`)**, and **SNR (`dB`)** to fingerprint and cluster physical SX1262 transmitters—allowing you to distinguish two identical nodes sitting side by side or group multiple RNS destination hashes back to the same physical radio.

### 2.4 GHz 802.11 Wi-Fi Promiscuous Sniffer (`Channels 1–13` · ESP32-S3 Core 0)
- **Management & Data Frame Interception**:
  - Captures `PROBE_REQ`, `BEACON`, `ASSOC_REQ`, `DEAUTH`, and high-RSSI `DATA` frames while automatically hopping across Channels 1–13 (or locked to a single channel).
- **SSID & IEEE OUI Fingerprinting**:
  - Extracts target and broadcast SSIDs from 802.11 Information Elements (`IE Tag 0x00`) and resolves source/BSSID MAC prefixes against an onboard IEEE OUI vendor lookup table (Espressif/Heltec, Apple, Raspberry Pi, Ralink, Realtek, TP-Link, etc.).
- **LAA Randomized MAC De-Duplication**:
  - Inspects the Locally Administered Address bit (`mac[0] & 0x02`) and clusters rotating mobile Probe Request bursts by target SSID and Kalman RSSI proximity so a single phone rotating MACs does not flood the radar.

---

## 2. What It Renders & How

On the **R36MAX** (`stump_gui.py`), graphics are rendered at `720×720` via SDL2 `KMSDRM` with a direct `/dev/fb0` `mmap` stride-aware framebuffer blitter fallback. Signal levels are processed through a **1D Kalman filter** for jitter-free RSSI bars and distance estimation.

### 5 Tactical Views (`[L1 / R1]` or `1–5` to Switch)
1. **`1:SPECTRUM` — Dual-Band Spectrum & Waterfall**:
   - **Top**: Live & Kalman-smoothed LoRa packet RSSI meter.
   - **Middle**: 12-channel Sub-GHz (`903.000 – 925.000 MHz`) real-time energy bars + 15-row color-graded rolling waterfall showing the active sweep frequency (`SCAN`) and home frequency (`HOME`).
   - **Bottom**: 2.4 GHz 802.11 Promiscuous Channel Occupancy (`Ch 1–13`) bar graph + latest intercepted Wi-Fi and LoRa packet summaries.
2. **`2:DEVICES` — Discovered Devices Radar Cards & Confirmation Dossier**:
   - **2×4 Small Card Grid**: Displays confirmed LoRa and Wi-Fi emitters with rank, device name/hash, protocol badge, proximity badge (`IMMEDIATE <1m` to `DISTANT`), log-distance meter estimate (`~m`), Kalman RSSI bar, crystal offset (`Ferr Hz`) or MAC/Channel, packet count, and age.
   - **Live Sorting (`[Y]`)**: Cycle between `PROXIMITY` (strongest RSSI first), `NAME`, or `TYPE`.
   - **Selectable Snapshot Refresh (`[X]`)**: Cycle between `LIVE`, `2 SEC`, `5 SEC`, `1 MIN`, `5 MIN`, and `10 MIN` while keeping the D-pad cursor locked onto your selected device UID across refreshes.
   - **TTL Garbage Collection (`[B]`)**: Automatic background eviction of transient Wi-Fi ghosts (`20s–75s` TTL) while protecting LoRa/Reticulum nodes (`10 min` TTL), plus one-button manual Wi-Fi ghost purge.
   - **Device Confirmation Dossier Pop-Up (`[A]`)**: Opens a full-screen modal for the selected card with an **18-sample live RSSI sparkline**, min/peak RSSI, packet cadence (`ms`), crystal frequency offset (`Hz`), RNS destination hashes / rotated MAC list, and hex/ASCII payload preview to confirm which physical device is which when two nodes are next to each other.
3. **`3:DISSECTOR` — Cursor-Locked Protocol Frame Dissector**:
   - Scrollable packet capture list (`D-Pad UP/DOWN` locks the cursor onto a specific frame without losing your place as new packets arrive) with a lower inspection pane decoding 802.11 headers or LoRa/Reticulum/Meshtastic fields and full `YYYY-MM-DD HH:MM:SS.mmm` timestamps.
4. **`4:TUNER` — Interactive D-Pad Radio & Wi-Fi Tuner**:
   - Live adjustment of LoRa Home Frequency (`150–960 MHz`), Spreading Factor (`SF7–SF12`), Bandwidth (`62.5–500 kHz`), Coding Rate (`4/5–4/8`), Sync Word (`0x12` RNS, `0x2B` Meshtastic, `0x34` LoRaWAN), Acquisition Mode (`SNIFF`, `SWEEP`, `SCAN`, `IDLE`), and 2.4 GHz Wi-Fi Promiscuous (`ON/OFF`, `AUTO-HOP` vs. `FIXED CH 1–13`).
5. **`5:CONSOLE` — Dated Serial Telemetry Log**:
   - Live dated serial stream (`RX<`, `TX>`, `STAT`, `LORA`, `WIFI`) with `START` / `SPACE` stream pause.

---

## 3. What Can Be Exported (Wireshark, Charles Proxy & JSON)

Pressing **`[X]` or `[Y]`** in `3:DISSECTOR` or `5:CONSOLE` on the R36MAX (or clicking the export buttons in the Web Console) exports all captured traffic and device dossiers with full timestamps (`YYYY-MM-DD HH:MM:SS.mmm` and ISO-8601 UTC):

1. **Wireshark Binary Capture (`.pcap`)**:
   - Standard libpcap binary file (`LINKTYPE_ETHERNET = 1`) with microsecond packet headers.
   - Encapsulates 802.11 Wi-Fi frames on UDP ports `2401–2413` (mapped to Channels 1–13 with source/BSSID MACs) and Sub-GHz LoRa packets on UDP ports `4242` (Reticulum), `4403` (Meshtastic), or `9150` (Raw LoRa) with full RF metadata tags (`freq`, `sf`, `bw`, `rssi`, `snr`, `ferr`) and raw payload bytes for direct filtering in Wireshark.
2. **Charles Proxy JSON Session (`.chlsj`)**:
   - Native Charles Proxy session file mapping each intercepted RF frame to an inspectable request/response transaction (`http://wifi-ch6.24ghz.stump/...` or `http://reticulum.subghz.stump/915.0MHz/SF8/...`) with custom `X-RF-*` telemetry headers (`X-RF-Date`, `X-RF-RSSI-dBm`, `X-RF-Crystal-Ferr-Hz`, `X-RF-OUI-Vendor`) and JSON/hex bodies.
3. **Full Structured Recon Snapshot (`.json`)**:
   - Self-contained JSON export containing `radio_status`, all `discovered_devices` (with Kalman RSSI history, crystal offsets, RNS hashes, and rotated LAA MACs), `captured_events`, and `serial_logs`.
4. **Continuous Live JSONL Stream (`.jsonl`)**:
   - Every incoming event is automatically appended in real time to `/roms/ports/stump-recon/exports/live_stream_<YYYY-MM-DD_HHMMSS>.jsonl` on the R36MAX SD card.

---

## 4. Quick Controls & Deployment Reference

| Control (R36MAX / Keyboard) | Action |
| :--- | :--- |
| **`L1 / R1`** (`TAB`) | Cycle across the 5 views (`SPECTRUM`, `DEVICES`, `DISSECTOR`, `TUNER`, `CONSOLE`) |
| **`D-Pad`** (`Arrow Keys`) | Navigate device cards (`DEVICES`), lock frame cursor (`DISSECTOR`), or tune radio (`TUNER`) |
| **`A`** (`Enter`) | Open/Close Device Confirmation Dossier Pop-up (`DEVICES`) or engage `SNIFF` mode |
| **`B`** (`Backspace`) | Close Pop-up / Purge stale Wi-Fi ghosts (`DEVICES`) or engage `SWEEP` mode |
| **`X`** (`X`) | Cycle Card Refresh Rate (`DEVICES`), Export `.pcap/.chlsj/.json` (`DISSECTOR`/`CONSOLE`), or `SCAN` |
| **`Y`** (`Y`) | Cycle Card Sort Mode (`DEVICES`), Export `.pcap/.chlsj/.json` (`DISSECTOR`/`CONSOLE`), or Toggle Sync |
| **`START`** (`Space`) | Pause / Resume live UI telemetry stream |
| **`SELECT`** (`Esc` / `Q`) | Close pop-up or exit cleanly to EmulationStation |