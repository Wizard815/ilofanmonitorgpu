#!/bin/sh
# Previews (PNG) of the assembly, closed and open.
cd "$(dirname "$0")"
mkdir -p img
for v in closed open; do
  o=$([ $v = open ] && echo true || echo false)
  xvfb-run -a openscad -D "open_view=$o" --imgsize=1400,1000 \
    --camera=0,60,70,64,0,208,1250 --colorscheme=Tomorrow -o img/assembly_$v.png jbod_hanger.scad
  xvfb-run -a openscad -D "open_view=$o" --imgsize=1400,1000 \
    --camera=0,-20,90,90,0,90,560 --projection=o --colorscheme=Tomorrow -o img/side_$v.png jbod_hanger.scad
done
