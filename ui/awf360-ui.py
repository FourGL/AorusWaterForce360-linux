#!/usr/bin/env python3
"""WaterForce 360 — CPU monitor and control panel for the AorusWaterForce360-linux service."""

import os
import subprocess
import sys
import threading
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

APP_ID = "com.github.fourgl.awf360"
UNIT = "AorusWaterForce360-linux"
CTL = "/usr/local/bin/awf360-ctl"
USB_ID = ("1044", "7a4d")
EXT_UUID = "awf360-temp@fourgl.github.com"
DEFAULT_SENSOR, DEFAULT_INTERVAL = "Tccd1", 5
HISTORY = 120  # seconds of graph
# RPM ranges must match the daemon (main.go) and the root helper (awf360-ctl)
FAN_RANGE, PUMP_RANGE, RPM_STEP = (750, 2750), (1600, 3200), 50
MODES = [("factory", "Factory profile"), ("fixed", "Fixed speed"), ("curve", "Curve")]
DEFAULT_CURVES = {
    "fan": ((30, 800), (50, 1200), (70, 2000), (85, 2750)),
    "pump": ((30, 1800), (50, 2200), (70, 2800), (85, 3200)),
}
CURVE_TEMPS = (20, 100)  # °C shown on the curve editor's x axis


# ---------- system readers (no root needed) ----------

def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def find_cpu_sensors():
    """label -> tempN_input path of the k10temp/coretemp hwmon, same lookup as the daemon."""
    base = "/sys/class/hwmon"
    sensors = {}
    for d in sorted(os.listdir(base)):
        if read(f"{base}/{d}/name") not in ("k10temp", "coretemp"):
            continue
        for f in sorted(os.listdir(f"{base}/{d}")):
            if f.startswith("temp") and f.endswith("_label"):
                sensors[read(f"{base}/{d}/{f}")] = f"{base}/{d}/{f.replace('_label', '_input')}"
    return sensors


def read_temp(path):
    v = read(path)
    return int(v) / 1000 if v else None


def cpu_mhz():
    """First "cpu MHz" line — exactly what the daemon sends to the display."""
    for line in read("/proc/cpuinfo").splitlines():
        if line.startswith("cpu MHz"):
            return float(line.split(":", 1)[1])
    return 0.0


def cpu_times():
    parts = read("/proc/stat").splitlines()[0].split()[1:]
    vals = [int(v) for v in parts]
    return vals[3], sum(vals)


def read_cooler():
    """Readings of the gigabyte_waterforce hwmon: [(key, title, text)], None if the driver isn't bound.
    Each read makes the driver query the cooler over USB, so call it off the UI thread."""
    base = "/sys/class/hwmon"
    for d in os.listdir(base):
        if read(f"{base}/{d}/name") != "waterforce":
            continue
        p = f"{base}/{d}"
        files = sorted(os.listdir(p))
        rows = []
        for f in files:
            if f.startswith("fan") and f.endswith("_input"):
                n = f[3:-6]
                v = read(f"{p}/{f}")
                rows.append((f, read(f"{p}/fan{n}_label") or f"Fan {n}", f"{v} RPM" if v else "—"))
        for f in files:
            if f.startswith("pwm") and f[3:].isdigit():
                n = f[3:]
                v = read(f"{p}/{f}")
                title = (read(f"{p}/fan{n}_label") or f"Fan {n}") + " duty"
                rows.append((f, title, f"{round(int(v) * 100 / 255)}%" if v else "—"))
        for f in files:
            if f.startswith("temp") and f.endswith("_input"):
                n = f[4:-6]
                t = read_temp(f"{p}/{f}")
                rows.append((f, read(f"{p}/temp{n}_label") or f"Temperature {n}",
                             "—" if t is None else f"{t:.1f} °C"))
        return rows
    return None


def device_connected():
    base = "/sys/bus/usb/devices"
    for d in os.listdir(base):
        if (read(f"{base}/{d}/idVendor"), read(f"{base}/{d}/idProduct")) == USB_ID:
            return True
    return False


