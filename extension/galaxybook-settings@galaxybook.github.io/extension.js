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

const BUDS_BUS_NAME = 'io.github.galaxybook.Buds1';
const BUDS_OBJECT_PATH = '/io/github/galaxybook/Buds1';

const BudsIface = `
<node>
  <interface name="io.github.galaxybook.Buds1">
    <property name="Connected" type="b" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="NoiseControlModes" type="as" access="read"/>
    <property name="BatteryLeft" type="i" access="read"/>
    <property name="BatteryRight" type="i" access="read"/>
    <property name="BatteryCase" type="i" access="read"/>
    <property name="NoiseControl" type="s" access="read"/>
    <property name="TouchpadLocked" type="b" access="read"/>
    <method name="SetNoiseControl"><arg name="mode" type="s" direction="in"/></method>
    <method name="SetTouchpadLocked"><arg name="locked" type="b" direction="in"/></method>
  </interface>
</node>`;
const BudsProxy = Gio.DBusProxy.makeProxyWrapper(BudsIface);

const PHONE_BUS_NAME = 'io.github.galaxybook.Phone1';
const PHONE_OBJECT_PATH = '/io/github/galaxybook/Phone1';

const PhoneIface = `
<node>
  <interface name="io.github.galaxybook.Phone1">
    <property name="Connected" type="b" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="Battery" type="i" access="read"/>
    <property name="BatteryCharging" type="b" access="read"/>
    <property name="BatterySource" type="s" access="read"/>
    <property name="AdbConnected" type="b" access="read"/>
    <property name="CanMirror" type="b" access="read"/>
    <method name="StartMirroring"/>
  </interface>
</node>`;
const PhoneProxy = Gio.DBusProxy.makeProxyWrapper(PhoneIface);

const NOISE_MODES = [
    {mode: 'anc', label: 'Noise canceling'},
    {mode: 'adaptive', label: 'Adaptive'},
    {mode: 'ambient', label: 'Ambient sound'},
    {mode: 'off', label: 'Off'},
];

function newProxy(Wrapper, bus, name, path, cancellable) {
    return new Promise((resolve, reject) => {
        new Wrapper(bus, name, path, (proxy, error) => {
            if (error)
                reject(error);
            else
                resolve(proxy);
        }, cancellable, Gio.DBusProxyFlags.NONE);
    });
}

