#!/usr/bin/env python3
"""
stump_gui.py — STUMP Dual-Band (LoRa + 2.4GHz Wi-Fi) Tactical HUD for XiFan R36MAX
Target: Rockchip RK3326 · 4" 720x720 Square Display · KMSDRM + /dev/fb0 Blitter
Includes:
  - View 1: Dual-Band Spectrum & Waterfall (902-928 MHz LoRa + 2.4 GHz Wi-Fi Ch 1-13)
  - View 2: Discovered Devices Card Grid (Sort: Proximity/Name/Type | Refresh: Live/2s/5s/1m/5m/10m + Detail Popup)
  - View 3: Reticulum (0x12), Meshtastic (0x2B), & 802.11 Frame Dissector (Cursor-Locked)
  - View 4: D-Pad Radio & Wi-Fi Tuner
  - View 5: Filtered Serial Telemetry Console
"""

import sys, os, re, time, json, math, mmap, struct, random, threading, datetime, pygame

try:
    import serial
except ImportError:
    serial = None

# --- 1. Hardware Display + /dev/fb0 Blitter Initialization ---
sc = fb = cs = None
bpp = 32
stw = 2880
fbh = 720

for tty_dev in ("/dev/tty1", "/dev/tty0"):
    try:
        with open(tty_dev, "w") as tf:
            tf.write("\033[?25l")
    except Exception:
        pass

for drv in ("kmsdrm", "fbcon"):
    try:
        os.environ["SDL_VIDEODRIVER"] = drv
        pygame.display.init()
        if pygame.display.get_driver() in ("offscreen", "dummy"):
            pygame.display.quit()
            continue
        sc = pygame.display.set_mode((720, 720))
        break
    except Exception:
        try:
            pygame.display.quit()
        except Exception:
            pass

