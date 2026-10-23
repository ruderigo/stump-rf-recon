#!/bin/sh
D=/roms/ports/stump-recon;mkdir -p $D;cd $D||exit 1;L=$D/debug.log
echo ark|sudo -S -p "" chmod 666 /dev/ttyUSB* /dev/ttyACM* /dev/fb0 /dev/dri/* /dev/tty1 2>/dev/null||true
printf "\033[?25l">/dev/tty1 2>/dev/null||true
export PYTHONUNBUFFERED=1 PYTHONNOUSERSITE=1 SDL_NOMOUSE=1
P=/home/ark/firefly/venv/bin/python;[ -x $P ]||P=/usr/bin/python3
$P $D/stump_gui.py>$L 2>&1;printf "\033c">/dev/tty1 2>/dev/null||true
