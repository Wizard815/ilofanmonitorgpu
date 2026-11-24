// JBOD fan hanger: a 1U rack tray (ESP32 + power) with a hinged 3 x 140 mm
// fan duct that hangs in front of a 2U 12-bay JBOD and blows into its drive
// bays. Gravity holds the duct's foam-lined rim against the JBOD's front; lift
// it on the hinge (and pin it) to get at the drives.
//
// Render one part:   openscad -D 'part="duct_left"' -o duct_left.stl jbod_hanger.scad
// Or all of them:    ./export.sh
// Preview:           part="assembly" (open_view=true shows it swung up)
//
// Coordinates: x across the rack (0 = centre), y out of the rack toward you
// (0 = front face of the rack posts), z up (0 = bottom of the JBOD's 2U).
// All sizes mm.

/* [What to render] */
part = "assembly"; // [assembly, fit_gauge, duct_view, duct_left, duct_center, duct_right, tray_left, tray_center, tray_right, shelf, splice, pin_test]
open_view = false;

/* [Measure these on your rack] */
// How far the JBOD's drive handles/latches stand out from the rack-post face, plus ~5 mm of foam
rim_y = 30;
// Drive-bay opening on the JBOD front, bottom and top (z, from the bottom of its 2U)
bay_z0 = 2;
bay_z1 = 87;
// U height of the JBOD under the tray
jbod_u = 2;

/* [Fans and duct] */
fan = 140;
fan_t = 25;
fan_hole_pitch = 124.5;   // 140 mm fan screw spacing
fan_hole_d = 4.5;
fan_bore = 136;
module_w = 142;           // one fan per module, three modules side by side
// Fan tilt: top of the fan leans toward the rack so it blows down-and-in
tilt = 20;
// Top edge of the fan face (y, z)
fan_top_y = 55;
fan_top_z = 135;
wall = 2.6;
face_t = 3;
rim_flange = 5;           // foam lands on this
rim_t = 4;

/* [Hinge] */
hinge_y = 40;
hinge_z = 118;
hinge_d = 6.5;            // M6 bolt
pin_d = 4.3;              // M4 bolt / 4 mm rod as the lock pin
pin_r = 13;               // lock-pin radius around the hinge
ear_r = 20;
ear_t = 5.5;              // on the outer ducts
arm_t = 6;                // on the tray
open_angle = 75;          // swung-up position the pin locks at

/* [Rack tray] */
U = 44.45;
panel_w = 482.6;
rack_hole_x = 465.1 / 2;
panel_t = 4;
lip = 6;
shelf_w = 100;
shelf_d = 110;
pcb = [70, 90];           // perfboard carrying ESP32, buck, fan headers
pcb_hole_inset = 3;
dc_jack_d = 11;           // panel-mount 5.5x2.1 barrel jack (DC-022B)
bed = 180;                // your printer's smaller bed side; parts are checked against it

$fn = 64;
eps = 0.01;

tray_z0 = jbod_u * U + 0.4;
tray_z1 = (jbod_u + 1) * U - 0.4;
seg_x = module_w / 2;     // tray splits at the duct seams
H = [hinge_y, hinge_z];
P = [hinge_y, hinge_z + pin_r];                      // pin hole in the duct ear (closed)
function rot2(p, a) = [H[0] + (p[0] - H[0]) * cos(a) - (p[1] - H[1]) * sin(a),
                       H[1] + (p[0] - H[0]) * sin(a) + (p[1] - H[1]) * cos(a)];
B = rot2(P, open_angle);                             // tray hole that locks it open
outer_x = 1.5 * module_w;                            // outer side of the duct row

