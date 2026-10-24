import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

const BUS_NAME = 'io.github.galaxybook.Settings1';
const OBJECT_PATH = '/io/github/galaxybook/Settings1';
const APP_ID = 'io.github.galaxybook.Settings.desktop';
const PROTECTED_LIMIT = 80;

const SettingsIface = `
<node>
  <interface name="io.github.galaxybook.Settings1">
    <property name="Features" type="as" access="read"/>
    <property name="ChargeEndThreshold" type="u" access="read"/>
    <property name="UsbCharging" type="b" access="read"/>
    <property name="PowerOnLidOpen" type="b" access="read"/>
    <property name="BlockRecording" type="b" access="read"/>
    <method name="SetChargeEndThreshold"><arg name="percent" type="u" direction="in"/></method>
    <method name="SetUsbCharging"><arg name="enabled" type="b" direction="in"/></method>
    <method name="SetPowerOnLidOpen"><arg name="enabled" type="b" direction="in"/></method>
    <method name="SetBlockRecording"><arg name="enabled" type="b" direction="in"/></method>
  </interface>
</node>`;
const SettingsProxy = Gio.DBusProxy.makeProxyWrapper(SettingsIface);

const SWITCHES = [
    {feature: 'block-recording', prop: 'BlockRecording', method: 'SetBlockRecording', label: 'Block camera and microphone'},
    {feature: 'usb-charging', prop: 'UsbCharging', method: 'SetUsbCharging', label: 'USB charging while off'},
    {feature: 'power-on-lid-open', prop: 'PowerOnLidOpen', method: 'SetPowerOnLidOpen', label: 'Power on when lid opens'},
];

const GalaxyBookToggle = GObject.registerClass(
class GalaxyBookToggle extends QuickMenuToggle {
    _init(proxy) {
        super._init({
            title: 'Galaxy Book',
            iconName: 'battery-good-charging-symbolic',
            toggleMode: false,
        });
        this._proxy = proxy;
        this._lastLimit = PROTECTED_LIMIT;

        this.menu.setHeader('computer-laptop-symbolic', 'Galaxy Book', 'Samsung hardware settings');

        this._switches = SWITCHES.map(spec => {
            const item = new PopupMenu.PopupSwitchMenuItem(spec.label, false);
            item.connect('toggled', (_item, state) => this._call(spec.method, state));
            this.menu.addMenuItem(item);
            return {...spec, item};
        });

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const settingsItem = this.menu.addAction('Galaxy Book Settings', () => {
            Main.overview.hide();
            Main.panel.closeQuickSettings();
            Shell.AppSystem.get_default().lookup_app(APP_ID)?.activate();
        });
        settingsItem.visible = Shell.AppSystem.get_default().lookup_app(APP_ID) !== null;

        this.connect('clicked', () => {
            const protectedNow = this.checked;
            this._call('SetChargeEndThreshold', protectedNow ? 100 : this._lastLimit);
        });
        this._propsId = proxy.connect('g-properties-changed', () => this._sync());
        this._sync();
    }

    _call(method, value) {
        this._proxy[`${method}Remote`](value, (_result, error) => {
            if (error) {
                Gio.DBusError.strip_remote_error(error);
                Main.notifyError('Galaxy Book', error.message);
                this._sync();
            }
        });
    }

    _sync() {
        const features = this._proxy.Features ?? [];
        const threshold = this._proxy.ChargeEndThreshold ?? 0;
        const hasThreshold = features.includes('charge-threshold') && threshold > 0;

        this.reactive = hasThreshold;
        this.checked = hasThreshold && threshold < 100;
        if (this.checked)
            this._lastLimit = threshold;
        this.subtitle = !hasThreshold ? null
            : this.checked ? `Battery protected · ${threshold}%`
                : 'Battery charges to 100%';

        for (const {feature, prop, item} of this._switches) {
            item.visible = features.includes(feature);
            item.setToggleState(Boolean(this._proxy[prop]));
        }
    }

    destroy() {
        this._proxy.disconnect(this._propsId);
        super.destroy();
    }
});

const GalaxyBookIndicator = GObject.registerClass(
class GalaxyBookIndicator extends SystemIndicator {
    _init(proxy) {
        super._init();
        this._proxy = proxy;
        // Panel icon shown while the camera and microphone are blocked
        this._indicator = this._addIndicator();
        this._indicator.iconName = 'camera-disabled-symbolic';
        this.quickSettingsItems.push(new GalaxyBookToggle(proxy));
        this._propsId = proxy.connect('g-properties-changed', () => this._sync());
        this._sync();
    }

    _sync() {
        this._indicator.visible = Boolean(this._proxy.BlockRecording);
    }

    destroy() {
        this._proxy.disconnect(this._propsId);
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class GalaxyBookExtension extends Extension {
    enable() {
        this._cancellable = new Gio.Cancellable();
        new SettingsProxy(Gio.DBus.system, BUS_NAME, OBJECT_PATH, (proxy, error) => {
            if (error) {
                if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    console.error(`Galaxy Book Settings: ${error.message}`);
                return;
            }
            if (!proxy.Features) {
                console.warn('Galaxy Book Settings: galaxybookd is not running');
                return;
            }
            this._indicator = new GalaxyBookIndicator(proxy);
            Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        }, this._cancellable, Gio.DBusProxyFlags.NONE);
    }

    disable() {
        this._cancellable?.cancel();
        this._cancellable = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
