// CPU temperature lookup and heat colour, shared by the extension and its tests (no Shell imports here).
import GLib from 'gi://GLib';

const HWMON = '/sys/class/hwmon';
// Settings the control panel writes for the display daemon; the top bar shows the same sensor.
export const DROPIN = '/etc/systemd/system/AorusWaterForce360-linux.service.d/ui.conf';
export const DEFAULT_SENSOR = 'Tccd1';

// green → yellow → orange → red, °C
const STOPS = [
    [40, [87, 227, 137]],
    [60, [246, 211, 45]],
    [75, [255, 163, 72]],
    [90, [246, 97, 81]],
];

function readText(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? new TextDecoder().decode(bytes).trim() : null;
    } catch {
        return null;
    }
}

/** CSS colour for a temperature, interpolated between STOPS. */
export function tempColor(celsius) {
    const first = STOPS.at(0), last = STOPS.at(-1);
    if (celsius <= first[0])
        return `rgb(${first[1].join(', ')})`;
    if (celsius >= last[0])
        return `rgb(${last[1].join(', ')})`;
    const i = STOPS.findIndex(([t]) => t > celsius);
    const [t0, c0] = STOPS.at(i - 1), [t1, c1] = STOPS.at(i);
    const k = (celsius - t0) / (t1 - t0);
    return `rgb(${c0.map((v, n) => Math.round(v + (c1.at(n) - v) * k)).join(', ')})`;
}

/** AWF_SENSOR from the drop-in text, or the default. */
export function parseSensor(dropinText) {
    const m = /AWF_SENSOR=([A-Za-z0-9]+)/.exec(dropinText ?? '');
    return m ? m[1] : DEFAULT_SENSOR;
}

export function configuredSensor() {
    return parseSensor(readText(DROPIN));
}

/** tempN_input of the k10temp/coretemp sensor with this label; the first one if the label is missing. */
export function findSensorPath(label) {
    let fallback = null;
    const dir = GLib.Dir.open(HWMON, 0);
    for (let d = dir.read_name(); d !== null; d = dir.read_name()) {
        const base = `${HWMON}/${d}`;
        if (!['k10temp', 'coretemp'].includes(readText(`${base}/name`)))
            continue;
        const files = GLib.Dir.open(base, 0);
        const names = [];
        for (let f = files.read_name(); f !== null; f = files.read_name())
            names.push(f);
        files.close();
        for (const f of names.filter(n => /^temp\d+_label$/.test(n)).sort()) {
            const input = `${base}/${f.replace('_label', '_input')}`;
            fallback ??= input;
            if (readText(`${base}/${f}`) === label) {
                dir.close();
                return input;
            }
        }
    }
    dir.close();
    return fallback;
}

/** °C or null. */
export function readTemp(path) {
    const v = path ? readText(path) : null;
    return v ? Number.parseInt(v, 10) / 1000 : null;
}
