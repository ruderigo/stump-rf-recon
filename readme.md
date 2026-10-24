# STUMP Dual-Band Sub-GHz LoRa & 2.4 GHz 802.11 Monitor (`v0.3.8`)

A passive, receive-only (**Sub-GHz LoRa 902–928 MHz** + **2.4 GHz 802.11b/g/n**) packet dissector and transmitter-clustering monitor built for the **Heltec WiFi LoRa 32 V3** (`ESP32-S3 + Semtech SX1262`) paired with the **XiFan R36MAX** handheld (`720×720` display) or the **Web Serial Console**.

<img width="630" height="750" alt="demo1" src="https://github.com/user-attachments/assets/3fe26296-2f89-4d70-8dfc-f233b778c94d" />

---

## 1. What It Can Sniff (Receive-Only Capture Scope)

All radio operations are **strictly receive-only (Zero-TX)**. Neither the SX1262 Sub-GHz transceiver nor the ESP32-S3 2.4 GHz Wi-Fi radio ever transmits a frame.

### Sub-GHz LoRa & Mesh (`902–928 MHz ISM` · ESP32-S3 Core 1 + SX1262)
- **Reticulum Network Stack (RNS / RNode `0x12`)**:
  - Captures `ANNOUNCE`, `DATA`, `LINK_REQ`, and `PROOF` frames on the configured home frequency and spreading factor.
  - Extracts 16-byte Destination Hashes, hop counts, header types (`HEADER_1` / `HEADER_2`), and IFAC flags.
  - **RNS Client Fingerprinting (`ANNOUNCE` `name_hash` + `app_data`)**: Matches the 10-byte SHA-256 aspect `name_hash` inside unencrypted `ANNOUNCE` payloads (offset `hdr + 64..74`) to identify **Sideband / LXMF** (`6ec60bc318e2c0f0d908`), **LXMF Propagation** (`e03a09b77ac21b22258e`), **NomadNet BBS** (`213e6311bcec54ab4fde`), **Reticulum MeshChat** (`c4a059d454529304d693`), **RNS Transport Probe/Router**, and **RNS Voice Call** (`db370f50a7f92e5e51f6`), and unpacks MessagePack/UTF-8 peer display names from `app_data`.
- **MeshCore (`0x12`)**:
  - Disambiguates MeshCore packets sharing sync word `0x12` by header version/type bits (`ADVERT`, `TXT_MSG`, `GRP_TXT`, `REQ`, `RESPONSE`, `PATH`).
  - Extracts 32-byte Ed25519 public key prefixes, hop counts, node roles (**Chat Node**, **Repeater**, **Room Server**, **Sensor**), optional signed GPS coordinates (`lat, lon`), and UTF-8 node names from `ADVERT` `appdata`.
- **Meshtastic (`0x2B`)**:
  - Extracts little-endian sender Node ID (`!from`), destination Node ID (`!to`), hop limit, ACK flags, and channel hash.
- **Sub-GHz RSSI Energy Sweeping (`903–925 MHz`)**:
  - Uses Time-Division Multiplexing (TDM) on Core 1 to sample instantaneous RSSI (`getRssiInst()`) across 12 frequencies (`903–925 MHz` in 2 MHz steps) and flags narrowband carrier bursts (`>= 11.5 dB` above noise median or `>= -78 dBm`).

### 2.4 GHz 802.11b/g/n Promiscuous Header Sniffer (`Channels 1–13` · ESP32-S3 Core 0)
- **802.11 Management & Data Frame Headers**:
  - Captures `PROBE_REQ`, `BEACON`, `ASSOC_REQ`, `DEAUTH`, and high-RSSI `DATA` headers across 2.4 GHz Channels 1–13.
- **SSID & IEEE OUI Prefix Lookup**:
  - Extracts cleartext SSIDs from 802.11 Information Element `Tag 0x00` and checks the 3-byte MAC prefix against an onboard OUI table, flagging the Locally Administered Address bit (`mac[0] & 0x02`) for randomized client MACs.

---

## 2. What It Renders & How

