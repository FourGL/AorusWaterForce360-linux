// WaterForce 360 CPU temperature in the GNOME top bar, coloured by heat.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

import {configuredSensor, findSensorPath, readTemp, tempColor} from './temp.js';

const APP_ID = 'com.github.fourgl.awf360.desktop';
const REFRESH_SECONDS = 2;

const TempIndicator = GObject.registerClass(
class TempIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'WaterForce 360 CPU temperature', true);
        this._label = new St.Label({
            text: '—°C',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'awf360-temp-label',
        });
        this.add_child(this._label);
        this._sensor = null;
        this._path = null;
        this._refresh();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, REFRESH_SECONDS, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _refresh() {
        // the sensor can change from the control panel, so follow the drop-in
        const sensor = configuredSensor();
        if (sensor !== this._sensor || !this._path) {
            this._sensor = sensor;
            this._path = findSensorPath(sensor);
        }
        const t = readTemp(this._path);
        if (t === null) {
            this._label.text = '—°C';
            this._label.style = null;
            this._path = null;
            return;
        }
        this._label.text = `${Math.round(t)}°C`;
        this._label.style = `color: ${tempColor(t)};`;
    }

    vfunc_event(event) {
        // no menu: a click opens the control panel
        const type = event.type();
        if (type === Clutter.EventType.BUTTON_RELEASE || type === Clutter.EventType.TOUCH_END) {
            Shell.AppSystem.get_default().lookup_app(APP_ID)?.activate();
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    destroy() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        super.destroy();
    }
});

export default class WaterForceTempExtension extends Extension {
    enable() {
        this._indicator = new TempIndicator();
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
