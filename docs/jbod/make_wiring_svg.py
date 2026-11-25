"""Generates wiring.svg (the JBOD fan controller schematic). Run: python3 make_wiring_svg.py"""
import os

W, H = 1200, 890
C = {"12v": "#d62828", "5v": "#f77f00", "3v3": "#9d4edd", "gnd": "#222", "pwm": "#1d6fd1", "tach": "#2a9d4b",
     "ow": "#b08900", "box": "#f4f4f2", "edge": "#555", "text": "#111", "muted": "#666"}
out = []


def add(s): out.append(s)
def text(x, y, s, size=13, anchor="start", color=None, weight=400):
    add(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" fill="{color or C["text"]}" font-weight="{weight}">{s}</text>')
def box(x, y, w, h, title, sub=None):
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{C["box"]}" stroke="{C["edge"]}" stroke-width="1.5"/>')
    text(x + w / 2, y + 20, title, 14, "middle", weight=700)
    if sub: text(x + w / 2, y + 37, sub, 11.5, "middle", C["muted"])
def wire(pts, color, w=2.5):
    add(f'<polyline points="{" ".join(f"{x},{y}" for x, y in pts)}" fill="none" stroke="{color}" stroke-width="{w}" stroke-linejoin="round" stroke-linecap="round"/>')
def dot(x, y, color): add(f'<circle cx="{x}" cy="{y}" r="4.5" fill="{color}"/>')
def pin(x, y, label, side="left"):
    add(f'<circle cx="{x}" cy="{y}" r="3.5" fill="#fff" stroke="{C["edge"]}" stroke-width="1.5"/>')
    text(x + (8 if side == "left" else -8), y + 4.5, label, 12.5, "start" if side == "left" else "end")
def gnd(x, y):   # ground symbol hanging below (x, y)
    wire([(x, y), (x, y + 12)], C["gnd"], 2)
    for i, half in enumerate((10, 6.5, 3)):
        add(f'<line x1="{x - half}" y1="{y + 12 + i * 4}" x2="{x + half}" y2="{y + 12 + i * 4}" stroke="{C["gnd"]}" stroke-width="2"/>')
def tag(x, y, label, color, side="right"):   # net label flag
    w = 9 + 7.4 * len(label)
    x0 = x if side == "right" else x - w
    add(f'<rect x="{x0}" y="{y - 10}" width="{w}" height="20" rx="4" fill="{color}"/>')
    text(x0 + w / 2, y + 4.5, label, 12, "middle", "#fff", 700)
def resistor(x1, y, x2, label):   # horizontal zig-zag
    mid, n, amp = (x1 + x2) / 2, 6, 6
    pts = [(x1, y), (mid - 18, y)]
    for i in range(n):
        pts.append((mid - 18 + (i + 0.5) * 36 / n, y + (amp if i % 2 else -amp)))
    pts += [(mid + 18, y), (x2, y)]
    wire(pts, C["edge"], 1.8)
    text(mid, y - 10, label, 11.5, "middle", C["muted"])


add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="system-ui,-apple-system,Segoe UI,sans-serif">')
add(f'<rect width="{W}" height="{H}" fill="#fff"/>')
text(24, 34, "JBOD fan controller: wiring", 20, weight=700)
text(24, 56, "Wires of the same colour/label connect. Every ⏚ is the same common ground. Pins are named, not drawn in board order.", 13, color=C["muted"])

# --- 12 V in + buck ---------------------------------------------------------
box(24, 90, 190, 100, "12 V DC jack", "panel 5.5 × 2.1 mm")
text(119, 142, "from a 12 V 2 A brick", 11.5, "middle", C["muted"])
pin(214, 155, "+12 V", "right"); pin(214, 178, "GND", "right")
gnd(214, 178)
wire([(214, 155), (880, 155)], C["12v"], 3)
text(560, 147, "+12 V (fans and buck)", 12, "middle", C["12v"], 700)
dot(300, 155, C["12v"])
box(240, 240, 170, 120, "Buck 12 → 5 V", "MP1584 / LM2596")
text(325, 293, "set to 5.0 V first", 11.5, "middle", C["muted"])
pin(300, 240, "IN+", "left"); pin(255, 360, "IN−", "left"); pin(410, 320, "OUT+ 5 V", "right"); pin(395, 360, "OUT−", "right")
wire([(300, 155), (300, 240)], C["12v"], 3)
gnd(255, 360); gnd(395, 360)
# bulk cap
cx = 760
dot(cx, 155, C["12v"])
wire([(cx, 155), (cx, 186)], C["12v"], 2.5)
add(f'<line x1="{cx - 14}" y1="186" x2="{cx + 14}" y2="186" stroke="{C["edge"]}" stroke-width="2.5"/>')
add(f'<path d="M{cx - 14} 196 Q{cx} 190 {cx + 14} 196" fill="none" stroke="{C["edge"]}" stroke-width="2.5"/>')
text(cx - 22, 186, "470 µF 25 V", 11.5, "end", C["muted"]); text(cx - 22, 200, "(+ side up)", 11.5, "end", C["muted"])
gnd(cx, 196)

# --- ESP32 ------------------------------------------------------------------
ex, ey, ew, eh = 470, 250, 230, 430
box(ex, ey, ew, eh, "ESP32 DevKit", "ESP32-WROOM-32, 30 or 38 pin")
lp = [("5V / VIN", 320), ("GND", 360), ("3V3", 400), ("GPIO4", 460)]
rp = [("GPIO25", 320), ("GPIO26", 360), ("GPIO27", 400), ("GPIO32", 470), ("GPIO33", 510), ("GPIO35", 550), ("GND", 630)]
for n, y in lp: pin(ex, y, n, "left")
for n, y in rp: pin(ex + ew, y, n, "right")
text(ex + ew / 2, ey + eh - 52, "on board: LED = GPIO2", 11.5, "middle", C["muted"])
text(ex + ew / 2, ey + eh - 36, "BOOT button = GPIO0", 11.5, "middle", C["muted"])
text(ex + ew / 2, ey + eh - 20, "(hold 5 s: Wi-Fi setup again)", 11.5, "middle", C["muted"])
wire([(410, 320), (470, 320)], C["5v"], 3)
wire([(ex, 360), (448, 360)], C["gnd"], 2); gnd(448, 360)
wire([(ex, 400), (440, 400)], C["3v3"], 2.5); tag(440, 400, "3V3", C["3v3"], "left")
wire([(ex, 460), (440, 460)], C["ow"], 2.5); tag(440, 460, "1-WIRE", C["ow"], "left")
for k, (n, y) in enumerate(rp[:3]):
    wire([(ex + ew, y), (ex + ew + 30, y)], C["pwm"], 2.5); tag(ex + ew + 30, y, f"PWM{k + 1}", C["pwm"])
for k, (n, y) in enumerate(rp[3:6]):
    wire([(ex + ew, y), (ex + ew + 30, y)], C["tach"], 2.5); tag(ex + ew + 30, y, f"TACH{k + 1}", C["tach"])
wire([(ex + ew, 630), (ex + ew + 22, 630)], C["gnd"], 2); gnd(ex + ew + 22, 630)

# --- fans -------------------------------------------------------------------
fx, fw = 960, 210
wire([(880, 155), (930, 155), (930, 160 + 2 * 220 + 82)], C["12v"], 3)
for k in range(3):
    y0 = 160 + k * 220
    box(fx, y0, fw, 150, f"Fan {k + 1} header", "4-pin PWM, 140 mm fan")
    pins = [("1 GND", y0 + 58), ("2 +12 V", y0 + 82), ("3 TACH", y0 + 106), ("4 PWM", y0 + 130)]
    for n, y in pins: pin(fx, y, n, "left")
    # GND
    wire([(fx, y0 + 58), (fx - 14, y0 + 58)], C["gnd"], 2); gnd(fx - 14, y0 + 58)
    # 12 V from the vertical bus
    dot(930, y0 + 82, C["12v"]) if k < 2 else None
    wire([(930, y0 + 82), (fx, y0 + 82)], C["12v"], 3)
    # TACH with pull-up to 3V3
    wire([(fx, y0 + 106), (900, y0 + 106)], C["tach"], 2.5)
    dot(900, y0 + 106, C["tach"])
    tag(862, y0 + 106, f"TACH{k + 1}", C["tach"], "left")
    wire([(862, y0 + 106), (900, y0 + 106)], C["tach"], 2.5)
    wire([(900, y0 + 106), (900, y0 + 92)], C["edge"], 1.8)
    # vertical resistor up to a 3V3 tag
    pts = [(900, y0 + 92)]
    for i in range(6):
        pts.append((900 + (6 if i % 2 else -6), y0 + 92 - (i + 0.5) * 4))
    pts += [(900, y0 + 68), (900, y0 + 60)]
    wire(pts, C["edge"], 1.8)
    text(889, y0 + 84, "10k", 11.5, "end", C["muted"])
    wire([(900, y0 + 60), (880, y0 + 60)], C["3v3"], 2.5); tag(880, y0 + 60, "3V3", C["3v3"], "left")
    # PWM
    tag(862, y0 + 130, f"PWM{k + 1}", C["pwm"], "left")
    wire([(862, y0 + 130), (fx, y0 + 130)], C["pwm"], 2.5)
text(fx + fw / 2, 160 + 2 * 220 + 176, "Pull tach up to 3.3 V only, never 5 or 12 V.", 12, "middle", C["12v"], 700)

# --- DS18B20 ----------------------------------------------------------------
box(24, 500, 250, 130, "DS18B20 probe (optional)", "waterproof, taped in the JBOD exhaust")
pin(274, 550, "red VDD", "right"); pin(274, 578, "yellow DQ", "right"); pin(274, 606, "black GND", "right")
wire([(274, 550), (320, 550)], C["3v3"], 2.5); tag(320, 550, "3V3", C["3v3"])
wire([(274, 578), (380, 578)], C["ow"], 2.5); tag(380, 578, "1-WIRE", C["ow"])
wire([(274, 606), (296, 606)], C["gnd"], 2); gnd(296, 606)
dot(350, 578, C["ow"])
wire([(350, 578), (350, 602)], C["edge"], 1.8)
pts = [(350, 602)]
for i in range(6): pts.append((350 + (6 if i % 2 else -6), 602 + (i + 0.5) * 4))
pts += [(350, 626), (350, 634)]
wire(pts, C["edge"], 1.8)
text(362, 620, "4.7k", 11.5, color=C["muted"])
wire([(350, 634), (350, 646)], C["3v3"], 2.5); tag(336, 656, "3V3", C["3v3"])

# --- legend -----------------------------------------------------------------
lx, ly = 24, 730
text(lx, ly, "Legend", 14, weight=700)
for i, (k, name) in enumerate([("12v", "+12 V"), ("5v", "+5 V"), ("3v3", "3.3 V"), ("gnd", "ground"),
                               ("pwm", "PWM (25 kHz)"), ("tach", "tach"), ("ow", "1-Wire")]):
    x = lx + (i % 4) * 150; y = ly + 26 + (i // 4) * 24
    wire([(x, y - 4), (x + 30, y - 4)], C[k], 3); text(x + 38, y, name, 12.5)
text(lx, ly + 90, "Fan header, looking at the pins: 1 GND (black) · 2 +12 V (yellow/red) · 3 tach (green) · 4 PWM (blue); pin 1 is at the end next to the key.", 12.5, color=C["muted"])
text(lx, ly + 110, "PWM straight from the GPIO suits most fans. If a fan ignores it, drive PWM through a 2N7000 (open drain) and set PWM_INVERTED 1.", 12.5, color=C["muted"])
text(lx, ly + 130, "Set the buck to 5.0 V with a multimeter BEFORE connecting the ESP32.", 12.5, color=C["12v"], weight=700)
add("</svg>")
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiring.svg"), "w").write("\n".join(out))
print("wrote wiring.svg")