// ---------------------------------------------------------------- sanity checks
// A bad measurement stops the export here instead of wasting filament.
assert(module_w >= fan, "module_w must be at least the fan size");
assert(bay_z1 > bay_z0 + 40, "bay_z0 / bay_z1 look wrong: measure the drive-bay opening height");
assert(rim_y >= panel_t + lip + 3, "rim_y too small: the duct rim would hit the tray's front lips");
assert(rim_y <= hinge_y - 6, "rim_y too large for this hinge: raise hinge_y (and fan_top_y) together with it");
assert(outer_x + ear_t + 0.5 + arm_t <= rack_hole_x - 6, "duct row too wide: the hinge arm would sit under the rack screw heads");
assert(pin_r + pin_d / 2 + 2 <= ear_r && pin_r - pin_d / 2 - hinge_d / 2 >= 2, "lock pin does not fit around the hinge bolt");
assert(bay_z1 + rim_flange < hinge_z - ear_r + 25, "hinge sits too low for this bay height");
assert(module_w + ear_t <= bed && panel_w / 2 - seg_x <= bed, "a part is larger than the bed setting");
fan_bottom = [fan_top_y + fan * sin(tilt), fan_top_z - fan * cos(tilt)];
assert(fan_bottom[1] > bay_z0 - 25, "the fan bottom hangs far below the JBOD: lower tilt or raise fan_top_z");
// How far the swung-up duct reaches into the U above the tray (fan's top outer corner).
open_top = rot2([fan_top_y + fan_t * cos(tilt), fan_top_z + fan_t * sin(tilt)], open_angle);
echo(str("open: fan top reaches ", round(open_top[1] - (jbod_u + 1) * U), " mm into the U above, ",
         round(open_top[0]), " mm in front of the posts; duct sticks out ",
         round(fan_bottom[0] + fan_t * cos(tilt)), " mm when closed"));

// ---------------------------------------------------------------- helpers
// Local fan-face frame: u = x, v = down the face, n = out of the face.
module face_frame() {
    multmatrix([[1, 0, 0, 0],
                [0, sin(tilt), cos(tilt), fan_top_y],
                [0, -cos(tilt), sin(tilt), fan_top_z],
                [0, 0, 0, 1]]) children();
}
function face_pt(v, n) = [fan_top_y + v * sin(tilt) + n * cos(tilt), fan_top_z - v * cos(tilt) + n * sin(tilt)];

// 2D (y, z) shape -> a plate of thickness t at x = x0..x0+t
module yz_plate(x0, t) { translate([x0, 0, 0]) rotate([90, 0, 90]) linear_extrude(t) children(); }

// Hole along x with a 45° roof toward +y, so it prints clean with y up.
module teardrop_x(yz, d, len = 200) {
    translate([0, yz[0], yz[1]]) rotate([90, 0, 90]) linear_extrude(len, center = true)
        hull() { circle(d = d); translate([d * 0.7071, 0]) square(0.01, center = true); }
}

// ---------------------------------------------------------------- duct
module duct_shell() {
    w = module_w;
    difference() {
        union() {
            hull() {
                translate([-w / 2, rim_y, bay_z0 - wall]) cube([w, eps, bay_z1 - bay_z0 + 2 * wall]);
                face_frame() translate([-w / 2, 0, -eps]) cube([w, fan, eps]);
            }
            // rim flange the foam sits on
            translate([-w / 2, rim_y, bay_z0 - rim_flange]) cube([w, rim_t, bay_z1 - bay_z0 + 2 * rim_flange]);
        }
        hull() {
            translate([-w / 2 + wall, rim_y - 1, bay_z0]) cube([w - 2 * wall, eps, bay_z1 - bay_z0]);
            face_frame() translate([-w / 2 + wall, wall, -face_t]) cube([w - 2 * wall, fan - 2 * wall, eps]);
        }
        translate([-w / 2 + wall, rim_y - 1, bay_z0]) cube([w - 2 * wall, rim_t + 1.5, bay_z1 - bay_z0]);
        // fan bore + screw holes through the face plate
        face_frame() {
            translate([0, fan / 2, -face_t - 1]) cylinder(d = fan_bore, h = face_t + 2);
            for (a = [-1, 1], b = [-1, 1])
                translate([a * fan_hole_pitch / 2, fan / 2 + b * fan_hole_pitch / 2, -face_t - 1]) cylinder(d = fan_hole_d, h = face_t + 2);
        }
        // bolts joining neighbouring ducts (through both side walls)
        for (p = join_pts()) teardrop_x(p, 4.4, w + 2);
    }
}
function join_pts() = [[rim_y + 9, bay_z0 + 10], [rim_y + 9, bay_z1 - 8], [face_pt(118, -10)[0] - 6, face_pt(118, -10)[1]], [face_pt(45, -12)[0], face_pt(45, -12)[1]]];