def systemctl(*args):
    r = subprocess.run(["systemctl", *args, UNIT], capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def service_config():
    env = dict(
        kv.split("=", 1) for kv in systemctl("show", "-p", "Environment", "--value").split() if "=" in kv
    )
    sensor = env.get("AWF_SENSOR") or DEFAULT_SENSOR
    try:
        interval = int(env.get("AWF_INTERVAL", DEFAULT_INTERVAL))
    except ValueError:
        interval = DEFAULT_INTERVAL
    fan = channel_config(env, "fan", FAN_RANGE)
    pump = channel_config(env, "pump", PUMP_RANGE)
    return (sensor, interval), (fan, pump)


def format_curve(curve):
    return ",".join(f"{t}:{r}" for t, r in curve)


def parse_curve(text, rpm_range):
    """4 "temp:rpm" points, temperatures 0..100 strictly rising, RPM in range — as the daemon checks."""
    try:
        curve = tuple(tuple(int(v) for v in point.split(":")) for point in text.split(","))
    except ValueError:
        return None
    lo, hi = rpm_range
    if len(curve) != 4 or any(len(p) != 2 or not 0 <= p[0] <= 100 or not lo <= p[1] <= hi for p in curve):
        return None
    if any(a[0] >= b[0] for a, b in zip(curve, curve[1:])):
        return None
    return curve


def channel_config(env, name, rpm_range):
    """(mode, fixed rpm, curve) of one channel from the drop-in; older drop-ins only have the RPM."""
    prefix = f"AWF_{name.upper()}"
    lo, hi = rpm_range
    try:
        rpm = int(env.get(f"{prefix}_RPM", 0))
    except ValueError:
        rpm = 0
    mode = env.get(f"{prefix}_MODE") or ("fixed" if rpm else "factory")
    curve = parse_curve(env.get(f"{prefix}_CURVE", ""), rpm_range) or DEFAULT_CURVES[name]
    if not lo <= rpm <= hi:
        rpm = (lo + hi) // 2
    return mode, rpm, curve


def describe(cfg):
    mode, rpm, curve = cfg
    if mode == "fixed":
        return f"{rpm} RPM"
    if mode == "curve":
        return f"curve {curve[0][1]}–{curve[-1][1]} RPM"
    return "factory profile"


class TopBarExtension:
    """The GNOME Shell extension that shows the CPU temperature in the top bar.

    Turning it on/off edits org.gnome.shell enabled-extensions, which GNOME Shell watches;
    whether the running Shell has loaded it comes from its Extensions D-Bus service."""

    # ExtensionState in GNOME Shell 45+: 1 = active, 2 = inactive, 3 = error, 4 = out of date
    ACTIVE, INACTIVE = 1, 2

    def __init__(self):
        source = Gio.SettingsSchemaSource.get_default()
        self.settings = (Gio.Settings.new("org.gnome.shell")
                         if source and source.lookup("org.gnome.shell", True) else None)

    def installed(self):
        dirs = [GLib.get_user_data_dir(), *GLib.get_system_data_dirs()]
        return any(os.path.isfile(f"{d}/gnome-shell/extensions/{EXT_UUID}/metadata.json") for d in dirs)

    def enabled(self):
        return bool(self.settings) and EXT_UUID in self.settings.get_strv("enabled-extensions")

    def set_enabled(self, on):
        enabled = [u for u in self.settings.get_strv("enabled-extensions") if u != EXT_UUID]
        disabled = [u for u in self.settings.get_strv("disabled-extensions") if u != EXT_UUID]
        self.settings.set_strv("enabled-extensions", enabled + [EXT_UUID] if on else enabled)
        self.settings.set_strv("disabled-extensions", disabled if on else disabled + [EXT_UUID])
        Gio.Settings.sync()

    def user_extensions_off(self):
        return bool(self.settings) and self.settings.get_boolean("disable-user-extensions")

    def shell_state(self):
        """State in the running Shell, or None when it has not loaded the extension (or no Shell)."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            info = bus.call_sync("org.gnome.Shell.Extensions", "/org/gnome/Shell/Extensions",
                                 "org.gnome.Shell.Extensions", "GetExtensionInfo",
                                 GLib.Variant("(s)", (EXT_UUID,)), GLib.VariantType("(a{sv})"),
                                 Gio.DBusCallFlags.NONE, 1000, None).unpack()[0]
        except GLib.Error:
            return None
        return int(info["state"]) if "state" in info else None


# ---------- UI ----------

class StatCard(Gtk.Box):
    def __init__(self, caption):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("card")
        self.set_hexpand(True)
        self.value = Gtk.Label(label="—", css_classes=["title-1", "numeric"])
        self.value.set_margin_top(14)
        self.caption = Gtk.Label(label=caption, css_classes=["dim-label", "caption"])
        self.caption.set_margin_bottom(14)
        self.append(self.value)
        self.append(self.caption)

    def set_level(self, level):
        for c in ("success", "warning", "error"):
            self.value.remove_css_class(c)
        if level:
            self.value.add_css_class(level)


class CurveEditor(Gtk.DrawingArea):
    """4-point fan/pump curve: CPU temperature (x) -> RPM (y). Drag a point to move it."""

    PAD_L, PAD_R, PAD_T, PAD_B = 46, 14, 14, 26
    HIT = 18  # px around a point that grabs it

    def __init__(self, rpm_range, curve, on_change):
        super().__init__()
        self.lo, self.hi = rpm_range
        self.points = [list(p) for p in curve]
        self.on_change = on_change
        self.current_temp = None
        self.active = None
        self.hover = None
        self.set_content_height(230)
        self.set_hexpand(True)
        self.set_draw_func(self.draw)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        motion.connect("leave", lambda *_: self.set_hover(None))
        self.add_controller(motion)

    def curve(self):
        return tuple(tuple(p) for p in self.points)

    # --- geometry ---

    def plot_size(self):
        return (self.get_width() - self.PAD_L - self.PAD_R, self.get_height() - self.PAD_T - self.PAD_B)

    def to_xy(self, temp, rpm):
        w, h = self.plot_size()
        t0, t1 = CURVE_TEMPS
        x = self.PAD_L + w * (min(max(temp, t0), t1) - t0) / (t1 - t0)
        y = self.PAD_T + h * (1 - (rpm - self.lo) / (self.hi - self.lo))
        return x, y

    def from_xy(self, x, y):
        w, h = self.plot_size()
        t0, t1 = CURVE_TEMPS
        temp = t0 + (x - self.PAD_L) / w * (t1 - t0)
        rpm = self.lo + (1 - (y - self.PAD_T) / h) * (self.hi - self.lo)
        return temp, rpm

    def rpm_at(self, temp):
        """RPM the cooler targets at this temperature: linear between points, flat outside."""
        pts = self.points
        if temp <= pts[0][0]:
            return pts[0][1]
        for (ta, ra), (tb, rb) in zip(pts, pts[1:]):
            if temp <= tb:
                return ra + (rb - ra) * (temp - ta) / (tb - ta)
        return pts[-1][1]

    def nearest(self, x, y):
        best, best_d = None, self.HIT
        for i, (t, r) in enumerate(self.points):
            px, py = self.to_xy(t, r)
            d = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
            if d <= best_d:
                best, best_d = i, d
        return best

    # --- drawing ---

    def draw(self, _area, cr, width, height):
        fg = self.get_color()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        w, h = self.plot_size()
        t0, t1 = CURVE_TEMPS
        cr.set_font_size(10.5)
        cr.set_line_width(1)

        for t in range(t0, t1 + 1, 10):  # temperature grid
            x, _ = self.to_xy(t, self.lo)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
            cr.move_to(x, self.PAD_T)
            cr.line_to(x, self.PAD_T + h)
            cr.stroke()
            if t % 20 == 0:
                label = f"{t}°C"
                ext = cr.text_extents(label)
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
                cr.move_to(x - ext.width / 2, height - 8)
                cr.show_text(label)
        step = 500
        for rpm in range((self.lo + step - 1) // step * step, self.hi + 1, step):  # RPM grid
            _, y = self.to_xy(t0, rpm)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
            cr.move_to(self.PAD_L, y)
            cr.line_to(self.PAD_L + w, y)
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.6)
            ext = cr.text_extents(str(rpm))
            cr.move_to(self.PAD_L - 6 - ext.width, y + 4)
            cr.show_text(str(rpm))

        # the curve, flat before the first and after the last point
        line = [self.to_xy(t0, self.points[0][1])] + [self.to_xy(t, r) for t, r in self.points] \
            + [self.to_xy(t1, self.points[-1][1])]
        cr.move_to(line[0][0], self.PAD_T + h)
        for x, y in line:
            cr.line_to(x, y)
        cr.line_to(line[-1][0], self.PAD_T + h)
        cr.close_path()
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 0.12)
        cr.fill()
        cr.set_line_width(2)
        cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
        cr.move_to(*line[0])
        for x, y in line[1:]:
            cr.line_to(x, y)
        cr.stroke()

        # where the cooler is now: current CPU temperature on the curve
        if self.current_temp is not None:
            temp = self.current_temp
            x, y = self.to_xy(temp, self.rpm_at(temp))
            cr.set_line_width(1)
            cr.set_dash([4, 4])
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.move_to(x, self.PAD_T)
            cr.line_to(x, self.PAD_T + h)
            cr.stroke()
            cr.set_dash([])
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
            cr.arc(x, y, 4, 0, 6.2832)
            cr.fill()
            label = f"now {temp:.0f}°C · {self.rpm_at(temp):.0f} RPM"
            ext = cr.text_extents(label)
            lx = min(max(x + 6, self.PAD_L + 4), self.PAD_L + w - ext.width - 4)
            cr.move_to(lx, self.PAD_T + 12)
            cr.show_text(label)

        for i, (t, r) in enumerate(self.points):
            x, y = self.to_xy(t, r)
            big = i in (self.active, self.hover)
            cr.set_source_rgba(accent.red, accent.green, accent.blue, 1)
            cr.arc(x, y, 8 if big else 6, 0, 6.2832)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.arc(x, y, 2.5, 0, 6.2832)
            cr.fill()
            if big:
                label = f"{t}°C · {r} RPM"
                ext = cr.text_extents(label)
                lx = min(max(x - ext.width / 2, self.PAD_L), self.PAD_L + w - ext.width)
                ly = y - 14 if y - 14 > self.PAD_T + 24 else y + 24
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 1)
                cr.move_to(lx, ly)
                cr.show_text(label)

    # --- interaction ---

    def set_hover(self, idx):
        if idx != self.hover:
            self.hover = idx
            self.set_cursor_from_name("grab" if idx is not None else None)
            self.queue_draw()

    def on_motion(self, _ctl, x, y):
        if self.active is None:
            self.set_hover(self.nearest(x, y))

    def on_drag_begin(self, gesture, x, y):
        self.active = self.nearest(x, y)
        if self.active is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        self.drag_start = self.to_xy(*self.points[self.active])
        self.before = self.curve()
        self.set_cursor_from_name("grabbing")

    def on_drag_update(self, _gesture, dx, dy):
        if self.active is None:
            return
        i = self.active
        temp, rpm = self.from_xy(self.drag_start[0] + dx, self.drag_start[1] + dy)
        # keep temperatures rising: at least 1 °C between neighbours
        t_min = self.points[i - 1][0] + 1 if i > 0 else CURVE_TEMPS[0]
        t_max = self.points[i + 1][0] - 1 if i < len(self.points) - 1 else CURVE_TEMPS[1]
        self.points[i] = [int(min(max(round(temp), t_min), t_max)),
                          int(min(max(round(rpm / RPM_STEP) * RPM_STEP, self.lo), self.hi))]
        self.queue_draw()

    def on_drag_end(self, _gesture, _dx, _dy):
        if self.active is None:
            return
        self.active = None
        self.set_cursor_from_name("grab")
        self.queue_draw()
        if self.curve() != self.before:
            self.on_change()


class CoolingControl:
    """Mode (factory / fixed / curve) of one channel, with an RPM slider and a curve editor."""

    def __init__(self, group, name, rpm_range, cfg, on_change):
        lo, hi = rpm_range
        mode, rpm, curve = cfg
        self.on_change = on_change
        self.mode_row = Adw.ComboRow(title=f"{name} mode", model=Gtk.StringList.new([t for _, t in MODES]))
        self.mode_row.set_selected(next((i for i, (k, _) in enumerate(MODES) if k == mode), 0))
        group.add(self.mode_row)

        self.fixed_row = Gtk.ListBoxRow(activatable=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                      margin_top=10, margin_bottom=6, margin_start=12, margin_end=12)
        head = Gtk.Box()
        head.append(Gtk.Label(label=f"{name} speed", xalign=0, hexpand=True))
        self.value = Gtk.Label(css_classes=["numeric", "dim-label"])
        head.append(self.value)
        box.append(head)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, RPM_STEP)
        self.scale.set_draw_value(False)
        for mark in (lo, (lo + hi) // 2, hi):
            self.scale.add_mark(mark, Gtk.PositionType.BOTTOM, f"{mark}")
        self.scale.set_value(rpm)
        box.append(self.scale)
        self.fixed_row.set_child(box)
        group.add(self.fixed_row)

        self.curve_row = Gtk.ListBoxRow(activatable=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        box.append(Gtk.Label(label=f"{name} curve", xalign=0))
        self.hint = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        box.append(self.hint)
        self.editor = CurveEditor(rpm_range, curve, self.changed_now)
        box.append(self.editor)
        self.curve_row.set_child(box)
        group.add(self.curve_row)

        self.timer = 0
        self.refresh()
        self.mode_row.connect("notify::selected", self.changed_now)
        self.scale.connect("value-changed", self.changed_later)

    def mode(self):
        return MODES[self.mode_row.get_selected()][0]

    def config(self):
        return (self.mode(), int(round(self.scale.get_value() / RPM_STEP) * RPM_STEP), self.editor.curve())

    def set_current(self, temp, sensor):
        self.editor.current_temp = temp
        self.hint.set_label(f"CPU temperature ({sensor}) → RPM. The cooler follows the curve on its own, "
                            "linearly between points. Drag a point to move it.")
        self.editor.queue_draw()

    def refresh(self):
        self.fixed_row.set_visible(self.mode() == "fixed")
        self.curve_row.set_visible(self.mode() == "curve")
        self.value.set_label(f"{self.config()[1]} RPM")

    def changed_now(self, *_):
        self.timer = 0
        self.refresh()
        self.on_change()
        return False

    def changed_later(self, *_):
        self.refresh()
        # a drag fires many events: apply once the slider rests
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(600, self.changed_now)


class Graph(Gtk.DrawingArea):
    """Temperature line over a filled CPU-load area, last HISTORY seconds."""

    def __init__(self, temps, loads):
        super().__init__()
        self.temps, self.loads = temps, loads
        self.set_content_height(160)
        self.set_hexpand(True)
        self.set_draw_func(self.draw)

    def draw(self, _area, cr, w, h):
        fg = self.get_color()
        pad_l, pad_r, pad_t, pad_b = 34, 8, 8, 18
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        lo, hi = 20, 100  # °C / % axis

        def y(v):
            return pad_t + gh * (1 - (min(max(v, lo), hi) - lo) / (hi - lo))

        def x(i):
            return pad_l + gw * i / (HISTORY - 1)

        cr.select_font_face("Sans")
        cr.set_font_size(10)
        cr.set_line_width(1)
        for v in (40, 60, 80, 100):
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
            cr.move_to(pad_l, y(v))
            cr.line_to(w - pad_r, y(v))
            cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(4, y(v) + 3)
            cr.show_text(f"{v}°")
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
        cr.move_to(pad_l, h - 4)
        cr.show_text(f"−{HISTORY // 60} min")
        cr.move_to(w - pad_r - 24, h - 4)
        cr.show_text("now")

        offset = HISTORY - len(self.loads)
        if self.loads:  # load: filled area, % mapped onto the same axis
            cr.set_source_rgba(0.40, 0.55, 0.95, 0.22)
            cr.move_to(x(offset), pad_t + gh)
            for i, v in enumerate(self.loads):
                cr.line_to(x(offset + i), pad_t + gh * (1 - v / 100))
            cr.line_to(x(offset + len(self.loads) - 1), pad_t + gh)
            cr.close_path()
            cr.fill()

        pts = [(i, t) for i, t in enumerate(self.temps) if t is not None]
        if len(pts) > 1:
            cr.set_source_rgba(0.93, 0.33, 0.23, 1)
            cr.set_line_width(2)
            cr.move_to(x(offset + pts[0][0]), y(pts[0][1]))
            for i, t in pts[1:]:
                cr.line_to(x(offset + i), y(t))
            cr.stroke()


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="WaterForce 360", default_width=520, default_height=820)
        self.sensors = find_cpu_sensors()
        self.temps = deque(maxlen=HISTORY)
        self.loads = deque(maxlen=HISTORY)
        self.prev_cpu = cpu_times()
        self.syncing = False
        self.applied, self.cooling = service_config()

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.scroll = scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=640)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(16)
        clamp.set_child(box)
        scroll.set_child(clamp)
        view.set_content(scroll)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # stats
        stats = Gtk.Box(spacing=10, homogeneous=True)
        self.temp_card = StatCard("Temperature")
        self.freq_card = StatCard("Frequency")
        self.load_card = StatCard("Load")
        for c in (self.temp_card, self.freq_card, self.load_card):
            stats.append(c)
        box.append(stats)

        # graph
        graph_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"])
        self.graph = Graph(self.temps, self.loads)
        for side in ("top", "bottom", "start", "end"):
            getattr(self.graph, f"set_margin_{side}")(10)
        graph_card.append(self.graph)
        legend = Gtk.Label(
            use_markup=True, css_classes=["caption"], margin_bottom=10,
            label='<span foreground="#ed553b">━━</span> temperature   '
                  '<span foreground="#6b8cf2">▇▇</span> load, %',
        )
        graph_card.append(legend)
        box.append(graph_card)

        # all sensors
        sensors_group = Adw.PreferencesGroup(title="CPU sensors")
        self.sensor_rows = {}
        for label in self.sensors:
            row = Adw.ActionRow(title=label)
            value = Gtk.Label(css_classes=["numeric"])
            row.add_suffix(value)
            sensors_group.add(row)
            self.sensor_rows[label] = value
        if not self.sensors:
            sensors_group.add(Adw.ActionRow(title="k10temp/coretemp not found"))
        box.append(sensors_group)

        # cooler readings from the kernel driver
        self.cooler_group = Adw.PreferencesGroup(title="Cooler")
        self.cooler_rows = {}
        self.cooler_missing = Adw.ActionRow(
            title="No readings",
            subtitle="The waterforce kernel driver isn't bound to the cooler. "
                     "Reinstall the service (ui/install.sh) or replug the cooler.")
        self.cooler_group.add(self.cooler_missing)
        box.append(self.cooler_group)
        self.cooler_polling = False

        # fan / pump speed, sent to the cooler by the display service when it starts
        control = Adw.PreferencesGroup(
            title="Cooling control",
            description="Factory profile: the cooler's own setting. Fixed speed: one RPM. Curve: the "
                        "cooler changes speed itself by the CPU temperature the display service sends. "
                        "Reverse-engineered commands, verified on this cooler; the service sends them "
                        "when it starts, so it has to be running.")
        fan_cfg, pump_cfg = self.cooling
        self.fan = CoolingControl(control, "Fan", FAN_RANGE, fan_cfg, self.on_cooling_changed)
        self.pump = CoolingControl(control, "Pump", PUMP_RANGE, pump_cfg, self.on_cooling_changed)
        box.append(control)

        # display service
        group = Adw.PreferencesGroup(title="Cooler display")
        self.apply_btn = Gtk.Button(label="Apply", css_classes=["suggested-action", "pill"],
                                    valign=Gtk.Align.CENTER, sensitive=False)
        self.apply_btn.connect("clicked", self.on_apply)
        group.set_header_suffix(self.apply_btn)

        self.device_row = Adw.ActionRow(title="Device")
        group.add(self.device_row)

        self.run_row = Adw.SwitchRow(title="Show data on display")
        self.run_row.connect("notify::active", self.on_run_toggled)
        group.add(self.run_row)

        self.boot_row = Adw.SwitchRow(title="Start on boot")
        self.boot_row.connect("notify::active", self.on_boot_toggled)
        group.add(self.boot_row)

        labels = list(self.sensors) or [DEFAULT_SENSOR]
        if self.applied[0] not in labels:
            labels.append(self.applied[0])
        self.sensor_labels = labels
        self.sensor_row = Adw.ComboRow(title="Display sensor", model=Gtk.StringList.new(labels))
        self.sensor_row.set_selected(labels.index(self.applied[0]))
        self.sensor_row.connect("notify::selected", self.on_setting_changed)
        group.add(self.sensor_row)

        self.interval_row = Adw.SpinRow.new_with_range(1, 60, 1)
        self.interval_row.set_title("Display refresh, seconds")
        self.interval_row.set_value(self.applied[1])
        self.interval_row.connect("notify::value", self.on_setting_changed)
        group.add(self.interval_row)
        box.append(group)

        # GNOME top bar indicator (a Shell extension)
        self.topbar = TopBarExtension()
        topbar_group = Adw.PreferencesGroup(title="Top bar")
        self.topbar_row = Adw.SwitchRow(title="CPU temperature in the top bar")
        self.topbar_row.connect("notify::active", self.on_topbar_toggled)
        topbar_group.add(self.topbar_row)
        box.append(topbar_group)
        if self.topbar.settings:
            self.topbar.settings.connect("changed::enabled-extensions", lambda *_: self.sync_topbar())

        self.tick()
        self.poll_service()
        GLib.timeout_add_seconds(1, self.tick)
        GLib.timeout_add_seconds(2, self.poll_service)

    # --- periodic updates ---

    def tick(self):
        idle, total = cpu_times()
        d_total = total - self.prev_cpu[1]
        load = 100 * (1 - (idle - self.prev_cpu[0]) / d_total) if d_total else 0
        self.prev_cpu = (idle, total)

        values = {label: read_temp(p) for label, p in self.sensors.items()}
        for label, v in values.items():
            self.sensor_rows[label].set_label("—" if v is None else f"{v:.1f} °C")

        sensor = self.applied[0]
        temp = values.get(sensor)
        self.temp_card.caption.set_label(f"Temperature · {sensor}")
        if temp is None:
            self.temp_card.value.set_label("—")
            self.temp_card.set_level(None)
        else:
            self.temp_card.value.set_label(f"{temp:.0f}°")
            self.temp_card.set_level("success" if temp < 60 else "warning" if temp < 80 else "error")

        mhz = cpu_mhz()
        self.freq_card.value.set_label(f"{mhz / 1000:.1f} GHz")
        self.load_card.value.set_label(f"{load:.0f}%")

        self.fan.set_current(temp, sensor)
        self.pump.set_current(temp, sensor)

        self.temps.append(temp)
        self.loads.append(load)
        self.graph.queue_draw()
        return True

    def poll_service(self):
        connected = device_connected()
        self.device_row.set_subtitle("Connected (1044:7a4d)" if connected else "Not found on USB")
        active = systemctl("is-active")
        enabled = systemctl("is-enabled") == "enabled"
        self.syncing = True
        self.run_row.set_active(active == "active")
        self.boot_row.set_active(enabled)
        self.syncing = False
        self.run_row.set_subtitle({
            "active": "Service running",
            "inactive": "Service stopped",
            "failed": "Service failed — see journalctl -u " + UNIT,
            "activating": "Starting…",
        }.get(active, active))
        self.poll_cooler()
        self.sync_topbar()
        return True

    def sync_topbar(self):
        ext = self.topbar
        self.syncing = True
        self.topbar_row.set_active(ext.enabled())
        self.syncing = False
        state = ext.shell_state() if ext.settings else None
        if not ext.settings:
            subtitle, usable = "Needs GNOME Shell", False
        elif not ext.installed():
            subtitle, usable = "Extension not installed: run ui/install.sh", False
        elif ext.user_extensions_off():
            subtitle, usable = "Extensions are turned off in GNOME (Extensions app)", True
        elif state is None:
            subtitle, usable = "Log out and back in once so GNOME Shell loads the extension", True
        elif state == ext.ACTIVE:
            subtitle, usable = "Green to red by heat, same sensor as the display. Click it to open this window", True
        elif state == ext.INACTIVE:
            subtitle, usable = "Colored from green to red by heat, same sensor as the display", True
        else:
            subtitle, usable = "The extension failed to load: see journalctl --user -b | grep awf360", True
        self.topbar_row.set_subtitle(subtitle)
        self.topbar_row.set_sensitive(usable)
        return False

    def poll_cooler(self):
        if self.cooler_polling:
            return
        self.cooler_polling = True

        def work():
            rows = read_cooler()
            GLib.idle_add(self.show_cooler, rows)

        threading.Thread(target=work, daemon=True).start()

    def show_cooler(self, rows):
        self.cooler_polling = False
        rows = rows or []
        keys = {key for key, _, _ in rows}
        for key in list(self.cooler_rows):
            if key not in keys:
                self.cooler_group.remove(self.cooler_rows.pop(key)[0])
        for key, title, text in rows:
            if key not in self.cooler_rows:
                row = Adw.ActionRow(title=title)
                value = Gtk.Label(css_classes=["numeric"])
                row.add_suffix(value)
                self.cooler_group.add(row)
                self.cooler_rows[key] = (row, value)
            self.cooler_rows[key][1].set_label(text)
        self.cooler_missing.set_visible(not rows)
        return False

    # --- controls ---

    def selected_settings(self):
        return self.sensor_labels[self.sensor_row.get_selected()], int(self.interval_row.get_value())

    def on_setting_changed(self, *_):
        self.apply_btn.set_sensitive(self.selected_settings() != self.applied)

    def on_apply(self, _btn):
        sensor, interval = self.selected_settings()

        def done(ok):
            if ok:
                self.applied = (sensor, interval)
                self.on_setting_changed()
                self.toast("Settings applied")

        self.ctl(self.apply_args(sensor, interval, self.cooling), done)

    @staticmethod
    def apply_args(sensor, interval, cooling):
        args = ["apply", sensor, str(interval)]
        for mode, rpm, curve in cooling:
            args += [mode, str(rpm), format_curve(curve)]
        return args

    def on_cooling_changed(self):
        wanted = (self.fan.config(), self.pump.config())
        if wanted == self.cooling:
            return
        sensor, interval = self.applied

        def done(ok):
            if ok:
                self.cooling = wanted
                self.toast(f"Fan: {describe(wanted[0])}, pump: {describe(wanted[1])}")

        self.ctl(self.apply_args(sensor, interval, wanted), done)

    def on_topbar_toggled(self, row, _pspec):
        if not self.syncing and row.get_active() != self.topbar.enabled():
            self.topbar.set_enabled(row.get_active())
            GLib.timeout_add(500, self.sync_topbar)  # the Shell needs a moment to (un)load it

    def on_run_toggled(self, row, _pspec):
        if not self.syncing:
            self.ctl(["start" if row.get_active() else "stop"])

    def on_boot_toggled(self, row, _pspec):
        if not self.syncing:
            self.ctl(["enable" if row.get_active() else "disable"])

    def ctl(self, args, done=None):
        """Run the root helper via pkexec without blocking the UI."""
        try:
            proc = Gio.Subprocess.new(["pkexec", CTL, *args],
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
        except GLib.Error as e:
            self.toast(f"Could not run pkexec: {e.message}")
            return

        def finished(p, res):
            _, out, _ = p.communicate_utf8_finish(res)
            ok = p.get_successful()
            if not ok:
                self.toast((out or "").strip().splitlines()[-1:][0] if out and out.strip()
                           else "Action cancelled or failed")
            if done:
                done(ok)
            self.poll_service()

        proc.communicate_utf8_async(None, None, finished)

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))


class App(Adw.Application):
    def __init__(self, screenshot=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE if screenshot else 0)
        self.screenshot = screenshot

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()
        if self.screenshot:
            win.set_default_size(520, 1640)  # tall enough to capture every section
            GLib.timeout_add(4000, self.save_screenshot, win)

    def save_screenshot(self, win):
        if not getattr(self, "scrolled", False):  # AWF_SHOT_SCROLL=bottom captures the lower half
            adj = win.scroll.get_vadjustment()
            adj.set_value(adj.get_upper() if os.environ.get("AWF_SHOT_SCROLL") == "bottom" else 0)
            self.scrolled = True
            GLib.timeout_add(500, self.save_screenshot, win)
            return False
        paintable = Gtk.WidgetPaintable(widget=win)
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, win.get_width(), win.get_height())
        node = snap.to_node()
        if node is None:  # not drawn yet
            GLib.timeout_add(500, self.save_screenshot, win)
            return False
        win.get_renderer().render_texture(node, None).save_to_png(self.screenshot)
        self.quit()
        return False


if __name__ == "__main__":
    shot = None
    if len(sys.argv) > 2 and sys.argv[1] == "--screenshot":
        shot = sys.argv[2]
    sys.exit(App(shot).run([sys.argv[0]]))
