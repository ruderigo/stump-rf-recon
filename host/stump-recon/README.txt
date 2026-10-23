=== STUMP RF RECON FOR R36MAX (dArkOS / Debian 13 arm64) ===

Exact Installation Directory:
Put this whole package directly into /roms/ports/ so the structure is:

  /roms/ports/STUMP-Recon.sh
  /roms/ports/stump-recon/
      ├── stump_gui.py
      ├── stump_cli.py
      └── README.txt

Why you might have seen a black screen before:
1. ArkOS has NO X11 server! Setting DISPLAY=:0 broke SDL2/Pygame. The launcher now correctly unsets DISPLAY and uses KMSDRM/fbcon.
2. In exFAT (/roms), file permissions are not retained. STUMP-Recon.sh is invoked via bash and automatically runs `sudo chmod 666 /dev/ttyUSB*` to allow serial reading.
3. If anything fails, check the debug log at:
   cat /roms/ports/stump-recon/debug.log

Controls (GO-Super Gamepad):
  [A] Sniff Mode (915MHz SF8)
  [B] Sweep SF (SF7-SF12)
  [X] Scan RSSI (902-928MHz)
  [Y] Sync Word Toggle (Reticulum 0x12 <-> Meshtastic 0x2B)
  [SELECT] Exit to EmulationStation