// Hinge ear on the outer side of the outer ducts (side = -1 left, +1 right).
module duct_ear(side) {
    x0 = side > 0 ? module_w / 2 - eps : -module_w / 2 - ear_t + eps;
    difference() {
        intersection() {
            yz_plate(x0, ear_t) hull() {
                translate(H) circle(r = ear_r);
                translate([rim_y, bay_z1 - 6]) circle(r = 2);
                translate(face_pt(4, -4)) circle(r = 2);
                translate(face_pt(40, -6)) circle(r = 2);
            }
            translate([-500, rim_y, -100]) cube([1000, 500, 500]);   // flat on the bed with the rim
        }
        teardrop_x(H, hinge_d);
        teardrop_x(P, pin_d);
    }
}

module duct(side = 0) {
    duct_shell();
    if (side != 0) duct_ear(side);
}

// ---------------------------------------------------------------- tray
module rack_slot(x, z) {
    translate([x, -1, z]) rotate([-90, 0, 0]) hull() for (dx = [-2, 2]) translate([dx, 0, 0]) cylinder(d = 6.6, h = panel_t + 2);
}

module tray_arm(side) {
    x0 = side > 0 ? outer_x + ear_t + 0.5 : -outer_x - ear_t - 0.5 - arm_t;
    difference() {
        yz_plate(x0, arm_t) hull() {
            translate(H) circle(r = ear_r);
            translate([panel_t - eps, tray_z0 + 3]) square([eps, tray_z1 - tray_z0 - 6]);
        }
        teardrop_x(H, hinge_d);
        teardrop_x(P, pin_d);     // pin here = locked down (transport)
        teardrop_x(B, pin_d);     // pin here = locked up
    }
}

// Bolt holes shared by the faceplate segments and the parts that bolt to them
splice_holes = [for (s = [-1, 1], dx = [-15, 15]) [s * seg_x + dx, (tray_z0 + tray_z1) / 2]];
shelf_holes = [for (dx = [-40, 40], dz = [-12, 12]) [dx, (tray_z0 + tray_z1) / 2 + dz]];

module tray_segment(which) {   // which: -1 left, 0 center, 1 right
    x0 = which == 0 ? -seg_x : which < 0 ? -panel_w / 2 : seg_x;
    x1 = which == 0 ? seg_x : which < 0 ? -seg_x : panel_w / 2;
    lip_x0 = max(x0, -225);
    lip_x1 = min(x1, 225);
    difference() {
        union() {
            translate([x0, 0, tray_z0]) cube([x1 - x0, panel_t, tray_z1 - tray_z0]);
            for (z = [tray_z0, tray_z1 - 3]) translate([lip_x0, 0, z]) cube([lip_x1 - lip_x0, panel_t + lip, 3]);
            if (which != 0) tray_arm(which);
        }
        if (which != 0)
            for (dz = [6.35, 22.225, 38.1]) rack_slot(which * rack_hole_x, jbod_u * U + dz);
        for (h = concat(splice_holes, which == 0 ? shelf_holes : []))
            translate([h[0], -1, h[1]]) rotate([-90, 0, 0]) cylinder(d = 4.4, h = panel_t + 2);
        if (which == 0)   // fan cables out to the duct
            translate([0, -1, tray_z1 - 12]) rotate([-90, 0, 0]) hull() for (dx = [-18, 18]) translate([dx, 0, 0]) cylinder(d = 10, h = panel_t + 2);
    }
}