function callProxy(proxy, method, value, onError) {
    const args = value === undefined ? [] : [value];
    proxy[`${method}Remote`](...args, (_result, error) => {
        if (error) {
            Gio.DBusError.strip_remote_error(error);
            Main.notifyError('Galaxy Book', error.message);
            onError?.();
        }
    });
}

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
        const settingsItem = this.menu.addAction('Galaxy Book Settings', () => launchApp(null));
        settingsItem.visible = Shell.AppSystem.get_default().lookup_app(APP_ID) !== null;

        this.connect('clicked', () => {
            const protectedNow = this.checked;
            this._call('SetChargeEndThreshold', protectedNow ? 100 : this._lastLimit);
        });
        this._propsId = proxy.connect('g-properties-changed', () => this._sync());
        this._sync();
    }

    _call(method, value) {
        callProxy(this._proxy, method, value, () => this._sync());
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

const BudsToggle = GObject.registerClass(
class BudsToggle extends QuickMenuToggle {
    _init(proxy) {
        super._init({
            title: 'Galaxy Buds',
            iconName: 'audio-headphones-symbolic',
            toggleMode: false,
        });
        this._proxy = proxy;

        this.menu.setHeader('audio-headphones-symbolic', 'Galaxy Buds', 'Noise controls');
        this._modeItems = NOISE_MODES.map(({mode, label}) => {
            const item = this.menu.addAction(label, () => this._call('SetNoiseControl', mode));
            return {mode, item};
        });
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._lockItem = new PopupMenu.PopupSwitchMenuItem('Lock touch controls', false);
        this._lockItem.connect('toggled', (_item, state) => this._call('SetTouchpadLocked', state));
        this.menu.addMenuItem(this._lockItem);

        // Clicking the tile switches noise canceling on and off
        this.connect('clicked', () => this._call('SetNoiseControl', this.checked ? 'off' : 'anc'));
        this._propsId = proxy.connect('g-properties-changed', () => this._sync());
        this._ownerId = proxy.connect('notify::g-name-owner', () => this._sync());
        this._sync();
    }

    _call(method, value) {
        callProxy(this._proxy, method, value, () => this._sync());
    }

    _sync() {
        const connected = this._proxy.g_name_owner !== null && Boolean(this._proxy.Connected);
        this.visible = connected;
        if (!connected)
            return;

        const mode = this._proxy.NoiseControl;
        this.checked = mode === 'anc';
        this.title = this._proxy.Name || 'Galaxy Buds';
        const levels = [['L', this._proxy.BatteryLeft], ['R', this._proxy.BatteryRight]]
            .filter(([, level]) => level >= 0)
            .map(([side, level]) => `${side} ${level}%`);
        this.subtitle = levels.join(' · ') || null;
        const modes = this._proxy.NoiseControlModes ?? [];
        for (const {mode: itemMode, item} of this._modeItems) {
            item.visible = modes.includes(itemMode);
            item.setOrnament(itemMode === mode ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        }
        this._lockItem.setToggleState(Boolean(this._proxy.TouchpadLocked));
    }

    destroy() {
        this._proxy.disconnect(this._propsId);
        this._proxy.disconnect(this._ownerId);
        super.destroy();
    }
});

function launchApp(action) {
    Main.overview.hide();
    Main.panel.closeQuickSettings();
    const app = Shell.AppSystem.get_default().lookup_app(APP_ID);
    if (action)
        app?.launch_action(action, 0, -1);
    else
        app?.activate();
}

const PhoneToggle = GObject.registerClass(
class PhoneToggle extends QuickMenuToggle {
    _init(proxy) {
        super._init({
            title: 'Galaxy phone',
            iconName: 'phone-symbolic',
            toggleMode: false,
        });
        this._proxy = proxy;

        this.menu.setHeader('phone-symbolic', 'Galaxy phone');
        this._mirrorItem = this.menu.addAction('Mirror screen', () => {
            Main.panel.closeQuickSettings();
            callProxy(this._proxy, 'StartMirroring');
        });
        this.menu.addAction('Send files…', () => launchApp('send-files'));
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Phone settings', () => launchApp(null));

        // There is nothing to switch on or off, so a click opens the menu
        this.connect('clicked', () => this.menu.open());
        this._propsId = proxy.connect('g-properties-changed', () => this._sync());
        this._ownerId = proxy.connect('notify::g-name-owner', () => this._sync());
        this._sync();
    }

    _sync() {
        const running = this._proxy.g_name_owner !== null;
        const connected = running && (Boolean(this._proxy.Connected) || Boolean(this._proxy.AdbConnected));
        this.visible = connected;
        if (!connected)
            return;

        this.checked = true;
        this.title = this._proxy.Name || 'Galaxy phone';
        const level = this._proxy.Battery ?? -1;
        const parts = [];
        if (level >= 0)
            parts.push(this._proxy.BatterySource === 'adb' ? `${level}%` : `~${level}%`);
        if (this._proxy.BatteryCharging)
            parts.push('charging');
        this.subtitle = parts.join(' · ') || null;
        this.menu.setHeader('phone-symbolic', this.title, this.subtitle ?? '');
        this._mirrorItem.visible = Boolean(this._proxy.CanMirror);
    }

    destroy() {
        this._proxy.disconnect(this._propsId);
        this._proxy.disconnect(this._ownerId);
        super.destroy();
    }
});

const GalaxyBookIndicator = GObject.registerClass(
class GalaxyBookIndicator extends SystemIndicator {
    _init(proxy, budsProxy, phoneProxy) {
        super._init();
        this._proxy = proxy;
        if (proxy) {
            // Panel icon shown while the camera and microphone are blocked
            this._indicator = this._addIndicator();
            this._indicator.iconName = 'camera-disabled-symbolic';
            this.quickSettingsItems.push(new GalaxyBookToggle(proxy));
            this._propsId = proxy.connect('g-properties-changed', () => this._sync());
            this._sync();
        }
        if (budsProxy)
            this.quickSettingsItems.push(new BudsToggle(budsProxy));
        if (phoneProxy)
            this.quickSettingsItems.push(new PhoneToggle(phoneProxy));
    }

    _sync() {
        this._indicator.visible = Boolean(this._proxy.BlockRecording);
    }

    destroy() {
        if (this._propsId)
            this._proxy.disconnect(this._propsId);
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class GalaxyBookExtension extends Extension {
    enable() {
        this._cancellable = new Gio.Cancellable();
        const cancellable = this._cancellable;
        const settled = (promise, what) => promise.catch(error => {
            if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                console.error(`Galaxy Book Settings: ${what}: ${error.message}`);
            return null;
        });

        Promise.all([
            settled(newProxy(SettingsProxy, Gio.DBus.system, BUS_NAME, OBJECT_PATH, cancellable), 'galaxybookd'),
            settled(newProxy(BudsProxy, Gio.DBus.session, BUDS_BUS_NAME, BUDS_OBJECT_PATH, cancellable), 'galaxybudsd'),
            settled(newProxy(PhoneProxy, Gio.DBus.session, PHONE_BUS_NAME, PHONE_OBJECT_PATH, cancellable), 'galaxyphoned'),
        ]).then(([proxy, budsProxy, phoneProxy]) => {
            if (cancellable.is_cancelled())
                return;
            if (proxy && !proxy.Features) {
                console.warn('Galaxy Book Settings: galaxybookd is not running');
                proxy = null;
            }
            if (!proxy && !budsProxy && !phoneProxy)
                return;
            this._indicator = new GalaxyBookIndicator(proxy, budsProxy, phoneProxy);
            Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        });
    }

    disable() {
        this._cancellable?.cancel();
        this._cancellable = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