On the **R36MAX** (`stump_gui.py`), graphics render at `720×720` via SDL2 `KMSDRM` with a `/dev/fb0` `mmap` blitter fallback. Per-device RSSI readings are smoothed with a **1D Kalman filter** (`Q = 0.15`, `R = 3.0`).

### 5 Tactical Views (`[L1 / R1]` or `1–5` to Switch)
1. **`1:SPECTRUM` — Dual-Band Spectrum & Waterfall**:
   - **Top**: Raw & Kalman-smoothed LoRa packet RSSI bar.
   - **Middle**: 12-bin Sub-GHz (`903–925 MHz`) energy histogram + 15-row rolling waterfall indicating the active TDM sweep bin (`SCAN`) and home demodulation frequency (`HOME`).
   - **Bottom**: 2.4 GHz 802.11 Channel Occupancy (`Ch 1–13`) bar graph + latest Wi-Fi and LoRa packet summaries.
2. **`2:DEVICES` — Clustered Emitter Cards & Inspection Dossier**:
   - **2×4 Small Card Grid**: Displays active LoRa and 2.4 GHz Wi-Fi emitter clusters with rank, node name/hash, protocol/client badge (`Sideband / LXMF`, `NomadNet BBS`, `Repeater`, `Meshtastic`, etc.), relative signal tier (`IMMEDIATE` to `DISTANT`), Kalman RSSI, mean crystal frequency error (`Ferr Hz`) or MAC/Channel, packet count, and age.
   - **Sorting (`[Y]`) & Snapshot Refresh (`[X]`)**: Sort by `PROXIMITY` (Kalman RSSI), `NAME`, or `TYPE`; lock snapshot refresh to `LIVE`, `2 SEC`, `5 SEC`, `1 MIN`, `5 MIN`, or `10 MIN` while keeping the cursor locked to the selected `uid`.
   - **TTL Garbage Collection (`[B]`)**: Evicts unconfirmed 1-packet Wi-Fi frames after `20s`, LAA probe clusters after `45s`, confirmed Wi-Fi stations after `75s`, and LoRa nodes after `600s`.
   - **Device Dossier Pop-Up (`[A]`)**: Full-screen modal displaying an **18-sample live RSSI sparkline** (for near-field antenna proximity checks), min/peak RSSI, average packet interval (`ms`), mean SX1262 crystal frequency offset (`Hz`), observed RNS destination hashes / MeshCore role & GPS / rotated LAA MACs, and hex/ASCII payload preview.
3. **`3:DISSECTOR` — Cursor-Locked Frame Dissector**:
   - Scrollable 40-frame ring buffer (`D-Pad UP/DOWN` locks the cursor onto a specific frame while new traffic arrives) with lower header breakdown for 802.11, Reticulum, MeshCore, and Meshtastic frames.
4. **`4:TUNER` — D-Pad Radio & Wi-Fi Tuner**:
   - Configures LoRa Home Frequency (`150–960 MHz`), `SF7–SF12`, `BW 62.5–500 kHz`, `CR 4/5–4/8`, Sync Word (`0x12`, `0x2B`, `0x34`), Mode (`SNIFF`, `SWEEP`, `SCAN`, `IDLE`), and 2.4 GHz channel hopping.
5. **`5:CONSOLE` — Dated Serial Telemetry Log**:
   - Raw JSON serial stream monitor (`RX<`, `TX>`, `STAT`, `LORA`, `WIFI`) with `START` / `SPACE` pause.

---

## 3. What Can Be Exported (Wireshark, Charles Proxy & JSON)

Pressing **`[X]` or `[Y]`** in `3:DISSECTOR` or `5:CONSOLE` on the R36MAX (or clicking the export buttons in the Web Console) writes timestamped captures (`YYYY-MM-DD HH:MM:SS.mmm` local and ISO-8601 UTC):

1. **Synthetic Wireshark Telemetry PCAP (`.pcap`)**:
   - Libpcap file (`LINKTYPE_ETHERNET = 1`) wrapping parsed RF metadata + raw LoRa payload bytes inside synthetic Ethernet/IPv4/UDP packets (`UDP 2401–2413` for 2.4 GHz Ch 1–13 metadata; `UDP 4242` for Reticulum/MeshCore; `UDP 4403` for Meshtastic; `UDP 9150` for Raw LoRa). *Note: This is a reconstructed metadata wrapper for Wireshark filtering, not a raw 802.11 Radiotap bit-level capture.*