// Flat bar across each faceplate seam, on the back.
module splice() {
    difference() {
        translate([seg_x - 25, -4, tray_z0 + 6]) cube([50, 4, tray_z1 - tray_z0 - 12]);
        for (h = splice_holes) if (h[0] > 0) translate([h[0], -5, h[1]]) rotate([-90, 0, 0]) cylinder(d = 4.4, h = 6);
    }
}

// Electronics shelf: bolts to the back of the centre segment, rests on the JBOD.
module shelf() {
    fz = tray_z0;
    fl = 3;
    fh = tray_z1 - tray_z0 - 2;
    py = -15 - pcb[1] / 2;
    difference() {
        union() {
            translate([-shelf_w / 2, -4, fz]) cube([shelf_w, 4, fh]);                               // flange
            translate([-shelf_w / 2, -4 - shelf_d, fz]) cube([shelf_w, shelf_d, fl]);               // floor
            translate([-shelf_w / 2, -4 - shelf_d, fz]) cube([shelf_w, 4, 26]);                     // rear lip
            for (x = [-shelf_w / 2, shelf_w / 2 - 3]) translate([x, 0, 0])                          // gussets
                yz_plate(0, 3) polygon([[-4, fz], [-4, fz + fh], [-4 - 40, fz]]);
            for (a = [-1, 1], b = [-1, 1])
                translate([a * (pcb[0] / 2 - pcb_hole_inset), py + b * (pcb[1] / 2 - pcb_hole_inset), fz]) cylinder(d = 7, h = fl + 6);
        }
        for (h = shelf_holes) translate([h[0], -5, h[1]]) rotate([-90, 0, 0]) cylinder(d = 4.4, h = 6);
        for (a = [-1, 1], b = [-1, 1])
            translate([a * (pcb[0] / 2 - pcb_hole_inset), py + b * (pcb[1] / 2 - pcb_hole_inset), fz + 1]) cylinder(d = 2.6, h = 20);
        translate([30, -2 - shelf_d, fz + 15]) rotate([90, 0, 0]) cylinder(d = dc_jack_d, h = 10, center = true);   // 12 V in
        for (x = [-30, -10]) translate([x, -2 - shelf_d, fz + 14]) cube([12, 10, 6], center = true);               // zip-tie / cable slots
        for (y = [-20, -95]) translate([-shelf_w / 2 + 8, y, fz - 1]) cube([4, 10, 10]);
    }
}

// ---------------------------------------------------------------- assembly
module fan_dummy() {
    face_frame() translate([-fan / 2, 0, 0]) difference() {
        cube([fan, fan, fan_t]);
        translate([fan / 2, fan / 2, -1]) cylinder(d = fan_bore, h = fan_t + 2);
    }
}

module ducts_placed() {
    for (i = [-1, 0, 1]) translate([i * module_w, 0, 0]) {
        color("SteelBlue") duct(i);
        color("#333") fan_dummy();
    }
}

module assembly() {
    // context: rack posts, the JBOD, a 1U neighbour above
    color("Gainsboro", 0.5) translate([-panel_w / 2, -420, 0]) cube([panel_w, 420 + rim_y - 8, jbod_u * U]);   // JBOD (drive handles at rim_y - foam)
    color("DimGray", 0.6) for (s = [-1, 1]) translate([s * 233 - 8, -30, -20]) cube([16, 30, 4 * U + 40]);   // rack posts
    color("Orange") for (w = [-1, 0, 1]) tray_segment(w);
    color("Gold") shelf();
    color("Gold") { splice(); mirror([1, 0, 0]) splice(); }
    if (open_view)
        translate([0, H[0], H[1]]) rotate([open_angle, 0, 0]) translate([0, -H[0], -H[1]]) ducts_placed();
    else
        ducts_placed();
}

