#!/bin/bash
# Read-only survey of the Unraid host for the JBOD fan build. Changes nothing.
# Run on the Unraid box (terminal or SSH) and paste the whole output back:
#   bash jbod_survey.sh 2>&1 | tee /tmp/jbod_survey.txt
# Run it once now, and again with one D3600 fan module pulled (see CHECKLIST.md).

sec() { printf '\n===== %s =====\n' "$1"; }

sec "Unraid"
cat /etc/unraid-version 2>/dev/null
uname -r
date

sec "Disk temp refresh (Settings > Disk Settings > Tunable poll_attributes; want 60)"
grep -h poll_attributes /boot/config/disk.cfg 2>/dev/null || echo "poll_attributes not set (Unraid default, 1800 s)"
echo -n "display unit: "; grep -h '^unit=' /boot/config/plugins/dynamix/dynamix.cfg 2>/dev/null || echo "(default C)"

sec "Storage controllers"
lspci -nnk 2>/dev/null | grep -iA3 -E 'sas|raid|scsi|sata|nvme' | grep -vE '^--$'

sec "SCSI hosts (driver per host)"
for h in /sys/class/scsi_host/host*; do [ -e "$h" ] || continue; echo "$(basename "$h"): $(cat "$h/proc_name" 2>/dev/null)"; done

sec "Block devices: where each disk hangs (expander = behind the JBOD's SAS expander)"
for b in /sys/block/sd* /sys/block/nvme*n1; do
  [ -e "$b" ] || continue
  d=$(basename "$b"); p=$(readlink -f "$b")
  host=$(echo "$p" | grep -o 'host[0-9]*' | head -1)
  exp=no; echo "$p" | grep -q '/expander-' && exp=YES
  model=$(cat "$b/device/model" 2>/dev/null | xargs)
  size=$(( $(cat "$b/size" 2>/dev/null || echo 0) / 2 / 1024 / 1024 ))
  printf '%-8s %-6s expander=%-3s rot=%s %6sG  %s\n' "$d" "$host" "$exp" "$(cat "$b/queue/rotational" 2>/dev/null)" "$size" "$model"
done

sec "Unraid disks.ini (name / device / temp / rotational)"
awk -F'"' 'function out(){ if (n != "") printf "%-10s dev=%-9s temp=%-4s rot=%s\n", n, d, t, r }
  /^\["/ { out(); n=$2; d=""; t=""; r="" } /^device=/ {d=$2} /^temp=/ {t=$2} /^rotational=/ {r=$2}
  END { out() }' /var/local/emhttp/disks.ini 2>/dev/null || echo "no /var/local/emhttp/disks.ini"

sec "SES enclosures (the D3600 should show up here)"
found=0
for e in /sys/class/enclosure/*; do
  [ -e "$e" ] || continue; found=1
  dev="$e/device"
  echo "-- $(basename "$e"): $(cat "$dev/vendor" 2>/dev/null | xargs) $(cat "$dev/model" 2>/dev/null | xargs) rev $(cat "$dev/rev" 2>/dev/null | xargs), $(cat "$e/components" 2>/dev/null) components"
  for c in "$e"/*/; do
    [ -f "$c/type" ] || continue
    t=$(cat "$c/type"); case "$t" in "array device"|"device") continue;; esac
    printf '   %-28s %-22s status=%s fault=%s\n' "$(basename "$c")" "$t" "$(cat "$c/status" 2>/dev/null)" "$(cat "$c/fault" 2>/dev/null)"
  done
  sg=$(ls "$dev/scsi_generic" 2>/dev/null | head -1)
  if [ -n "$sg" ] && command -v sg_ses >/dev/null; then
    echo "   -- sg_ses /dev/$sg (fans, temps, PSUs):"
    sg_ses --page=2 "/dev/$sg" 2>/dev/null | grep -iE -A3 'cooling|temperature|power supply|Element type' | sed 's/^/      /' | head -120
  elif [ -n "$sg" ]; then
    echo "   (install sg3_utils from the Nerd Tools/Un-get plugin for fan speeds: sg_ses --page=2 /dev/$sg)"
  fi
done
[ $found = 1 ] || echo "none -- the controller may hide the enclosure (RAID mode), or the ses module isn't loaded"

sec "Docker container"
docker ps --format '{{.Names}}  {{.Status}}' 2>/dev/null | grep -i fan
echo; echo "done"