2. **Charles Proxy JSON Session (`.chlsj`)**:
   - Maps each captured LoRa or 802.11 event into a Charles Proxy transaction (`X-RF-Date`, `X-RF-Protocol`, `X-RF-RSSI-dBm`, `X-RF-SNR-dB`, `X-RF-Crystal-Ferr-Hz`, `X-RF-OUI-Vendor`) for timeline filtering and diffing.
3. **Structured Session Snapshot (`.json`) & Live Stream (`.jsonl`)**:
   - Full JSON dump of `radio_status`, `discovered_devices`, `captured_events`, and `serial_logs`, plus continuous append-only logging to `/roms/ports/stump-recon/exports/live_stream_<timestamp>.jsonl`.

---

## 4. Installation & Flashing Guide (R36MAX + Heltec V3)

### Step 1: Download the Packages
From the **R36MAX Handheld HUD** tab in the Web Console:
- Click **`Download stump-recon-r36max.zip`** (contains `STUMP-Recon.sh`, `stump-recon/stump_gui.py`, `stump-recon/stump_cli.py`, `stump-recon/validate_recon.py`, and `stump-recon/README.md`).
- Click **`Download heltec-v3-firmware.zip`** if you need to flash a Heltec V3 board (contains `platformio.ini` and `src/main.cpp`).

### Step 2: Deploy to the XiFan R36MAX

#### Option A: Over Wi-Fi (`scp` + `ssh`)
Run this single block in your Mac/Linux Terminal (it automatically selects the newest `stump-recon-r36max*.zip` in `~/Downloads`, even if macOS appended `(1)` or `(2)`):

