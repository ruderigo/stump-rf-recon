#!/usr/bin/env python3
"""
stump_cli.py — Headless CLI reader and JSONL recorder for R36MAX (dArkOS / Debian 13)
"""

import argparse, json, os, sys, threading, time

try:
    import serial
except ImportError:
    sys.exit("pyserial not found. Run: sudo apt install -y python3-serial")

def find_port():
    for p in ("/dev/ttyUSB0", "/dev/ttyACM0", "/dev/ttyUSB1", "/dev/ttyACM1"):
        if os.path.exists(p):
            return p
    return None

def fmt_rx(d):
    tag = "RX " if d.get("crc_ok") else "RX!"
    sync = d.get("sync")
    proto = "RNS " if sync == 18 else "MESH" if sync == 43 else "LORA"
    base = (f"{tag} [{proto}] {d.get('freq'):>7}MHz SF{d.get('sf')} BW{d.get('bw')} "
            f"len={d.get('len','?'):>3} RSSI={d.get('rssi'):>6}dBm "
            f"SNR={d.get('snr'):>5}dB ferr={d.get('ferr','?')}Hz")
    pl = d.get("payload")
    if pl:
        base += f"  pl={pl[:28]}…" if len(pl) > 28 else f"  pl={pl}"
    return base

def main():
    port = find_port()
    if not port:
        sys.exit("No serial port found. Plug in Heltec V3 over USB OTG.")

    logpath = f"/roms/stump/recon_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    os.makedirs(os.path.dirname(logpath) or ".", exist_ok=True)
    logf = open(logpath, "a")
    print(f"[connected to {port} @ 115200, logging to {logpath}]")

    ser = serial.Serial(port, 115200, timeout=1)
    time.sleep(0.2)

    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            logf.write(text + "\n")
            logf.flush()
            try:
                d = json.loads(text)
                if d.get("ev") == "rx":
                    print(fmt_rx(d))
                elif d.get("ev") == "scan":
                    print(f"SCAN {d.get('freq')}MHz  {d.get('rssi')}dBm")
                elif d.get("ev") == "status":
                    print(f"[status] {d}")
            except Exception:
                print("· " + text)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        logf.close()

if __name__ == "__main__":
    main()
