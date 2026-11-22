#!/bin/sh
# Export every printable part to stl/ (each already in its print orientation).
cd "$(dirname "$0")"
mkdir -p stl
for p in duct_left duct_center duct_right tray_left tray_center tray_right shelf splice pin_test; do
  echo "== $p"
  openscad -D "part=\"$p\"" -o "stl/$p.stl" jbod_hanger.scad 2>&1 | grep -iE "warning|error|Simple:" || true
done