```bash
LATEST_R36_ZIP="$(ls -t ~/Downloads/stump-recon-r36max*.zip | head -n 1)" && \
echo "Uploading: $LATEST_R36_ZIP" && \
scp "$LATEST_R36_ZIP" ark@r36max.local:/roms/ports/stump-recon-r36max.zip && \
ssh ark@r36max.local "cd /roms/ports && rm -rf STUMP* stump-recon && unzip -o stump-recon-r36max.zip && chmod +x STUMP-Recon.sh stump-recon/*.py && python3 stump-recon/validate_recon.py stump-recon/stump_gui.py"
```
*(Default ArkOS SSH password is `ark`. Replace `r36max.local` with the handheld's IP address if mDNS is disabled).*

#### Option B: Direct MicroSD Card Copy (`EASYROMS`)
If the R36MAX MicroSD card is mounted on your Mac at `/Volumes/EASYROMS`:

```bash
LATEST_R36_ZIP="$(ls -t ~/Downloads/stump-recon-r36max*.zip | head -n 1)" && \
cd /Volumes/EASYROMS/ports && \
rm -rf STUMP* stump-recon && \
unzip -o "$LATEST_R36_ZIP" && \
dot_clean -m /Volumes/EASYROMS/ports
```

### Step 3: Flash the Heltec WiFi LoRa 32 V3 Firmware (`v0.3.7-DUALBAND-TDM`)
Plug the Heltec V3 into your computer via USB-C and run:

```bash
export PATH="$HOME/.platformio/penv/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" && \
rm -rf ~/Downloads/heltec-v3-latest && \
unzip -o "$(ls -t ~/Downloads/heltec-v3-firmware*.zip | head -n 1)" -d ~/Downloads/heltec-v3-latest && \
cd ~/Downloads/heltec-v3-latest && \
pio run -t upload
```
*(If PlatformIO CLI is not installed yet, run `brew install platformio` on macOS first. If a board is stuck in RNode/deep-sleep mode and not enumerating, hold **`PRG / BOOT`** while plugging in USB-C, tap **`RST`**, then run `pio run -t upload`).*

### Step 4: Launch & Controls Reference
Plug the flashed Heltec V3 into the R36MAX **`OTG`** USB-C port and launch **`Ports` $\rightarrow$ `STUMP-Recon`**.

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

---

## 5. Hardware & Architectural Ceiling (What This Tool Does NOT Do)

To avoid overstating what an ESP32-S3 + SX1262 can physically deliver:

1. **No 5 GHz or 6 GHz Wi-Fi (`2.4 GHz 802.11b/g/n` Header-Only)**:
   - The ESP32-S3 radio only covers `2.4 GHz` Channels 1–13. It is completely blind to `5 GHz` (`802.11a/ac/ax`) and `6 GHz` (`Wi-Fi 6E/7`) traffic. Furthermore, the firmware's promiscuous callback extracts 802.11 MAC headers and SSID `IE 0x00` tags; it does not capture full MPDU bodies, decrypt WPA2/WPA3 data, or export `LINKTYPE_IEEE802_11_RADIOTAP` frames.
2. **Single-Channel LoRa Demodulation & TDM Blind Spots**:
   - A single Semtech SX1262 cannot decode multiple spreading factors (`SF7–SF12`) or multiple frequencies at the same microsecond. Packet decoding only occurs on the single configured home `(Freq, SF, BW, Sync)` while in `SNIFF` or `SWEEP` mode. During the ~18 ms TDM window when Core 1 steps to an off-home frequency to read `getRssiInst()`, preambles arriving on the home frequency can be missed.
3. **RSSI Path-Loss Index (`~Xm`) Is Not True Ranging**:
   - Single-antenna RSSI without Time-of-Flight (ToF) or multi-antenna Angle-of-Arrival (AoA) cannot measure true metric distance. A remote `+22 dBm` LoRa repeater at 300 m with line-of-sight can produce a stronger RSSI than a `+2 dBm` node 5 m away behind a wall. Treat `~Xm` solely as a relative log-distance sorting index; physical board identification requires **near-field differential RSSI** (holding the antenna `5–10 cm` from a suspect device to observe a jump above `-45 dBm`).
4. **Encrypted Reticulum `DATA` Frames Are Opaque**:
   - Reticulum's `ANNOUNCE` frames expose the 10-byte aspect `name_hash` and optional `app_data` in cleartext, allowing client fingerprinting (`Sideband`, `NomadNet`, `MeshChat`). However, subsequent `DATA`, `LINK_REQ`, and `PROOF` frames use ephemeral Link IDs or encrypted payloads with no cleartext sender address or `name_hash`. Associating `DATA` frames with a physical transmitter relies entirely on physical-layer `(Ferr, RSSI)` clustering.

---

## 6. Validation Methodology: Known-Transmitter & LAA Clustering

### How `Ferr` (Crystal Frequency Error) & Identity Clustering Works
In `stump_gui.py` (`match_physical_lora_device`) and `DeviceRadarPanel.tsx` (`buildLiveDeviceMap`), incoming LoRa packets are grouped into physical transmitter cards using a two-stage gate:

1. **Stage 1 — Cryptographic / Hardware Identity Gate (Hard Separation)**:
   - Packets with different verified Meshtastic `!from` IDs, different MeshCore 32-byte Ed25519 `pubkey` prefixes, different `app_data` `peer_name` strings, or two Reticulum `ANNOUNCE` packets advertising the **same** 10-byte `name_hash` aspect (e.g., both `lxmf.delivery`) with **different** 16-byte `dest_hash` values are **never** merged, even if their crystal offsets and RSSI are identical.
2. **Stage 2 — Physical-Layer `(Ferr, RSSI)` Gate (Multi-Payload Consolidation)**:
   - When a packet does not conflict on Stage 1 (e.g., the same node announcing a second RNS aspect like `nomadnetwork.node`, or transmitting unannounced `DATA` / `LINK_REQ` frames), it merges into an existing transmitter on the same `(SF, Sync)` if and only if:
     - **`|Ferr_pkt - Ferr_avg| <= 250 Hz`** (`<= 320 Hz` at low `SNR < -3 dB`)
     - **`|RSSI_pkt - RSSI_kalman| <= 10.0 dB`**

### Running the Ground-Truth Bench Validation Suite (`validate_recon.py`)
An automated validation suite (`stump-recon/validate_recon.py`) parses and executes `stump_gui.py`'s actual `dissect_lora()`, `match_physical_lora_device()`, and `update_device_registry()` functions against 5 controlled transmitter scenarios:

```bash
python3 /roms/ports/stump-recon/validate_recon.py /roms/ports/stump-recon/stump_gui.py
```

| Test ID | Ground-Truth Scenario | Input Stream | Expected Output |
| :--- | :--- | :--- | :--- |
| **Test 1** | **Same Node, 3 Different Payloads** | Node A (`Ferr ≈ -310 Hz`, `-54 dBm`) sends `Sideband ANNOUNCE` (`9a4c...`), `NomadNet ANNOUNCE` (`7788...`), and encrypted `DATA` (`aabb...`) | **1 Cluster** (`pkts=3`, `ferr_avg=-310.2 Hz`, all hashes attached to `rns_dests`) |
| **Test 2** | **Two Side-by-Side Nodes, Same Payload (`DATA`)** | Node A (`Ferr = -310 Hz`, `-54 dBm`) and Node B (`Ferr = +145 Hz`, `-54 dBm`) both send raw `DATA` frames at identical RSSI | **2 Distinct Clusters** (separated by `\|ΔFerr\| = 455 Hz > 250 Hz`) |
| **Test 3** | **Two Side-by-Side Nodes with Overlapping Crystals (`\|ΔFerr\| = 100 Hz`), Same Aspect** | Node A (`-310 Hz`) and Node C (`-210 Hz`) both send `Sideband / LXMF` `ANNOUNCE` frames with different `dest_hash` | **2 Distinct Clusters** (Stage 1 blocks merge because a single RNS identity cannot have two `lxmf.delivery` hashes) |
| **Test 4** | **MeshCore `0x12` vs. Reticulum `0x12` Coexistence** | MeshCore `ADVERT` (`0x11`, Repeater + GPS) and Reticulum `ANNOUNCE` (`0x01`, Sideband) at same `Ferr` and `RSSI` | **2 Distinct Clusters** (`MC "MC-Repeater-NW"` vs `RNS "Operator-1"`) |
| **Test 5** | **Known False-Merge Boundary** | Two unannounced `DATA`-only transmitters at `-70 dBm` whose crystals lie within `\|ΔFerr\| = 90 Hz <= 250 Hz` | **Merges into 1 Cluster** until at least one node transmits an `ANNOUNCE` / `ADVERT` |

### Documented Failure Modes & False-Merge Boundaries
- **TCXO Collision on Unannounced `DATA`-Only Nodes**: Temperature-Compensated Crystal Oscillators (TCXOs) on two Heltec V3 boards from the same manufacturing reel can occasionally sit within `< 200 Hz` (`< 0.22 ppm` at 915 MHz) of each other. If both boards sit at the same distance (`|ΔRSSI| <= 10 dB`) and *only* transmit encrypted `DATA` frames (never an `ANNOUNCE` or `ADVERT`), Stage 2 will merge them into a single physical cluster. Separating them requires moving the receiver antenna close (`5–10 cm`) to one board so `|ΔRSSI| > 10 dB`, or capturing an `ANNOUNCE` frame.
- **Thermal Crystal Drift During Continuous TX**: On cheaper non-TCXO (standard XTAL) LoRa boards, PA self-heating during a long burst can drift `Ferr` by `> 300 Hz` over 60 seconds, which can split an unannounced raw LoRa transmitter into two `LORA:PHY` buckets.
- **Wi-Fi LAA Wildcard Probe Collisions**: Because the ESP32 UART JSON stream only reports `(mac, ftype, ssid, ch, rssi)` and does not forward raw 802.11 Information Element tag bitmasks (HT/VHT capabilities, Supported Rates, Vendor tags), wildcard (`<Wildcard>`) LAA `PROBE_REQ` frames are grouped purely by a `±10 dB` Kalman RSSI window. Two different phones probing with wildcard SSIDs at the same distance will merge into the same `WIFI:LAA:PROX` card; conversely, walking away from a phone while it rotates MACs will spawn a new proximity bucket until the `45s` LAA TTL expires.