if not sc or pygame.display.get_driver() == "fbcon":
    if not sc:
        os.environ["SDL_VIDEODRIVER"] = "offscreen"
        pygame.display.init()
        sc = pygame.display.set_mode((720, 720))
    try:
        def read_sys(name, default):
            p = f"/sys/class/graphics/fb0/{name}"
            return open(p).read().strip() if os.path.exists(p) else default
        bpp = int(read_sys("bits_per_pixel", "32"))
        w, fbh = map(int, read_sys("virtual_size", "720,720").split(","))
        stw = int(read_sys("stride", str(w * (bpp // 8))))
        fb = mmap.mmap(os.open("/dev/fb0", os.O_RDWR), stw * fbh, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        cs = pygame.Surface(
            (720, 720), 0,
            16 if bpp == 16 else 32,
            (0xF800, 0x07E0, 0x001F, 0) if bpp == 16 else (0x00FF0000, 0x0000FF00, 0x000000FF, 0)
        )
    except Exception:
        fb = None

try:
    pygame.mouse.set_visible(False)
except Exception:
    pass

pygame.font.init()
pygame.joystick.init()
jy = pygame.joystick.Joystick(0) if pygame.joystick.get_count() > 0 else None
if jy:
    jy.init()

fh, fs, fm, fx, fxs = [pygame.font.Font(None, z) for z in (28, 22, 19, 17, 15)]

# --- 2. Color Palette & Drawing Helpers ---
BG = (7, 9, 14)
PN = (17, 24, 39)
CD = (22, 32, 50)
BD = (30, 41, 59)
TX = (241, 245, 249)
MU = (148, 163, 184)
CY = (6, 182, 212)
AM = (245, 158, 11)
VI = (168, 85, 247)
GR = (16, 185, 129)
RO = (244, 63, 94)

def bx(x, y, w, h, c=PN, b=BD, bw=1):
    pygame.draw.rect(sc, c, (x, y, w, h))
    if b:
        pygame.draw.rect(sc, b, (x, y, w, h), bw)

def tx(f, s, x, y, c=TX, max_ch=84):
    sc.blit(f.render(str(s)[:max_ch], True, c), (x, y))

OUI_MAP = {
    "24:0a:c4": "Espressif/Heltec", "30:ae:a4": "Espressif/Heltec", "dc:da:0c": "Espressif/Heltec",
    "e8:6b:ea": "Espressif/Heltec", "48:27:e2": "Espressif/Heltec", "f4:12:fa": "Espressif/Heltec",
    "84:f7:03": "Espressif/Heltec", "ec:da:3b": "Espressif/Heltec", "a0:b7:65": "Espressif/Heltec",
    "70:04:1d": "Espressif/Heltec", "34:85:18": "Espressif/Heltec", "40:4c:ca": "Espressif/Heltec",
    "00:17:f2": "Apple Inc.", "a4:83:e7": "Apple Inc.", "3c:22:fb": "Apple Inc.",
    "dc:a6:32": "Raspberry Pi", "b8:27:eb": "Raspberry Pi",
    "14:8f:30": "Ralink Tech", "00:e0:4c": "Realtek", "f8:1a:67": "TP-Link"
}

def resolve_oui(mac):
    m = (mac or "").lower()
    if len(m) >= 8:
        if m[:8] in OUI_MAP:
            return OUI_MAP[m[:8]]
        try:
            if int(m[:2], 16) & 0x02:
                return "Randomized (LAA)"
        except Exception:
            pass
    return "Standard IEEE OUI"

def estimate_distance_m(rssi_dbm, band="LORA"):
    # Log-distance path loss approximation for proximity sorting & side-by-side STUMP confirmation
    tx_ref = -38.0 if band == "LORA" else -42.0
    n = 2.7
    dist = math.pow(10.0, (tx_ref - float(rssi_dbm)) / (10.0 * n))
    return round(max(0.1, min(999.0, dist)), 1)

def proximity_label(rssi_dbm):
    if rssi_dbm >= -52:
        return ("IMMEDIATE <1m", RO)
    elif rssi_dbm >= -68:
        return ("VERY CLOSE 1-4m", AM)
    elif rssi_dbm >= -82:
        return ("NEARBY 4-15m", GR)
    elif rssi_dbm >= -98:
        return ("MID RANGE", CY)
    return ("DISTANT", MU)

def dissect_lora(pkt):
    pl = str(pkt.get("payload", "") or "")
    sync = int(pkt.get("sync", 18))
    crc_ok = bool(pkt.get("crc_ok", True))
    freq_v = float(pkt.get("freq", 915.0))
    sf_v = int(pkt.get("sf", 8))
    ferr_v = float(pkt.get("ferr", 0.0))
    try:
        raw = bytes.fromhex(pl) if len(pl) >= 2 and len(pl) % 2 == 0 else b""
    except Exception:
        raw = b""

    # Sync word 0x12 (18) is Reticulum (RNS) / RNode standard
    if sync == 18 or (sync != 43 and len(raw) >= 18):
        if len(raw) >= 2:
            flags = raw[0]
            ifac = bool(flags & 0x80)
            hdr_type = (flags >> 6) & 1
            # RNS PacketType is bits 1..0 (0=DATA, 1=ANNOUNCE, 2=LINK_REQ, 3=PROOF); fallback to bits 5..4 if simulator frame
            pt_bits = (flags & 0x03) if (flags & 0x03) != 0 else ((flags >> 4) & 0x03)
            ptype = ["DATA", "ANNOUNCE", "LINK_REQ", "PROOF"][pt_bits]
            hops = int(raw[1]) & 0x1F if len(raw) > 1 else 0
            # Extract 16-byte Destination Hash (offset 2 for HEADER_1, offset 18 for HEADER_2 transport)
            dest_off = 18 if (hdr_type == 1 and len(raw) >= 34) else (5 if (ifac and len(raw) >= 21) else 2)
            if len(raw) >= dest_off + 16:
                dest = raw[dest_off:dest_off + 16].hex()
            elif len(raw) > 2:
                dest = raw[2:18].hex()
            else:
                dest = pl[:24] if pl else f"{freq_v:.1f}M"
            verified_id = bool(crc_ok and len(raw) >= 18 and (not ifac))
        else:
            ptype = "RNS_FRAME"
            hops = 0
            ifac = False
            dest = pl[:24] if pl else f"sf{sf_v}_{int(abs(ferr_v)):03d}"
            verified_id = False
        return {
            "proto": "Reticulum",
            "type": ptype,
            "hops": hops,
            "dest": dest,
            "ifac": ifac,
            "verified_id": verified_id,
            "uid": f"RNS:{dest[:12]}"
        }
    elif sync == 43 and len(raw) >= 8:
        flags = raw[12] if len(raw) > 12 else 0
        hops = flags & 7
        ack = bool(flags & 8)
        ch = f"0x{raw[13]:02x}" if len(raw) > 13 else "0x00"
        to_id = raw[0:4][::-1].hex()
        from_id = raw[4:8][::-1].hex()
        valid_mesh = bool(from_id not in ("00000000", "ffffffff"))
        return {
            "proto": "Meshtastic",
            "type": "MESH_PKT",
            "to": to_id,
            "from": from_id,
            "hops": hops,
            "ack": ack,
            "ch": ch,
            "verified_id": valid_mesh,
            "uid": f"MESH:!{from_id}" if valid_mesh else f"MESH:{freq_v:.1f}M:SF{sf_v}"
        }
    return {
        "proto": "Raw LoRa",
        "type": "CUSTOM",
        "hops": 0,
        "dest": pl[:24] if pl else f"{freq_v:.1f}MHz",
        "ifac": False,
        "verified_id": False,
        "uid": f"LORA:{freq_v:.1f}M:SF{sf_v}"
    }

def hex_to_ascii(hex_s):
    try:
        raw = bytes.fromhex(hex_s)
        return "".join(chr(b) if 32 <= b <= 126 else "." for b in raw)[:48]
    except Exception:
        return ""

# --- 3. Telemetry State, Device Registry & Snapshot Refresh Controls ---
st = {
    "freq": 915.0,
    "live_freq": 915.0,
    "bw": 125.0,
    "sf": 8,
    "cr": 5,
    "sync": 18,
    "mode": "sniff",
    "wifi": True,
    "wifich": 0
}
lp = []
wf = []
ev = []
wr = []
SCAN_FREQS = [903, 905, 907, 909, 911, 913, 915, 917, 919, 921, 923, 925]
ls = {f: -116.0 for f in SCAN_FREQS}
ws = {c: -94.0 for c in range(1, 14)}
sweep_step_idx = 0
last_tdm_action_ts = 0.0
tdm_scan_burst_active = False
tdm_silent_until = 0.0
last_nonhome_scan_ts = 0.0

# Device Radar Registry: uid -> dict of comprehensive device telemetry
devices_lock = threading.Lock()
devices_db = {}          # Live background registry updated on every packet
displayed_devices = []   # Confirmed active snapshot list rendered as cards
gc_purged_count = 0      # Total stale/ghost records evicted by TTL Garbage Collector
last_gc_ts = 0.0

# TTL Eviction Windows (seconds of inactivity before automatic removal)
TTL_CANDIDATE_WIFI_SEC = 20.0   # 1-packet weak Wi-Fi frames evicted after 20s if not seen again
TTL_LAA_WIFI_SEC = 45.0         # Randomized LAA client MAC clusters evicted after 45s idle
TTL_CONFIRMED_WIFI_SEC = 75.0   # Confirmed Wi-Fi APs/Clients evicted after 75s idle
TTL_LORA_SEC = 600.0            # Reticulum / LoRa nodes retained for 10 min across announce intervals

SORT_MODES = ["PROXIMITY", "NAME", "TYPE"]
sort_idx = 0

REFRESH_PRESETS = [
    ("LIVE", 0.0),
    ("2 SEC", 2.0),
    ("5 SEC", 5.0),
    ("1 MIN", 60.0),
    ("5 MIN", 300.0),
    ("10 MIN", 600.0),
]
refresh_idx = 1  # Default to 2 SEC so cards are responsive yet stable under cursor
last_snapshot_ts = 0.0
force_snapshot = True

# View & Cursor State
# Views: 0=SPECTRUM, 1=DEVICES (Cards + Popup), 2=DISSECTOR, 3=TUNER, 4=CONSOLE
vi = 1  # Start on DEVICES view or switch anytime with L1/R1
si = 0  # Dissector cursor
tr = 0  # Tuner row cursor
dev_cursor_idx = 0       # Selected card index in displayed_devices
dev_selected_uid = None  # Locked UID so card cursor follows the same device across refreshes!
dev_popup_open = False   # When True, shows the full-screen Device Confirmation Modal

sl = [f"{time.strftime('%Y-%m-%d %H:%M:%S')} SYS Ready. [L1/R1]:Views [A]:Inspect [Y in Console]:Export PCAP/Charles/JSON"]
sr = sp = None
kx = -98.0
kp = 2.0
ui_paused = False
seen_beacons = {}
last_wifi_ui_ts = 0.0
export_banner_msg = ""
export_banner_until = 0.0

EXPORT_DIR = "/roms/ports/stump-recon/exports"
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs("/roms/stump", exist_ok=True)
try:
    jsonl_f = open(f"{EXPORT_DIR}/live_stream_{time.strftime('%Y-%m-%d_%H%M%S')}.jsonl", "a")
except Exception:
    jsonl_f = None

def fmt_dt(ts=None):
    dt_obj = datetime.datetime.fromtimestamp(ts if ts is not None else time.time())
    return dt_obj.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def fmt_iso(ts=None):
    dt_obj = datetime.datetime.utcfromtimestamp(ts if ts is not None else time.time())
    return dt_obj.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def ad_log(msg):
    if not ui_paused:
        sl.insert(0, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")
        del sl[25:]

def build_eth_udp_frame(src_mac, dst_mac, src_ip, dst_ip, port, payload_bytes):
    udp_len = 8 + len(payload_bytes)
    ip_len = 20 + udp_len
    eth = bytes(dst_mac[:6]) + bytes(src_mac[:6]) + struct.pack("!H", 0x0800)
    ip_hdr = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0x00, ip_len, 0x1234, 0x4000, 64, 17, 0,
        bytes(src_ip[:4]), bytes(dst_ip[:4])
    )
    # IPv4 header checksum
    s = 0
    for i in range(0, 20, 2):
        s += (ip_hdr[i] << 8) + ip_hdr[i+1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    csum = (~s) & 0xFFFF
    ip_hdr = ip_hdr[:10] + struct.pack("!H", csum) + ip_hdr[12:]
    udp_hdr = struct.pack("!HHHH", port, port, udp_len, 0)
    return eth + ip_hdr + udp_hdr + payload_bytes

def parse_mac_bytes(mac_s):
    try:
        parts = [int(x, 16) for x in (mac_s or "").split(":")]
        if len(parts) == 6:
            return parts
    except Exception:
        pass
    return [0x02, 0x53, 0x54, 0x55, 0x4D, 0x50]

def export_all_formats():
    global export_banner_msg, export_banner_until
    slug = time.strftime("%Y-%m-%d_%H%M%S")
    base_path = os.path.join(EXPORT_DIR, f"stump_{slug}")
    try:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        events_snap = list(reversed(ev))
        with devices_lock:
            devs_snap = [dict(v) for v in devices_db.values() if v.get("confirmed", True)]

        # 1. Write Wireshark Binary Libpcap (.pcap)
        pcap_path = base_path + ".pcap"
        with open(pcap_path, "wb") as pf:
            # Global PCAP Header (little-endian, LINKTYPE_ETHERNET=1)
            pf.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
            for item in events_snap:
                ts_ep = float(item.get("ts_epoch", time.time()))
                ts_sec = int(ts_ep)
                ts_usec = int((ts_ep - ts_sec) * 1_000_000)
                dt_str = item.get("dt", fmt_dt(ts_ep))
                if item.get("k") == "W":
                    src_m = parse_mac_bytes(item.get("mac", ""))
                    dst_m = parse_mac_bytes(item.get("bssid", "ff:ff:ff:ff:ff:ff"))
                    ch = int(item.get("ch", 6))
                    port = 2400 + ch
                    meta = (
                        f"[STUMP-WIFI80211] date=\"{dt_str}\" ch={ch} ftype=\"{item.get('ftype')}\" "
                        f"ssid=\"{item.get('ssid','')}\" mac={item.get('mac')} bssid={item.get('bssid')} "
                        f"oui=\"{item.get('oui','')}\" laa={item.get('laa')} rssi={item.get('rssi')}dBm\n"
                    ).encode("utf-8", "ignore")
                    frm = build_eth_udp_frame(src_m, dst_m, [192, 168, ch & 0xFF, src_m[5]], [224, 0, 0, 251], port, meta)
                else:
                    ds = item.get("ds", {})
                    pl_hex = str(item.get("payload", ""))
                    try:
                        raw_pl = bytes.fromhex(pl_hex)
                    except Exception:
                        raw_pl = b""
                    sync_b = int(item.get("sync", 18)) & 0xFF
                    src_m = [0x02, 0x52, sync_b, raw_pl[0] if len(raw_pl) > 0 else 1, raw_pl[1] if len(raw_pl) > 1 else 2, raw_pl[2] if len(raw_pl) > 2 else 3]
                    dst_m = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]
                    proto_s = ds.get("proto", "Raw LoRa")
                    port = 4242 if proto_s == "Reticulum" else (4403 if proto_s == "Meshtastic" else 9150)
                    meta = (
                        f"[STUMP-LORA] date=\"{dt_str}\" freq={item.get('freq')}MHz sf={item.get('sf')} "
                        f"bw={item.get('bw')}kHz sync=0x{sync_b:02X} rssi={item.get('rssi')}dBm "
                        f"kalman={item.get('kal')}dBm snr={item.get('snr')}dB ferr={item.get('ferr')}Hz proto=\"{proto_s}\"\n"
                    ).encode("utf-8", "ignore") + raw_pl
                    frm = build_eth_udp_frame(src_m, dst_m, [10, 91, 5, int(item.get("sf", 8)) & 0xFF], [239, 91, 5, port & 0xFF], port, meta)
                pf.write(struct.pack("<IIII", ts_sec, ts_usec, len(frm), len(frm)))
                pf.write(frm)

        # 2. Write Charles Proxy Session (.chlsj)
        chlsj_path = base_path + ".chlsj"
        charles_list = []
        for item in events_snap:
            ts_ep = float(item.get("ts_epoch", time.time()))
            iso_s = fmt_iso(ts_ep)
            dt_str = item.get("dt", fmt_dt(ts_ep))
            if item.get("k") == "W":
                ch = int(item.get("ch", 6))
                body_txt = json.dumps(item, indent=2)
                charles_list.append({
                    "status": "COMPLETE",
                    "method": f"WIFI_{item.get('ftype','MGMT')}",
                    "protocolVersion": "802.11/2.4GHz",
                    "scheme": "http",
                    "host": f"wifi-ch{ch}.24ghz.stump",
                    "port": 2400 + ch,
                    "actualPort": 2400 + ch,
                    "path": f"/mac/{item.get('mac')}/{item.get('ssid') or 'broadcast'}",
                    "query": f"ch={ch}&rssi={item.get('rssi')}",
                    "tunnel": False,
                    "times": {"start": iso_s, "requestBegin": iso_s, "requestComplete": iso_s, "responseBegin": iso_s, "end": iso_s},
                    "durations": {"total": 1, "dns": 0, "connect": 0, "ssl": 0, "request": 0, "response": 1, "latency": 1},
                    "request": {
                        "sizes": {"headers": 128, "body": len(body_txt)},
                        "mimeType": "application/json",
                        "charset": "UTF-8",
                        "header": {
                            "firstLine": f"WIFI_{item.get('ftype','MGMT')} /mac/{item.get('mac')} 802.11/2.4GHz",
                            "headers": [
                                {"name": "X-RF-Date", "value": dt_str},
                                {"name": "X-RF-Source-MAC", "value": str(item.get("mac"))},
                                {"name": "X-RF-OUI-Vendor", "value": str(item.get("oui"))},
                                {"name": "X-RF-SSID", "value": str(item.get("ssid"))},
                                {"name": "X-RF-RSSI-dBm", "value": f"{item.get('rssi')} dBm"}
                            ]
                        },
                        "body": {"text": body_txt, "charset": "UTF-8"}
                    },
                    "response": {
                        "status": 200,
                        "sizes": {"headers": 64, "body": len(body_txt)},
                        "mimeType": "application/json",
                        "charset": "UTF-8",
                        "header": {"firstLine": "802.11 200 OK", "headers": [{"name": "Content-Type", "value": "application/json"}]},
                        "body": {"text": body_txt, "charset": "UTF-8"}
                    }
                })
            else:
                ds = item.get("ds", {})
                proto_s = ds.get("proto", "Raw LoRa")
                port = 4242 if proto_s == "Reticulum" else (4403 if proto_s == "Meshtastic" else 9150)
                body_txt = json.dumps(item, indent=2)
                charles_list.append({
                    "status": "COMPLETE",
                    "method": "LORA_RX",
                    "protocolVersion": "LoRa/SubGHz",
                    "scheme": "http",
                    "host": f"{proto_s.lower().replace(' ','-')}.subghz.stump",
                    "port": port,
                    "actualPort": port,
                    "path": f"/{item.get('freq')}MHz/SF{item.get('sf')}/{ds.get('uid','node')}",
                    "query": f"rssi={item.get('rssi')}&snr={item.get('snr')}&ferr={item.get('ferr')}",
                    "tunnel": False,
                    "times": {"start": iso_s, "requestBegin": iso_s, "requestComplete": iso_s, "responseBegin": iso_s, "end": iso_s},
                    "durations": {"total": 1, "dns": 0, "connect": 0, "ssl": 0, "request": 0, "response": 1, "latency": 1},
                    "request": {
                        "sizes": {"headers": 128, "body": len(str(item.get("payload", "")))},
                        "mimeType": "application/octet-stream",
                        "charset": "UTF-8",
                        "header": {
                            "firstLine": f"LORA_RX /{item.get('freq')}MHz/SF{item.get('sf')} LoRa/1.0",
                            "headers": [
                                {"name": "X-RF-Date", "value": dt_str},
                                {"name": "X-RF-Protocol", "value": proto_s},
                                {"name": "X-RF-RSSI-dBm", "value": f"{item.get('rssi')} dBm"},
                                {"name": "X-RF-SNR-dB", "value": f"{item.get('snr')} dB"},
                                {"name": "X-RF-Crystal-Ferr-Hz", "value": f"{item.get('ferr')} Hz"}
                            ]
                        },
                        "body": {"text": str(item.get("payload", "")), "charset": "UTF-8"}
                    },
                    "response": {
                        "status": 200,
                        "sizes": {"headers": 64, "body": len(body_txt)},
                        "mimeType": "application/json",
                        "charset": "UTF-8",
                        "header": {"firstLine": "LoRa/1.0 200 CRC_OK", "headers": [{"name": "Content-Type", "value": "application/json"}]},
                        "body": {"text": body_txt, "charset": "UTF-8"}
                    }
                })
        with open(chlsj_path, "w") as cf:
            json.dump(charles_list, cf, indent=2)

        # 3. Write Full Structured JSON (.json)
        json_path = base_path + ".json"
        full_json = {
            "exported_at_date": fmt_dt(),
            "exported_at_iso": fmt_iso(),
            "radio_status": st,
            "discovered_devices": [
                {
                    **dv,
                    "first_seen_date": fmt_dt(dv.get("first_seen")),
                    "last_seen_date": fmt_dt(dv.get("last_seen"))
                }
                for dv in devs_snap
            ],
            "captured_events": events_snap,
            "serial_logs": list(sl)
        }
        with open(json_path, "w") as jf:
            json.dump(full_json, jf, indent=2)

        export_banner_msg = f"SAVED .pcap + .chlsj + .json -> exports/stump_{slug}.*"
        export_banner_until = time.time() + 6.0
        ad_log(f"EXPORTED stump_{slug}.pcap/.chlsj/.json")
    except Exception as ex:
        export_banner_msg = f"EXPORT ERR: {ex}"
        export_banner_until = time.time() + 5.0
        ad_log(f"EXPORT ERR: {ex}")

def insert_event(item):
    global si
    if ui_paused:
        return
    if len(ev) > 0 and (si > 0 or vi == 2):
        si = min(39, si + 1)
    ev.insert(0, item)
    del ev[40:]

def match_physical_lora_device(freq, sf, sync, ferr, rssi, dest_hash, from_id, verified_id, proto, crc_ok=True):
    """
    Matches an incoming LoRa packet to an existing physical LoRa transmitter in devices_db.
    Ensures the user's ~3 physical Reticulum/LoRa radios are each identified as a Reticulum node
    without exploding into 27 cards from IFAC/DATA/split packets or dropping to 0.
    """
    # 1. Exact Meshtastic hardware NodeNum match
    if proto == "Meshtastic" and verified_id and from_id:
        uid = f"MESH:!{from_id}"
        if uid in devices_db:
            return devices_db[uid]

    # 2. Exact Reticulum Destination Hash match on a known physical radio
    if proto == "Reticulum" and dest_hash:
        d12 = dest_hash[:12]
        for rec in devices_db.values():
            if rec.get("band") == "SUB-GHZ LORA":
                if d12 in rec.get("rns_dests", []) or rec.get("uid") == f"RNS:{d12}":
                    return rec

    # Count currently active LoRa devices on this sync word
    lora_recs = [
        r for r in devices_db.values()
        if r.get("band") == "SUB-GHZ LORA" and int(r.get("sync", 18)) == sync
    ]

    # 3. Physical Layer RF Fingerprint Match: Crystal Freq Error (ferr_avg) + Kalman RSSI proximity
    best_rec = None
    best_score = 999999.0
    for rec in lora_recs:
        # If both have different verified Meshtastic hardware IDs, keep them separate
        if proto == "Meshtastic" and verified_id and rec.get("from_id") and rec.get("from_id") != from_id:
            continue

        df = abs(ferr - float(rec.get("ferr_avg", ferr)))
        dr = abs(rssi - float(rec.get("rssi_kalman", rssi)))

        # Tight match: same physical radio crystal offset (+/- 450 Hz) & RSSI (+/- 8.0 dB)
        if df <= 450.0 and dr <= 8.0:
            score = df + (dr * 30.0)
            if score < best_score:
                best_score = score
                best_rec = rec
        # Wider match for non-announce frames, CRC-error frames, or when >= 3 nodes are already tracked
        elif (not verified_id or not crc_ok or len(lora_recs) >= 3) and df <= 950.0 and dr <= 13.5:
            score = df + (dr * 30.0)
            if score < best_score:
                best_score = score
                best_rec = rec

    return best_rec

def update_device_registry(kind, pkt):
    now_t = time.time()
    with devices_lock:
        if kind == "LORA":
            rssi = float(pkt.get("rssi", -105.0))
            if rssi <= -135.0:
                return
            crc_ok = bool(pkt.get("crc_ok", True))
            ds = pkt.get("ds", {})
            sync = int(pkt.get("sync", 18))
            proto = ds.get("proto", "Reticulum" if sync == 18 else "Raw LoRa")
            if sync == 18:
                proto = "Reticulum"
            snr = float(pkt.get("snr", 0.0))
            ferr = float(pkt.get("ferr", 0.0))
            freq = float(pkt.get("freq", 915.0))
            sf = int(pkt.get("sf", 8))
            verified_id = bool(ds.get("verified_id", False))
            dest = str(ds.get("dest", "") or "")
            from_id = str(ds.get("from", "") or "")

            rec = match_physical_lora_device(freq, sf, sync, ferr, rssi, dest, from_id, verified_id, proto, crc_ok)

            if not rec:
                if proto == "Meshtastic" and from_id:
                    uid = f"MESH:!{from_id}"
                    name = f"Mesh !{from_id}"
                elif proto == "Reticulum":
                    d_tag = dest[:10] if len(dest) >= 4 else f"{freq:.1f}M{ferr:+.0f}Hz"
                    uid = f"RNS:{dest[:12]}" if len(dest) >= 4 else f"RNS:{freq:.1f}M:SF{sf}:{int(round(ferr/350.0)*350):+d}Hz"
                    # Avoid UID collision if two distinct physical radios happened to share short tag
                    if uid in devices_db:
                        uid = f"{uid}:{int(round(rssi))}"
                    name = f"RNS <{d_tag}>"
                else:
                    ferr_bucket = int(round(ferr / 400.0) * 400)
                    uid = f"LORA:{freq:.1f}M:SF{sf}:{ferr_bucket:+d}Hz"
                    name = f"LoRa {freq:.1f}M ({ferr:+.0f}Hz)"

                rns_list = [dest[:12]] if (proto == "Reticulum" and len(dest) >= 4) else []
                rec = {
                    "uid": uid, "name": name, "dtype": proto, "band": "SUB-GHZ LORA",
                    "confirmed": True,
                    "rssi_raw": rssi, "rssi_kalman": rssi, "rssi_min": rssi, "rssi_max": rssi,
                    "kp": 2.0, "history": [rssi], "pkts": 1,
                    "first_seen": now_t, "last_seen": now_t, "avg_int_ms": 0,
                    "freq": freq, "sf": sf,
                    "bw": pkt.get("bw", 125.0), "cr": pkt.get("cr", 5),
                    "sync": sync, "snr": snr, "ferr": ferr,
                    "ferr_avg": ferr, "hops": ds.get("hops", 0),
                    "ifac": ds.get("ifac", False), "dest": dest,
                    "rns_dests": rns_list,
                    "from_id": from_id, "to_id": ds.get("to", ""),
                    "mesh_ch": ds.get("ch", ""), "ptype": ds.get("type", "PKT"),
                    "crc_ok": crc_ok, "last_len": pkt.get("len", 0),
                    "payload_hex": pkt.get("payload", ""),
                    "payload_ascii": hex_to_ascii(pkt.get("payload", ""))
                }
                devices_db[uid] = rec
            else:
                dt_ms = int((now_t - rec["last_seen"]) * 1000)
                if dt_ms > 10:
                    rec["avg_int_ms"] = dt_ms if rec["avg_int_ms"] == 0 else int(rec["avg_int_ms"] * 0.7 + dt_ms * 0.3)
                rec["pkts"] += 1
                rec["last_seen"] = now_t
                rec["rssi_raw"] = rssi
                rec["rssi_min"] = min(rec["rssi_min"], rssi)
                rec["rssi_max"] = max(rec["rssi_max"], rssi)
                rec["kp"] += 0.15
                k = rec["kp"] / (rec["kp"] + 3.0)
                rec["rssi_kalman"] = round(rec["rssi_kalman"] + k * (rssi - rec["rssi_kalman"]), 1)
                rec["kp"] *= (1.0 - k)
                rec["history"].append(rssi)
                if len(rec["history"]) > 18:
                    rec["history"].pop(0)
                rec["snr"] = snr
                rec["ferr"] = ferr
                rec["ferr_avg"] = round(rec["ferr_avg"] * 0.7 + ferr * 0.3, 1)
                rec["freq"] = freq
                rec["sf"] = sf
                rec["bw"] = pkt.get("bw", rec["bw"])
                rec["hops"] = ds.get("hops", rec["hops"])
                rec["ptype"] = ds.get("type", rec["ptype"])
                rec["ifac"] = ds.get("ifac", rec.get("ifac", False))
                rec["crc_ok"] = crc_ok or rec.get("crc_ok", True)
                rec["last_len"] = pkt.get("len", rec["last_len"])
                if proto == "Reticulum":
                    rec["dtype"] = "Reticulum"
                    if len(dest) >= 4 and (verified_id or not rec.get("dest")):
                        rec["dest"] = dest
                        if dest[:12] not in rec.get("rns_dests", []):
                            rec.setdefault("rns_dests", []).append(dest[:12])
                            if len(rec["rns_dests"]) > 12:
                                rec["rns_dests"].pop(0)
                        rec["name"] = f"RNS <{dest[:10]}>"
                if pkt.get("payload"):
                    rec["payload_hex"] = pkt.get("payload", "")
                    rec["payload_ascii"] = hex_to_ascii(pkt.get("payload", ""))

        elif kind == "LORA_SCAN":
            rssi = float(pkt.get("rssi", -116.0))
            if rssi <= -93.0 or rssi >= -16.0:
                return
            freq = float(pkt.get("freq", 915.0))
            sf = int(pkt.get("sf", st.get("sf", 8)))
            bw = float(pkt.get("bw", st.get("bw", 125.0)))
            cr = int(pkt.get("cr", st.get("cr", 5)))
            sync = int(pkt.get("sync", st.get("sync", 18)))
            proto = "Reticulum" if sync == 18 else ("Meshtastic" if sync == 43 else "Raw LoRa")

            lora_recs = [r for r in devices_db.values() if r.get("band") == "SUB-GHZ LORA"]
            best_rec = None
            best_dr = 999.0
            for cand in lora_recs:
                dr = abs(rssi - float(cand.get("rssi_kalman", rssi)))
                df_mhz = abs(freq - float(cand.get("freq", freq)))
                if (df_mhz <= 1.5 and dr <= 11.0) or dr <= 8.5 or len(lora_recs) >= 3:
                    score = dr + (df_mhz * 2.5)
                    if score < best_dr:
                        best_dr = score
                        best_rec = cand

            if not best_rec:
                rssi_bkt = int(round(rssi / 9.0) * 9)
                uid = f"RNS:RF:{freq:.1f}M:{rssi_bkt}dBm"
                if uid in devices_db:
                    best_rec = devices_db[uid]
                else:
                    name = f"RNS <{freq:.1f}M {rssi_bkt}dB>" if proto == "Reticulum" else f"{proto} {freq:.1f}M"
                    devices_db[uid] = {
                        "uid": uid, "name": name, "dtype": proto, "band": "SUB-GHZ LORA",
                        "confirmed": True,
                        "rssi_raw": rssi, "rssi_kalman": rssi, "rssi_min": rssi, "rssi_max": rssi,
                        "kp": 2.0, "history": [rssi], "pkts": 1,
                        "first_seen": now_t, "last_seen": now_t, "avg_int_ms": 0,
                        "freq": freq, "sf": sf, "bw": bw, "cr": cr,
                        "sync": sync, "snr": round((rssi + 115.0) * 0.35, 1), "ferr": 0.0,
                        "ferr_avg": 0.0, "hops": 0,
                        "ifac": False, "dest": f"{freq:.1f}MHz_RF",
                        "rns_dests": [], "from_id": "", "to_id": "",
                        "mesh_ch": "", "ptype": "RF_CARRIER",
                        "crc_ok": True, "last_len": 0,
                        "payload_hex": "", "payload_ascii": "[Sub-GHz LoRa Carrier Active]"
                    }
            if best_rec:
                dt_ms = int((now_t - best_rec["last_seen"]) * 1000)
                if dt_ms > 20:
                    best_rec["avg_int_ms"] = dt_ms if best_rec["avg_int_ms"] == 0 else int(best_rec["avg_int_ms"] * 0.7 + dt_ms * 0.3)
                best_rec["pkts"] += 1
                best_rec["last_seen"] = now_t
                best_rec["confirmed"] = True
                best_rec["rssi_raw"] = rssi
                best_rec["rssi_min"] = min(best_rec["rssi_min"], rssi)
                best_rec["rssi_max"] = max(best_rec["rssi_max"], rssi)
                best_rec["kp"] += 0.15
                k = best_rec["kp"] / (best_rec["kp"] + 3.0)
                best_rec["rssi_kalman"] = round(best_rec["rssi_kalman"] + k * (rssi - best_rec["rssi_kalman"]), 1)
                best_rec["kp"] *= (1.0 - k)
                best_rec["history"].append(rssi)
                if len(best_rec["history"]) > 18:
                    best_rec["history"].pop(0)

        elif kind == "WIFI":
            mac = (pkt.get("mac") or "00:00:00:00:00:00").lower()
            if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                return
            rssi = float(pkt.get("rssi", -85.0))
            ftype = pkt.get("ftype", "MGMT")
            ssid = (pkt.get("ssid") or "").strip()
            if ssid == "<Wildcard>":
                ssid = ""
            oui = pkt.get("oui") or resolve_oui(mac)
            laa = bool(pkt.get("laa", False)) or (oui == "Randomized (LAA)")
            ch = int(pkt.get("ch", 6))
            dtype = "Wi-Fi AP" if ftype == "BEACON" else "Wi-Fi Client"
            if "Heltec" in oui or "Espressif" in oui:
                dtype = "ESP32 / Heltec"
            short_mac = mac[-8:]

            # Cluster Randomized (LAA) Client Probe Bursts so 1 phone rotating MACs doesn't create 50 cards
            rec = None
            if laa and ftype != "BEACON" and dtype != "ESP32 / Heltec":
                if ssid:
                    uid = f"WIFI:LAA:SSID:{ssid[:16].lower()}"
                    rec = devices_db.get(uid)
                else:
                    # Match existing wildcard LAA probe cluster within +/- 10 dBm proximity
                    for cand in devices_db.values():
                        if cand.get("band") == "2.4GHZ WIFI" and cand.get("laa") and cand.get("ftype") != "BEACON":
                            if abs(rssi - float(cand.get("rssi_kalman", rssi))) <= 10.0:
                                rec = cand
                                uid = cand["uid"]
                                break
                    if not rec:
                        rssi_bkt = int(round(rssi / 12.0) * 12)
                        uid = f"WIFI:LAA:PROX:{rssi_bkt}dBm"
                        rec = devices_db.get(uid)
                name = f"LAA Probe \"{ssid[:10]}\"" if ssid else f"LAA Mobile ({short_mac})"
            else:
                uid = f"WIFI:{mac}"
                rec = devices_db.get(uid)
                name = f"{ssid[:14]} ({short_mac})" if ssid else f"{oui[:12]} ({short_mac})"

            # Require 2+ packets OR strong nearby signal (>=-68dBm) OR AP Beacon (>=-80dBm) to confirm
            is_conf = bool(rssi >= -68.0 or (ftype == "BEACON" and rssi >= -80.0) or dtype == "ESP32 / Heltec")

            if not rec:
                rec = {
                    "uid": uid, "name": name, "dtype": dtype, "band": "2.4GHZ WIFI",
                    "confirmed": is_conf,
                    "rssi_raw": rssi, "rssi_kalman": rssi, "rssi_min": rssi, "rssi_max": rssi,
                    "kp": 2.0, "history": [rssi], "pkts": 1,
                    "first_seen": now_t, "last_seen": now_t, "avg_int_ms": 0,
                    "mac": mac, "rotated_macs": [mac],
                    "bssid": pkt.get("bssid", "ff:ff:ff:ff:ff:ff"),
                    "oui": oui, "laa": laa,
                    "ch": ch, "ftype": ftype, "ftypes_seen": [ftype],
                    "ssids": [ssid] if ssid else [], "last_len": pkt.get("len", 0)
                }
                devices_db[uid] = rec
            else:
                dt_ms = int((now_t - rec["last_seen"]) * 1000)
                if dt_ms > 10:
                    rec["avg_int_ms"] = dt_ms if rec["avg_int_ms"] == 0 else int(rec["avg_int_ms"] * 0.7 + dt_ms * 0.3)
                rec["pkts"] += 1
                rec["last_seen"] = now_t
                rec["rssi_raw"] = rssi
                rec["rssi_min"] = min(rec["rssi_min"], rssi)
                rec["rssi_max"] = max(rec["rssi_max"], rssi)
                rec["kp"] += 0.15
                k = rec["kp"] / (rec["kp"] + 3.0)
                rec["rssi_kalman"] = round(rec["rssi_kalman"] + k * (rssi - rec["rssi_kalman"]), 1)
                rec["kp"] *= (1.0 - k)
                rec["history"].append(rssi)
                if len(rec["history"]) > 18:
                    rec["history"].pop(0)
                rec["ch"] = ch
                rec["ftype"] = ftype
                rec["mac"] = mac
                rec["last_len"] = pkt.get("len", rec["last_len"])
                if mac not in rec.get("rotated_macs", []):
                    rec.setdefault("rotated_macs", []).append(mac)
                    if len(rec["rotated_macs"]) > 24:
                        rec["rotated_macs"].pop(0)
                if ftype not in rec["ftypes_seen"]:
                    rec["ftypes_seen"].append(ftype)
                if ssid and ssid not in rec["ssids"]:
                    rec["ssids"].append(ssid)
                # Confirm once seen >= 2 packets (for non-LAA or strong LAA) or >= 3 packets for weak LAA
                if rec["pkts"] >= 2 and (not laa or rec["pkts"] >= 3 or rec["rssi_kalman"] >= -76.0):
                    rec["confirmed"] = True
                elif is_conf:
                    rec["confirmed"] = True
                if laa and len(rec.get("rotated_macs", [])) > 1:
                    first_ssid = rec["ssids"][0][:10] if rec["ssids"] else "Wildcard"
                    rec["name"] = f"LAA ({len(rec['rotated_macs'])} MACs) {first_ssid}"
                elif ssid:
                    rec["name"] = f"{ssid[:14]} ({short_mac})"

def gc_stale_devices(now_t=None, force_aggressive=False):
    """
    Periodic TTL Garbage Collector.
    Evicts transient 1-packet Wi-Fi ghosts and rotated LAA probe bursts while preserving
    active Reticulum / LoRa nodes across announce intervals.
    """
    global gc_purged_count
    if now_t is None:
        now_t = time.time()
    removed = 0
    with devices_lock:
        stale_uids = []
        for uid, rec in devices_db.items():
            age = now_t - float(rec.get("last_seen", now_t))
            band = rec.get("band", "2.4GHZ WIFI")
            if force_aggressive:
                # Manual purge: aggressively clean transient Wi-Fi ghosts; preserve active LoRa nodes (<300s)
                if band == "SUB-GHZ LORA":
                    if age > 300.0:
                        stale_uids.append(uid)
                else:
                    if age > 12.0 or not rec.get("confirmed", True) or rec.get("pkts", 1) <= 1:
                        stale_uids.append(uid)
                continue
            if band == "SUB-GHZ LORA":
                if age > TTL_LORA_SEC:
                    stale_uids.append(uid)
            else:
                if not rec.get("confirmed", False):
                    if age > TTL_CANDIDATE_WIFI_SEC:
                        stale_uids.append(uid)
                elif rec.get("laa", False):
                    if age > TTL_LAA_WIFI_SEC:
                        stale_uids.append(uid)
                else:
                    if age > TTL_CONFIRMED_WIFI_SEC:
                        stale_uids.append(uid)
        for uid in stale_uids:
            devices_db.pop(uid, None)
            removed += 1
        gc_purged_count += removed

    # Also clean stale seen_beacons cache
    stale_b = [m for m, (ts_b, _) in list(seen_beacons.items()) if now_t - ts_b > 60.0]
    for m in stale_b:
        seen_beacons.pop(m, None)
    return removed

def rebuild_device_snapshot():
    global displayed_devices, dev_cursor_idx, dev_selected_uid
    with devices_lock:
        # Only display confirmed active devices (filters out 1-packet weak Wi-Fi noise candidates)
        items = [dict(v) for v in devices_db.values() if v.get("confirmed", True)]
    sort_mode = SORT_MODES[sort_idx]
    if sort_mode == "PROXIMITY":
        items.sort(key=lambda d: d.get("rssi_kalman", -120.0), reverse=True)
    elif sort_mode == "NAME":
        items.sort(key=lambda d: (d.get("name", "").lower(), -d.get("rssi_kalman", -120.0)))
    elif sort_mode == "TYPE":
        items.sort(key=lambda d: (d.get("dtype", ""), -d.get("rssi_kalman", -120.0)))

    displayed_devices = items
    if not displayed_devices:
        dev_cursor_idx = 0
        dev_selected_uid = None
        return

    # Keep cursor locked onto the same device UID if it was already selected!
    if dev_selected_uid:
        for idx, d in enumerate(displayed_devices):
            if d["uid"] == dev_selected_uid:
                dev_cursor_idx = idx
                return
    dev_cursor_idx = max(0, min(len(displayed_devices) - 1, dev_cursor_idx))
    dev_selected_uid = displayed_devices[dev_cursor_idx]["uid"]

def cm(c):
    ad_log("TX> " + c)
    if sr:
        try:
            sr.write((c + "\n").encode())
        except Exception:
            pass

def cm_silent(c):
    global tdm_silent_until
    tdm_silent_until = time.time() + 0.45
    if sr:
        try:
            sr.write((c + "\n").encode())
        except Exception:
            pass

def set_acq_mode(new_mode):
    global last_tdm_action_ts, tdm_scan_burst_active
    st["mode"] = new_mode
    last_tdm_action_ts = time.time()
    tdm_scan_burst_active = False
    if new_mode == "sniff":
        st["live_freq"] = float(st.get("freq", 915.0))
        cm("mode sniff")
    elif new_mode == "sweep":
        cm("mode sweep")
    elif new_mode == "scan":
        cm("mode scan")
    elif new_mode == "idle":
        cm("mode idle")

def nearest_lora_bin(freq_val):
    return min(SCAN_FREQS, key=lambda b: abs(b - freq_val))

def push_waterfall_row():
    wr.insert(0, [ls[k] for k in SCAN_FREQS])
    del wr[16:]

SCAN_INLINE_RE = re.compile(r'\{"ev":"scan","t":\d+,"freq":[-0-9.]+,"rssi":[-0-9.]+\}')

def salvage_rx_dict(s):
    """Fallback regex extractor if a UART line containing ev:rx was partially truncated."""
    if '"ev":"rx"' not in s:
        return None
    def grab_num(key, default_v, is_int=False):
        m = re.search(rf'"{key}":([-0-9.]+)', s)
        if not m:
            return default_v
        try:
            return int(float(m.group(1))) if is_int else float(m.group(1))
        except Exception:
            return default_v
    pl_m = re.search(r'"payload":"([0-9a-fA-F]*)', s)
    crc_m = re.search(r'"crc_ok":(true|false)', s)
    return {
        "ev": "rx",
        "t": grab_num("t", int(time.time() * 1000), True),
        "freq": grab_num("freq", float(st.get("freq", 915.0))),
        "bw": grab_num("bw", float(st.get("bw", 125.0))),
        "sf": grab_num("sf", int(st.get("sf", 8)), True),
        "cr": grab_num("cr", int(st.get("cr", 5)), True),
        "sync": grab_num("sync", int(st.get("sync", 18)), True),
        "len": grab_num("len", 0, True),
        "rssi": grab_num("rssi", -95.0),
        "snr": grab_num("snr", 0.0),
        "ferr": grab_num("ferr", 0.0),
        "crc_ok": (crc_m.group(1) == "true") if crc_m else True,
        "payload": pl_m.group(1) if pl_m else ""
    }

def handle_parsed_event(d):
    global kx, kp, last_wifi_ui_ts, last_nonhome_scan_ts, force_snapshot
    if not isinstance(d, dict):
        return
    now_ep = time.time()
    d["dt"] = fmt_dt(now_ep)
    d["ts_epoch"] = round(now_ep, 3)
    if jsonl_f:
        try:
            jsonl_f.write(json.dumps(d) + "\n")
            jsonl_f.flush()
        except Exception:
            pass
    e = d.get("ev")

    if e == "rx":
        v = float(d.get("rssi", -100.0))
        rx_freq = float(d.get("freq", st.get("live_freq", 915.0)))
        st["live_freq"] = rx_freq
        kp += 0.1
        k = kp / (kp + 3.5)
        kx += k * (v - kx)
        kp *= (1.0 - k)
        d["kal"] = round(kx, 1)
        d["ds"] = dissect_lora(d)
        d["k"] = "L"
        b_freq = nearest_lora_bin(rx_freq)
        ls[b_freq] = v
        push_waterfall_row()
        update_device_registry("LORA", d)
        force_snapshot = True
        if not ui_paused:
            lp.insert(0, d)
            del lp[30:]
        insert_event(d)
        ad_log(f"LORA {rx_freq:.3f}MHz SF{d.get('sf')} {d['ds']['proto']} <{d['ds'].get('dest','')[:8]}> {v:.0f}dBm SNR:{d.get('snr')}dB")

    elif e == "scan":
        f_val = float(d.get("freq", 915.0))
        r_val = float(d.get("rssi", -116.0))
        st["live_freq"] = f_val
        if abs(f_val - float(st.get("freq", 915.0))) > 0.4:
            last_nonhome_scan_ts = now_ep
        b_freq = nearest_lora_bin(f_val)
        valid_burst = (-92.0 <= r_val <= -18.0)
        if r_val >= -15.0 or r_val <= -140.0:
            r_val = -115.0 + random.uniform(-2.5, 2.5)
        ls[b_freq] = r_val
        if b_freq == 925 or st.get("mode") != "scan":
            push_waterfall_row()
        # If the Sub-GHz scan detects a genuine active RF transmission burst above the band noise median,
        # register/update the LoRa device card immediately even before/without full payload decode!
        if valid_burst:
            sorted_bins = sorted(ls.values())
            noise_med = sorted_bins[len(sorted_bins) // 2]
            if (r_val - noise_med >= 11.5) or (r_val >= -78.0):
                update_device_registry("LORA_SCAN", {
                    "freq": f_val,
                    "rssi": r_val,
                    "sf": st.get("sf", 8),
                    "bw": st.get("bw", 125.0),
                    "cr": st.get("cr", 5),
                    "sync": st.get("sync", 18)
                })
                force_snapshot = True

    elif e == "wifi":
        ch = max(1, min(13, int(d.get("ch", 6))))
        r_val = float(d.get("rssi", -85.0))
        ws[ch] = r_val
        mac = d.get("mac", "00:00:00:00:00:00")
        ftype = d.get("ftype", "MGMT")
        now_t = now_ep
        d["oui"] = resolve_oui(mac)
        d["k"] = "W"

        update_device_registry("WIFI", d)

        if ftype == "BEACON":
            prev = seen_beacons.get(mac)
            if prev and (now_t - prev[0] < 10.0) and abs(r_val - prev[1]) < 6.0:
                return
            seen_beacons[mac] = (now_t, r_val)

        if now_t - last_wifi_ui_ts < 0.35 and ftype == "BEACON":
            return
        last_wifi_ui_ts = now_t

        if not ui_paused:
            wf.insert(0, d)
            del wf[30:]
        insert_event(d)
        ssid_str = d.get("ssid") or "<Wildcard>"
        ad_log(f"WIFI Ch{ch:02d} {ftype:<9} {mac} ({d['oui'][:10]}) \"{ssid_str[:14]}\" {r_val:.0f}dBm")

    elif e == "status":
        if now_ep < tdm_silent_until:
            if "freq" in d:
                st["live_freq"] = float(d["freq"])
        else:
            st.update(d)
            st["live_freq"] = float(d.get("freq", st.get("live_freq", 915.0)))
            ad_log(f"STAT {float(st.get('live_freq',915.0)):.3f}MHz SF{st.get('sf')} BW{st.get('bw')} mode={st.get('mode')}")

    elif e == "boot":
        ad_log(f"BOOT Heltec V3 fw={d.get('fw')}")

def wk():
    global sr, sp
    pending_frag = ""
    while True:
        if not sr:
            for p in ("/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyUSB1"):
                if os.path.exists(p) and serial:
                    try:
                        s = serial.Serial()
                        s.port = p
                        s.baudrate = 115200
                        s.timeout = 0.3
                        s.dsrdtr = False
                        s.rtscts = False
                        s.open()
                        sr = s
                        sp = p
                        pending_frag = ""
                        sr.write(b"status\n")
                        ad_log("SYS Connected " + p + " (RNS 0x12 + TDM Sweep Active)")
                        break
                    except Exception:
                        sr = sp = None
        if sr:
            try:
                raw_ln = sr.readline()
            except Exception:
                sr = sp = None
                pending_frag = ""
                continue

            if not raw_ln:
                continue
            ln = raw_ln.decode("utf-8", "ignore").strip()
            if not ln:
                continue

            # 1. Extract any atomic {"ev":"scan",...} blocks that Core 1 may have injected mid-line
            scan_matches = SCAN_INLINE_RE.findall(ln)
            for sm in scan_matches:
                try:
                    handle_parsed_event(json.loads(sm))
                except Exception:
                    pass

            rem = SCAN_INLINE_RE.sub("", ln).strip()
            if not rem:
                continue

            # 2. Stitch split lines back together if an injected scan newline broke an rx/wifi line in two
            if rem.startswith('{"ev":'):
                if pending_frag:
                    salv = salvage_rx_dict(pending_frag)
                    if salv:
                        handle_parsed_event(salv)
                pending_frag = rem
            else:
                pending_frag = (pending_frag + rem) if pending_frag else rem

            if pending_frag.endswith("}"):
                # Handle potential back-to-back JSON objects on a single line
                chunks = pending_frag.replace("}{", "}\n{").splitlines()
                pending_frag = ""
                for ch_s in chunks:
                    ch_s = ch_s.strip()
                    if not ch_s:
                        continue
                    try:
                        handle_parsed_event(json.loads(ch_s))
                    except Exception:
                        salv = salvage_rx_dict(ch_s)
                        if salv:
                            handle_parsed_event(salv)
            elif len(pending_frag) > 1024:
                salv = salvage_rx_dict(pending_frag)
                if salv:
                    handle_parsed_event(salv)
                pending_frag = ""
        else:
            time.sleep(1.0)

threading.Thread(target=wk, daemon=True).start()

# --- 4. Interactive Controls & D-Pad Navigation ---
BW_LIST = [62.5, 125.0, 250.0, 500.0]
SY_LIST = [18, 43, 52]
MD_LIST = ["sniff", "sweep", "scan", "idle"]

def move_device_cursor(dx, dy):
    global dev_cursor_idx, dev_selected_uid
    if not displayed_devices:
        return
    # 2-column card grid navigation: Left/Right moves +/-1, Up/Down moves +/-2
    step = dx + (dy * 2)
    dev_cursor_idx = max(0, min(len(displayed_devices) - 1, dev_cursor_idx + step))
    dev_selected_uid = displayed_devices[dev_cursor_idx]["uid"]

def dp(dx, dy):
    global si, tr
    if dev_popup_open:
        # While popup is open, Left/Right or Up/Down steps between devices for quick comparison
        move_device_cursor(dx, dy)
        return
    if vi == 1:
        move_device_cursor(dx, dy)
        return
    if dy != 0:
        if vi == 2:
            si = max(0, min(max(0, len(ev) - 1), si + dy))
        elif vi == 3:
            tr = (tr + dy) % 8
    if dx != 0 and vi == 3:
        if tr == 0:
            nf = round(max(150.0, min(960.0, float(st.get("freq", 915.0)) + dx * 0.5)), 1)
            st["freq"] = nf
            st["live_freq"] = nf
            cm(f"freq {nf}")
        elif tr == 1:
            nsf = max(7, min(12, int(st.get("sf", 8)) + dx))
            st["sf"] = nsf
            cm(f"sf {nsf}")
        elif tr == 2:
            cur_bw = float(st.get("bw", 125.0))
            idx = BW_LIST.index(cur_bw) if cur_bw in BW_LIST else 1
            nbw = BW_LIST[(idx + dx) % len(BW_LIST)]
            st["bw"] = nbw
            cm(f"bw {nbw}")
        elif tr == 3:
            ncr = max(5, min(8, int(st.get("cr", 5)) + dx))
            st["cr"] = ncr
            cm(f"cr {ncr}")
        elif tr == 4:
            cur_sy = int(st.get("sync", 18))
            idx = SY_LIST.index(cur_sy) if cur_sy in SY_LIST else 0
            nsy = SY_LIST[(idx + dx) % len(SY_LIST)]
            st["sync"] = nsy
            cm(f"sync 0x{nsy:02X}")
        elif tr == 5:
            cur_m = str(st.get("mode", "sniff"))
            idx = MD_LIST.index(cur_m) if cur_m in MD_LIST else 0
            nm = MD_LIST[(idx + dx) % len(MD_LIST)]
            set_acq_mode(nm)
        elif tr == 6:
            st["wifi"] = not bool(st.get("wifi", True))
            cm(f"wifi {'on' if st['wifi'] else 'off'}")
        elif tr == 7:
            nch = (int(st.get("wifich", 0)) + dx) % 14
            st["wifich"] = nch
            cm(f"wifich {'hop' if nch == 0 else nch}")

ck = pygame.time.Clock()
rn = True
axis_cd = 0.0
decay_ts = time.time()

while rn:
    now_loop = time.time()

    # Firmware v0.3.6+ handles TDM scanning natively on Core 1 without resetting SX1262.
    # Only trigger a fallback scan if an older non-sweeping firmware has been silent for >8s.
    cur_mode = str(st.get("mode", "sniff"))
    home_f = float(st.get("freq", 915.0))
    if sr and not ui_paused and cur_mode == "sniff":
        if tdm_scan_burst_active:
            if now_loop - last_tdm_action_ts >= 0.16:
                tdm_scan_burst_active = False
                last_tdm_action_ts = now_loop
                st["live_freq"] = home_f
                cm_silent("mode sniff")
        elif (now_loop - last_tdm_action_ts >= 8.0) and (now_loop - last_nonhome_scan_ts >= 8.0):
            tdm_scan_burst_active = True
            last_tdm_action_ts = now_loop
            cm_silent("mode scan")

    # Periodic TTL Garbage Collection (every 1.0s) to evict transient 1-pkt ghosts & stale devices
    if now_loop - last_gc_ts >= 1.0:
        last_gc_ts = now_loop
        evicted_n = gc_stale_devices(now_loop, force_aggressive=False)
        if evicted_n > 0:
            force_snapshot = True

    # Smooth spectrum decay toward noise floor
    if now_loop - decay_ts > 0.20:
        decay_ts = now_loop
        for k_f in SCAN_FREQS:
            if ls[k_f] > -116.0:
                ls[k_f] = max(-116.0, ls[k_f] - 1.2)
            else:
                ls[k_f] = -116.0 + random.uniform(-1.5, 1.5)
        for k_c in range(1, 14):
            if ws[k_c] > -95.0:
                ws[k_c] = max(-95.0, ws[k_c] - 0.8)

    # Refresh Device Cards Snapshot according to selected Refresh Interval
    ref_label, ref_sec = REFRESH_PRESETS[refresh_idx]
    if not ui_paused and (force_snapshot or ref_sec == 0.0 or (now_loop - last_snapshot_ts >= ref_sec)):
        rebuild_device_snapshot()
        last_snapshot_ts = now_loop
        force_snapshot = False

    for e in pygame.event.get():
        if e.type == pygame.QUIT:
            rn = False
        elif e.type == pygame.KEYDOWN:
            k = e.key
            if k in (pygame.K_ESCAPE, pygame.K_q):
                if dev_popup_open:
                    dev_popup_open = False
                else:
                    rn = False
            elif k == pygame.K_RETURN or k == pygame.K_a:
                if vi == 1 and displayed_devices:
                    dev_popup_open = not dev_popup_open
                else:
                    set_acq_mode("sniff")
            elif k == pygame.K_b or k == pygame.K_BACKSPACE:
                if dev_popup_open:
                    dev_popup_open = False
                elif vi == 1:
                    purged_now = gc_stale_devices(now_loop, force_aggressive=True)
                    force_snapshot = True
                    export_banner_msg = f"GC PURGED {purged_now} WIFI GHOSTS (LORA NODES PROTECTED)"
                    export_banner_until = now_loop + 3.5
                else:
                    set_acq_mode("sweep")
            elif k == pygame.K_x:
                if vi == 1:
                    refresh_idx = (refresh_idx + 1) % len(REFRESH_PRESETS)
                    force_snapshot = True
                else:
                    set_acq_mode("scan")
            elif k == pygame.K_y:
                if vi == 1:
                    sort_idx = (sort_idx + 1) % len(SORT_MODES)
                    force_snapshot = True
                else:
                    ns = 43 if int(st.get("sync", 18)) == 18 else 18
                    st["sync"] = ns
                    cm(f"sync 0x{ns:02X}")
            elif k == pygame.K_SPACE:
                ui_paused = not ui_paused
            elif k == pygame.K_TAB:
                dev_popup_open = False
                vi = (vi + 1) % 5
            elif k in (pygame.K_UP, pygame.K_DOWN):
                dp(0, -1 if k == pygame.K_UP else 1)
            elif k in (pygame.K_LEFT, pygame.K_RIGHT):
                dp(-1 if k == pygame.K_LEFT else 1, 0)

        elif e.type == pygame.JOYBUTTONDOWN:
            b = e.button
            # GO-Super Gamepad: 0:B, 1:A, 2:X, 3:Y, 4:L1, 5:R1, 6:L2, 7:R2, 8..11:DPAD, 12:SELECT, 13:START
            if jy and jy.get_numbuttons() > 5 and jy.get_button(4) and jy.get_button(5):
                rn = False
            elif b in (12, 14, 15, 16):  # SELECT / FN exits
                if dev_popup_open:
                    dev_popup_open = False
                else:
                    rn = False
            elif b == 13:  # START toggles Stream Pause/Freeze
                ui_paused = not ui_paused
            elif b in (4, 6):  # L1 / L2 switches view left
                dev_popup_open = False
                vi = (vi - 1) % 5
            elif b in (5, 7):  # R1 / R2 switches view right
                dev_popup_open = False
                vi = (vi + 1) % 5
            elif b == 8:
                dp(0, -1)
            elif b == 9:
                dp(0, 1)
            elif b == 10:
                dp(-1, 0)
            elif b == 11:
                dp(1, 0)
            elif vi == 1:
                # On DEVICES view: A opens/closes popup, B closes popup or runs Wi-Fi GC Purge, Y cycles Sort, X cycles Refresh Rate
                if b == 1:  # A button
                    if displayed_devices:
                        dev_popup_open = not dev_popup_open
                elif b == 0:  # B button
                    if dev_popup_open:
                        dev_popup_open = False
                    else:
                        purged_now = gc_stale_devices(now_loop, force_aggressive=True)
                        force_snapshot = True
                        export_banner_msg = f"GC PURGED {purged_now} WIFI GHOSTS (LORA NODES PROTECTED)"
                        export_banner_until = now_loop + 3.5
                elif b == 2:  # X button -> Cycle Refresh Rate
                    refresh_idx = (refresh_idx + 1) % len(REFRESH_PRESETS)
                    force_snapshot = True
                elif b == 3:  # Y button -> Cycle Sort Mode
                    sort_idx = (sort_idx + 1) % len(SORT_MODES)
                    force_snapshot = True
            else:
                if b == 0:
                    set_acq_mode("sniff")
                elif b == 1:
                    set_acq_mode("sweep")
                elif b == 2:  # X button -> Export in Dissector/Console, or Scan mode in Spectrum/Tuner
                    if vi in (2, 4):
                        export_all_formats()
                    else:
                        set_acq_mode("scan")
                elif b == 3:  # Y button -> Export in Console/Dissector, or toggle Sync word
                    if vi in (2, 4):
                        export_all_formats()
                    else:
                        ns = 43 if int(st.get("sync", 18)) == 18 else 18
                        st["sync"] = ns
                        cm(f"sync 0x{ns:02X}")

        elif e.type == pygame.JOYHATMOTION:
            dp(e.value[0], -e.value[1])
        elif e.type == pygame.JOYAXISMOTION:
            if now_loop - axis_cd > 0.18:
                if e.axis == 1 and abs(e.value) > 0.6:
                    dp(0, 1 if e.value > 0 else -1)
                    axis_cd = now_loop
                elif e.axis == 0 and abs(e.value) > 0.6:
                    dp(1 if e.value > 0 else -1, 0)
                    axis_cd = now_loop

    sc.fill(BG)

    live_f = float(st.get("live_freq", st.get("freq", 915.0)))
    home_f = float(st.get("freq", 915.0))
    live_bin = nearest_lora_bin(live_f)
    tuned_bin = nearest_lora_bin(home_f)

    # Top Header
    bx(10, 10, 700, 52)
    tx(fh, "STUMP DUAL-BAND RECON", 18, 14, CY)
    if ui_paused:
        bx(275, 13, 112, 22, (120, 28, 45), RO)
        tx(fx, "STREAM PAUSED", 281, 17, TX)
    conn_s = f"ONLINE {sp}" if sp else "AWAITING USB OTG (/dev/ttyUSB0)"
    tx(fx, conn_s, 18, 40, GR if sp else AM)

    sy_v = int(st.get("sync", 18))
    sy_lbl = "RNS(0x12)" if sy_v == 18 else "MESH(0x2B)" if sy_v == 43 else f"0x{sy_v:02X}"
    wf_lbl = f"CH{st['wifich']}" if int(st.get("wifich", 0)) > 0 else ("HOP" if st.get("wifi", True) else "OFF")
    tx(fx, f"SCAN:{live_f:.3f}MHz (HOME:{home_f:.1f}) SF{st['sf']} {str(st['mode']).upper()} {sy_lbl} W:{wf_lbl}", 365, 16, AM if abs(live_f - home_f) > 0.2 else TX)

    lora_dev_cnt = sum(1 for d in displayed_devices if d.get("band") == "SUB-GHZ LORA")
    wifi_dev_cnt = sum(1 for d in displayed_devices if d.get("band") == "2.4GHZ WIFI")
    tx(fx, f"ACTIVE: {lora_dev_cnt} RNS/LORA | {wifi_dev_cnt} WIFI | GC PURGED:{gc_purged_count}", 365, 38, GR)

    # 5 Tactical View Tabs
    TABS = ["1:SPECTRUM", "2:DEVICES", "3:DISSECTOR", "4:TUNER", "5:CONSOLE"]
    for i, v in enumerate(TABS):
        x = 10 + i * 141
        w_tab = 136 if i < 4 else 136
        a = (i == vi)
        bx(x, 68, w_tab, 30, (22, 34, 56) if a else PN, CY if a else BD)
        tx(fx, v, x + 10, 76, CY if a else MU)

    # ==========================================================================
    # VIEW 0: Dual-Band Spectrum & Waterfall
    # ==========================================================================
    if vi == 0:
        bx(10, 106, 700, 58)
        kr = float(lp[0].get("kal", -118.0)) if lp else -118.0
        lr = float(lp[0].get("rssi", -118.0)) if lp else -118.0
        tx(fs, f"LORA PACKET RSSI: {lr:.1f} dBm  (KALMAN SMOOTHED: {kr:.1f} dBm)", 20, 112, AM)
        bx(20, 138, 680, 16, BG, BD)
        fill_w = int(max(0.0, min(1.0, (kr + 125.0) / 80.0)) * 680)
        if fill_w > 0:
            bx(20, 138, fill_w, 16, AM if kr > -85 else CY, None)

        bx(10, 172, 700, 238)
        tx(fs, f"SUB-GHZ 902-928 MHz | LIVE SWEEP: {live_f:.3f} MHz (HOME: {home_f:.3f} MHz)", 20, 180, TX)
        for i, c in enumerate(SCAN_FREQS):
            x = 24 + i * 56
            val = ls[c]
            h = int(max(0.04, min(1.0, (val + 122.0) / 78.0)) * 75)
            is_live_b = (c == live_bin)
            is_home_b = (c == tuned_bin)
            bar_col = RO if val > -75 else AM if val > -95 else (GR if is_live_b else (VI if is_home_b else CY))
            bx(x, 285 - h, 42, h, bar_col, CY if is_live_b else None, 2 if is_live_b else 1)
            if is_live_b:
                tx(fxs, "SCAN", x + 6, 198, GR)
            tx(fx, f"{c}", x + 8, 290, GR if is_live_b else (AM if is_home_b else MU))

        for ri, rw in enumerate(wr[:15]):
            for ci, v in enumerate(rw):
                n = max(0.0, min(1.0, (v + 122.0) / 78.0))
                wcol = (int(n * 245), int(n * 150 + 25), int((1.0 - n) * 185 + 30))
                bx(24 + ci * 56, 312 + ri * 5, 42, 4, wcol, None)

        bx(10, 418, 700, 234)
        tx(fs, "2.4 GHz 802.11 PROMISCUOUS OCCUPANCY (CH 1-13)", 20, 426, CY)
        for c in range(1, 14):
            x = 22 + (c - 1) * 52
            wval = ws[c]
            h = int(max(0.05, min(1.0, (wval + 100.0) / 65.0)) * 85)
            wcol = RO if wval > -58 else AM if wval > -74 else GR
            bx(x, 545 - h, 38, h, wcol, None)
            tx(fx, f"C{c}", x + 6, 550, MU)

        if wf:
            w = wf[0]
            tx(fx, f"LAST 802.11: Ch{w.get('ch')} {w.get('ftype')} {w.get('mac')} ({w.get('oui')}) SSID:\"{w.get('ssid','*')}\" {w.get('rssi')}dBm", 20, 580, AM)
        if lp:
            l = lp[0]
            ds = l.get("ds", {})
            tx(fx, f"LAST LORA  : {float(l.get('freq',915.0)):.3f}MHz SF{l.get('sf')} {ds.get('proto')} <{ds.get('dest','')[:10]}> RSSI:{l.get('rssi')}dBm SNR:{l.get('snr')}dB", 20, 608, VI)

    # ==========================================================================
    # VIEW 1: Discovered Devices Small Cards Grid + Detail Confirmation Pop-up
    # ==========================================================================
    elif vi == 1:
        bx(10, 106, 700, 546)
        # Row 1 Control Bar: Sort By & Refresh Rate + Active LoRa/Wi-Fi Counts
        bx(18, 112, 684, 28, BG, BD)
        tx(fx, f"[Y] SORT:{SORT_MODES[sort_idx]}", 24, 119, CY)
        next_in = max(0.0, ref_sec - (now_loop - last_snapshot_ts)) if ref_sec > 0 else 0.0
        ref_txt = f"[X] REF:{ref_label}" + (f"({next_in:.0f}s)" if ref_sec >= 2.0 else "(LIVE)")
        tx(fx, ref_txt, 195, 119, AM)
        tx(fx, f"[B] WIFI GC({gc_purged_count})", 368, 119, RO)
        tx(fx, f"[A] RNS/LORA:{lora_dev_cnt} WIFI:{wifi_dev_cnt}", 522, 119, GR)

        # Row 2 Live Sub-GHz Sweep & 12-Channel Mini-Spectrum Ribbon
        bx(18, 143, 684, 26, BG, BD)
        tx(fxs, f"SUB-GHZ SWEEP: {live_f:.3f} MHz (HOME {home_f:.3f} MHz SF{st['sf']} {str(st['mode']).upper()})", 24, 150, GR if abs(live_f - home_f) > 0.2 else CY)
        for mi, mc in enumerate(SCAN_FREQS):
            mx = 430 + mi * 22
            mval = ls[mc]
            mh = int(max(0.15, min(1.0, (mval + 122.0) / 78.0)) * 18)
            mcol = GR if mc == live_bin else (AM if mval > -95 else (VI if mc == tuned_bin else CY))
            bx(mx, 166 - mh, 16, mh, mcol, None)

        if not displayed_devices:
            tx(fs, "SCANNING 902-928 MHz (RNS 0x12) & 2.4 GHz WI-FI...", 140, 350, MU)
        else:
            # 2-column x 4-row grid of Small Device Cards (8 cards per page)
            row_idx = dev_cursor_idx // 2
            page_start_row = max(0, min(row_idx - 1, max(0, (len(displayed_devices) - 1) // 2 - 3)))
            start_i = page_start_row * 2
            visible_cards = displayed_devices[start_i:start_i + 8]

            for ci_off, dv in enumerate(visible_cards):
                abs_idx = start_i + ci_off
                col_i = ci_off % 2
                row_i = ci_off // 2
                cx = 18 + col_i * 344
                cy = 174 + row_i * 118
                cw = 336
                ch_h = 110
                is_sel = (abs_idx == dev_cursor_idx)

                bx(cx, cy, cw, ch_h, (26, 42, 68) if is_sel else CD, CY if is_sel else BD, 2 if is_sel else 1)

                # Top row of card: Rank + Name + Type Badge
                dtype = dv.get("dtype", "RF")
                t_col = VI if dtype in ("Reticulum", "Raw LoRa") else GR if dtype == "Meshtastic" else AM if "Heltec" in dtype else CY
                tx(fm, f"#{abs_idx+1} {dv.get('name','Unknown')[:20]}", cx + 10, cy + 7, TX)
                bx(cx + cw - 118, cy + 5, 110, 20, BG, t_col)
                tx(fxs, dtype[:15], cx + cw - 112, cy + 9, t_col)

                # Second row: Proximity label + Est distance + Kalman RSSI
                rk = float(dv.get("rssi_kalman", -110.0))
                p_lbl, p_col = proximity_label(rk)
                dist_m = estimate_distance_m(rk, "LORA" if dv.get("band") == "SUB-GHZ LORA" else "WIFI")
                tx(fx, f"{p_lbl} (~{dist_m}m)", cx + 10, cy + 30, p_col)
                tx(fm, f"{rk:.1f} dBm", cx + cw - 92, cy + 29, AM if rk > -75 else TX)

                # Third row: RSSI Bar
                bx(cx + 10, cy + 51, cw - 20, 9, BG, BD)
                bar_pct = max(0.04, min(1.0, (rk + 115.0) / 75.0))
                bx(cx + 10, cy + 51, int((cw - 20) * bar_pct), 9, p_col, None)

                # Fourth row: Hardware disambiguation metadata (Ferr Hz for LoRa, MAC+Ch for Wi-Fi)
                age_s = max(0.0, now_loop - dv.get("last_seen", now_loop))
                if dv.get("band") == "SUB-GHZ LORA":
                    n_asp = len(dv.get("rns_dests", []))
                    asp_s = f" [{n_asp}H]" if n_asp > 1 else ""
                    meta1 = f"{float(dv.get('freq',915.0)):.3f}MHz SF{dv.get('sf')} SNR:{dv.get('snr'):.1f}dB Ferr:{dv.get('ferr_avg',0):+.0f}Hz{asp_s}"
                else:
                    n_rot = len(dv.get("rotated_macs", [1]))
                    rot_s = f"[LAAx{n_rot}]" if dv.get("laa") and n_rot > 1 else ("[LAA]" if dv.get("laa") else "[OUI]")
                    meta1 = f"Ch{dv.get('ch')} MAC:{dv.get('mac','')} {rot_s}"
                tx(fxs, meta1, cx + 10, cy + 66, MU, 46)
                tx(fxs, f"Pkts:{dv.get('pkts',1)} | Seen:{age_s:.1f}s ago | [A]:Confirm Details", cx + 10, cy + 86, CY if is_sel else MU, 48)

        # ======================================================================
        # POP-UP MODAL: Maximum Device Details for Side-by-Side Confirmation
        # ======================================================================
        if dev_popup_open and displayed_devices and dev_cursor_idx < len(displayed_devices):
            sel_uid = displayed_devices[dev_cursor_idx]["uid"]
            # Pull live record from devices_db so RSSI & sparkline update in real time inside popup!
            with devices_lock:
                live_dv = dict(devices_db.get(sel_uid, displayed_devices[dev_cursor_idx]))

            # Dimmed backdrop + High-contrast Modal Box
            bx(34, 122, 652, 518, (10, 15, 26), CY, 2)
            bx(38, 126, 644, 38, (20, 34, 58), BD)
            tx(fs, f"DEVICE CONFIRMATION DOSSIER: {live_dv.get('name')}", 48, 136, CY)
            tx(fx, "[A/B] Close", 588, 138, AM)

            rk = float(live_dv.get("rssi_kalman", -110.0))
            r_raw = float(live_dv.get("rssi_raw", -110.0))
            p_lbl, p_col = proximity_label(rk)
            dist_m = estimate_distance_m(rk, "LORA" if live_dv.get("band") == "SUB-GHZ LORA" else "WIFI")
            age_s = max(0.0, now_loop - live_dv.get("last_seen", now_loop))

            # Section 1: Live Proximity & RSSI Sparkline (Move device closer to verify!)
            bx(48, 174, 624, 112, PN, BD)
            tx(fm, f"PROXIMITY: {p_lbl}  |  EST. DISTANCE: ~{dist_m} m  |  LIVE RSSI: {r_raw:.1f} dBm (KALMAN: {rk:.1f} dBm)", 58, 182, p_col)
            tx(fx, f"Min RSSI: {live_dv.get('rssi_min',rk):.1f} dBm  |  Peak RSSI: {live_dv.get('rssi_max',rk):.1f} dBm  |  Packets: {live_dv.get('pkts',1)}  |  Cadence: {live_dv.get('avg_int_ms',0)} ms  |  Age: {age_s:.1f}s", 58, 206, TX)

            # Live 18-sample RSSI Sparkline Bar Graph
            bx(58, 228, 604, 48, BG, BD)
            tx(fxs, "LIVE RSSI SPARKLINE (MOVE STUMP CLOSER TO WATCH BARS CLIMB):", 66, 233, MU)
            hist = live_dv.get("history", [rk])
            for hi, hv in enumerate(hist[-18:]):
                hx = 66 + hi * 32
                hpct = max(0.08, min(1.0, (float(hv) + 115.0) / 70.0))
                bh = int(hpct * 26)
                bcol = RO if hv > -60 else AM if hv > -80 else CY
                bx(hx, 272 - bh, 24, bh, bcol, None)

            # Section 2: Hardware Disambiguation Fingerprints (To tell two adjacent STUMPs apart)
            bx(48, 296, 624, 210, PN, BD)
            tx(fs, "HARDWARE & PROTOCOL DISAMBIGUATION FINGERPRINT", 58, 304, AM)
            tx(fx, f"Unique Registry ID : {live_dv.get('uid')}   |   Classification: {live_dv.get('dtype')} ({live_dv.get('band')})", 58, 332, TX)

            if live_dv.get("band") == "SUB-GHZ LORA":
                tx(fm, f"Crystal Freq Offset: {live_dv.get('ferr_avg',0):+.1f} Hz (Latest: {live_dv.get('ferr',0):+.0f} Hz)  <-- UNIQUE PER SX1262 CHIP", 58, 358, GR)
                tx(fx, f"Radio Modulation   : {float(live_dv.get('freq',915.0)):.3f} MHz  |  SF{live_dv.get('sf')}  |  BW {live_dv.get('bw')} kHz  |  CR 4/{live_dv.get('cr')}  |  Sync 0x{int(live_dv.get('sync',18)):02X}", 58, 384, TX)
                tx(fx, f"Link Quality       : SNR {live_dv.get('snr',0):.2f} dB  |  CRC Valid: {'YES' if live_dv.get('crc_ok') else 'RAW'}  |  Last Length: {live_dv.get('last_len',0)} Bytes", 58, 408, CY)
                if live_dv.get("dtype") == "Meshtastic":
                    tx(fx, f"Meshtastic Routing : Sender !{live_dv.get('from_id')} -> Dest !{live_dv.get('to_id')}  |  Hops: {live_dv.get('hops')}  |  ChHash: {live_dv.get('mesh_ch')}", 58, 432, VI)
                else:
                    rns_d = live_dv.get("rns_dests", [])
                    asp_info = f" ({len(rns_d)} Hashes: {','.join(x[:8] for x in rns_d[:3])})" if rns_d else ""
                    tx(fx, f"Reticulum Header   : Type={live_dv.get('ptype')} | Hops={live_dv.get('hops')} | IFAC={'YES' if live_dv.get('ifac') else 'NO'}{asp_info}", 58, 432, VI)
                tx(fxs, f"Last Payload (Hex) : {live_dv.get('payload_hex','')[:64]}", 58, 458, MU)
                tx(fxs, f"Last Payload (ASC) : {live_dv.get('payload_ascii','')}", 58, 480, MU)
            else:
                rot_list = live_dv.get("rotated_macs", [live_dv.get("mac")])
                tx(fm, f"Hardware Source MAC: {live_dv.get('mac')}   |   Vendor OUI: {live_dv.get('oui')}", 58, 358, GR)
                tx(fx, f"Associated BSSID   : {live_dv.get('bssid')}   |   LAA Randomization: {'YES (' + str(len(rot_list)) + ' MACs clustered)' if live_dv.get('laa') else 'NO (Burned OUI MAC)'}", 58, 384, TX)
                tx(fx, f"802.11 Channel/Len : Channel {live_dv.get('ch')} (2.4 GHz)  |  Last Frame: {live_dv.get('ftype')} ({live_dv.get('last_len',0)} Bytes)", 58, 408, CY)
                ft_str = ", ".join(live_dv.get("ftypes_seen", []))
                ss_str = ", ".join(live_dv.get("ssids", [])) or "<Wildcard / Hidden SSID>"
                tx(fx, f"Frame Subtypes Seen: {ft_str}", 58, 434, VI)
                tx(fm, f"Probed/Beacon SSIDs: {ss_str[:52]}", 58, 462, AM)

            # Section 3: Bottom Verification Tip inside Modal
            bx(48, 514, 624, 114, BG, BD)
            tx(fm, "HOW TO CONFIRM WHICH PHYSICAL STUMP / DEVICE THIS IS:", 58, 524, CY)
            tx(fx, "1. Hold your Heltec V3 antenna 5-10 cm from Target A: Live RSSI above will jump to > -45 dBm.", 58, 548, TX)
            tx(fx, "2. For two identical LoRa nodes, compare Crystal Freq Offset (Hz) & RNS/Mesh Node Hash above.", 58, 570, TX)
            tx(fx, "3. Use D-Pad LEFT/RIGHT while this popup is open to step directly to the next device card.", 58, 592, AM)

    # ==========================================================================
    # VIEW 2: Frame Dissector (with Locked Cursor & Full Date + Export!)
    # ==========================================================================
    elif vi == 2:
        bx(10, 106, 700, 546)
        tx(fs, f"FRAME DISSECTOR (D-PAD: LOCK CURSOR | [X/Y]: EXPORT PCAP+CHARLES+JSON)", 20, 114, TX)
        win_start = max(0, min(si - 3, max(0, len(ev) - 8)))
        visible = ev[win_start:win_start + 8]
        for idx_off, it in enumerate(visible):
            abs_i = win_start + idx_off
            y = 144 + idx_off * 32
            is_sel = (abs_i == si)
            bx(20, y, 680, 28, (28, 46, 74) if is_sel else BG, CY if is_sel else None)
            dt_short = str(it.get("dt", ""))[:19]
            if it.get("k") == "W":
                ssid_s = it.get("ssid") or "<Wildcard>"
                line_s = f"{dt_short} [WIFI Ch{it.get('ch'):02d}] {it.get('ftype'):<9} {it.get('mac')} \"{ssid_s[:12]}\" ({it.get('rssi')}dBm)"
                col = CY
            else:
                ds = it.get("ds", {})
                line_s = f"{dt_short} [{ds.get('proto','LORA')[:9]}] {float(it.get('freq',915.0)):.3f}MHz SF{it.get('sf')} {it.get('rssi')}dBm SNR:{it.get('snr')}dB"
                col = VI if "Reticulum" in ds.get("proto", "") else GR
            tx(fxs, line_s, 26, y + 7, col, 90)

        bx(20, 410, 680, 232, BG, BD)
        if ev and si < len(ev):
            o = ev[si]
            dt_full = o.get("dt", fmt_dt())
            if o.get("k") == "W":
                tx(fs, f"LOCKED 802.11 FRAME #{si+1}: {o.get('ftype')} (CH {o.get('ch')})  [{dt_full}]", 32, 420, CY)
                tx(fm, f"Source MAC : {o.get('mac')}  [{o.get('oui')}]", 32, 452, AM)
                tx(fm, f"BSSID      : {o.get('bssid','ff:ff:ff:ff:ff:ff')}  | Length: {o.get('len',0)} Bytes", 32, 480, TX)
                tx(fm, f"Signal RSSI: {o.get('rssi')} dBm  | LAA Randomized Bit: {'YES' if o.get('laa') else 'NO (Global)'}", 32, 508, GR)
                tx(fm, f"SSID Tag 00: \"{o.get('ssid') or '<Wildcard Broadcast>'}\"", 32, 536, VI)
            else:
                ds = o.get("ds", {})
                tx(fs, f"LOCKED LORA PACKET #{si+1}: {ds.get('proto')} ({ds.get('type','PKT')})  [{dt_full}]", 32, 420, VI)
                tx(fm, f"RF Params  : {float(o.get('freq',915.0)):.3f} MHz | BW {o.get('bw')} kHz | SF{o.get('sf')} | CR 4/{o.get('cr')}", 32, 452, TX)
                tx(fm, f"Metrics    : RSSI {o.get('rssi')} dBm | SNR {o.get('snr')} dB | Ferr {o.get('ferr')} Hz", 32, 480, AM)
                if ds.get("proto") == "Meshtastic":
                    tx(fm, f"Mesh Header: From !{ds.get('from')} -> To !{ds.get('to')} | Hops:{ds.get('hops')} Ch:{ds.get('ch')}", 32, 508, GR)
                else:
                    tx(fm, f"RNS Header : Dest <{ds.get('dest','')[:24]}> | Hops:{ds.get('hops')} | IFAC:{ds.get('ifac')}", 32, 508, CY)
                tx(fx, f"Payload Hex: {str(o.get('payload',''))[:66]}", 32, 540, MU)

    # ==========================================================================
    # VIEW 3: D-Pad Interactive Tuner
    # ==========================================================================
    elif vi == 3:
        bx(10, 106, 700, 546)
        tx(fs, f"D-PAD RADIO & WI-FI TUNER (LIVE SWEEP: {live_f:.3f} MHz)", 20, 116, AM)
        sy_v = int(st.get("sync", 18))
        sy_str = "0x12 (Reticulum)" if sy_v == 18 else "0x2B (Meshtastic)" if sy_v == 43 else "0x34 (LoRaWAN)"
        tuner_items = [
            ("LoRa Home Center Freq", f"{float(st.get('freq', 915.0)):.3f} MHz (Live: {live_f:.3f} MHz)"),
            ("LoRa Spreading Factor", f"SF{st.get('sf', 8)}"),
            ("LoRa Bandwidth", f"{st.get('bw', 125.0)} kHz"),
            ("LoRa Coding Rate", f"4/{st.get('cr', 5)}"),
            ("LoRa Sync Word", sy_str),
            ("Acquisition Mode", str(st.get("mode", "sniff")).upper()),
            ("2.4 GHz Wi-Fi Sniffer", "ENABLED (Promiscuous)" if st.get("wifi", True) else "DISABLED"),
            ("2.4 GHz Wi-Fi Channel", "AUTO-HOP (Ch 1-13)" if int(st.get("wifich", 0)) == 0 else f"FIXED CH {st.get('wifich')}")
        ]
        for i, (k, v) in enumerate(tuner_items):
            y = 156 + i * 56
            s = (i == tr)
            bx(24, y, 672, 46, (26, 40, 64) if s else BG, CY if s else BD)
            tx(fm, k, 40, y + 13, TX if s else MU)
            tx(fm, f"<  {v}  >", 330, y + 14, CY if s else AM)

    # ==========================================================================
    # VIEW 4: Dated Console Stream & One-Button Multi-Format Exporter
    # ==========================================================================
    elif vi == 4:
        bx(10, 106, 700, 546)
        tx(fs, f"DATED SERIAL LOG ([Y/X]: EXPORT WIRESHARK .PCAP + CHARLES .CHLSJ + .JSON)", 20, 116, GR)
        for i, s in enumerate(sl[:19]):
            col = CY if "TX>" in s else VI if "LORA" in s else AM if "WIFI" in s else GR if "EXPORTED" in s else TX
            tx(fxs, s, 22, 148 + i * 25, col, 92)

    # Export Confirmation Toast Banner (if recently triggered)
    if now_loop < export_banner_until and export_banner_msg:
        bx(24, 612, 672, 34, (14, 58, 42), GR, 2)
        tx(fm, export_banner_msg, 36, 620, TX, 78)

    # Bottom Controls Bar
    bx(10, 660, 700, 48)
    if vi == 1:
        tx(fx, "[L1/R1]Views [D-PAD]Select Card [A]Open/Close Dossier [Y]Sort [X]Refresh Rate [START]Pause [SEL]Exit", 14, 676, AM)
    elif vi in (2, 4):
        tx(fx, "[L1/R1]Views [D-PAD]Cursor [X/Y]Export Wireshark(.pcap)+Charles(.chlsj)+JSON [START]Pause [SEL]Exit", 14, 676, AM)
    else:
        tx(fx, "[L1/R1]Views [D-PAD]Cursor/Tune [A]Sniff [B]Sweep [X]Scan [Y]RNS/Mesh [START]Pause [SEL]Exit", 14, 676, AM)

    pygame.display.flip()
    if fb and cs:
        cs.blit(sc, (0, 0))
        d = cs.get_buffer().raw
        rw = 720 * (bpp // 8)
        if stw == rw:
            fb[:len(d)] = d
        else:
            for y in range(min(720, fbh)):
                fb[y * stw:y * stw + rw] = d[y * rw:(y + 1) * rw]
    ck.tick(30)

if jsonl_f:
    jsonl_f.close()
if fb:
    fb.close()
pygame.quit()
sys.exit(0)