// ---------------------------------------------------------------- fit gauge
// Full-size side profile (y-z) of tray, hinge, duct and fan, 3 mm thick.
// Hold it upright beside a rack post at the JBOD: the flat back edge on the
// post's front face, the three notches on the rack holes of the U above the
// JBOD. Then check the rim line lands ~5 mm in front of the drive handles and
// the two rim notches line up with the top and bottom of the drive bays.
module gauge_outline(t = 3) { difference() { children(); offset(delta = -t) children(); } }
module duct_profile() {
    hull() {
        translate([rim_y, bay_z0 - wall]) square([eps, bay_z1 - bay_z0 + 2 * wall]);
        polygon([face_pt(0, 0), face_pt(fan, 0), face_pt(fan, -eps), face_pt(0, -eps)]);
    }
    translate([rim_y, bay_z0 - rim_flange]) square([rim_t, bay_z1 - bay_z0 + 2 * rim_flange]);
}
module fit_gauge() {
    linear_extrude(3) difference() {
        union() {
            gauge_outline() duct_profile();
            gauge_outline() polygon([face_pt(0, 0), face_pt(fan, 0), face_pt(fan, fan_t), face_pt(0, fan_t)]);
            hull() { translate(H) circle(r = ear_r); translate([panel_t - eps, tray_z0 + 3]) square([eps, tray_z1 - tray_z0 - 6]); }
            translate([0, tray_z0]) square([panel_t + lip, tray_z1 - tray_z0]);       // faceplate + lips
            translate([0, bay_z0 - 8]) square([4, tray_z1 - bay_z0 + 8]);              // back edge: sits on the post face
            translate([0, bay_z0 - 8]) square([rim_y + rim_t, 4]);                    // ties the bottom together
        }
        translate(H) circle(d = hinge_d);
        translate(P) circle(d = pin_d);
        translate(B) circle(d = pin_d);
        for (dz = [6.35, 22.225, 38.1]) translate([-eps, jbod_u * U + dz]) polygon([[0, -2], [3, 0], [0, 2]]);   // rack-hole notches
        for (z = [bay_z0, bay_z1]) translate([rim_y + rim_t + eps, z]) polygon([[0, -2], [-3, 0], [0, 2]]);     // bay top/bottom
    }
}

// ---------------------------------------------------------------- print layouts
// Each part is laid flat in its print orientation.
module print_duct(side) { rotate([90, 0, 0]) translate([0, -rim_y, 0]) duct(side); }            // rim on the bed
module print_tray(which) { rotate([90, 0, 0]) tray_segment(which); }                           // back on the bed
module print_shelf() { translate([0, 0, -tray_z0]) shelf(); }

if (part == "assembly") assembly();
else if (part == "fit_gauge") fit_gauge();
else if (part == "duct_view") { color("SteelBlue") duct(1); }
else if (part == "duct_left") print_duct(-1);
else if (part == "duct_center") print_duct(0);
else if (part == "duct_right") print_duct(1);
else if (part == "tray_left") print_tray(-1);
else if (part == "tray_center") print_tray(0);
else if (part == "tray_right") print_tray(1);
else if (part == "shelf") print_shelf();
else if (part == "splice") rotate([-90, 0, 0]) splice();
else if (part == "pin_test") {  // hinge fit test: the tray arm and a duct ear, flat, ~20 g
    rotate([0, -90, 0]) translate([-outer_x - ear_t - 0.5, 0, 0]) tray_arm(1);
    translate([0, 90, 0]) rotate([0, -90, 0]) translate([-outer_x, 0, 0])
        intersection() { translate([module_w, 0, 0]) duct_ear(1); translate([outer_x, -100, -100]) cube([50, 400, 400]); }
}
