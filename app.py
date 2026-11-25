#!/usr/bin/python3
"""Selective Swisscom myCloud sync for the Ubuntu desktop."""
from contextlib import closing
import hashlib
import json
import logging
import sqlite3
import sys
from pathlib import Path
import threading
import time
from urllib.error import HTTPError

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
gi.require_version('Secret', '1')
from gi.repository import AyatanaAppIndicator3 as Indicator, Gio, GLib, Gtk, Secret

from sync import Sync, WebDAV, last_transfer
from feed import VERSION, read_events
from watcher import DirectoryWatcher, LocalChanges

APP_ID = 'ch.caesarlinx.CaesarSync'
DATA = Path(GLib.get_user_data_dir()) / 'caesarsync'
CONFIG = DATA / 'settings.json'
DATABASE = DATA / 'sync.sqlite3'
ICONS = Path(__file__).resolve().parent / 'icons'
SCHEMA = Secret.Schema.new(APP_ID, Secret.SchemaFlags.NONE, {
    'address': Secret.SchemaAttributeType.STRING,
    'username': Secret.SchemaAttributeType.STRING,
})
FEED_SCHEMA = Secret.Schema.new(APP_ID + '.Feed', Secret.SchemaFlags.NONE, {
    'address': Secret.SchemaAttributeType.STRING,
    'username': Secret.SchemaAttributeType.STRING,
})


def account(settings):
    return {name: settings[name] for name in ('address', 'username')}


def sync_scope(settings):
    return hashlib.sha256(json.dumps([
        settings['address'].rstrip('/') + '/', settings['username'], settings['root'],
    ]).encode()).hexdigest()


def save(settings):
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = CONFIG.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings, indent=2) + '\n')
    temporary.replace(CONFIG)


def checkpoint(settings, value=None):
    key = sync_scope(settings) + json.dumps(sorted(settings['folders']))
    with closing(sqlite3.connect(DATABASE)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS feed_checkpoint (scope TEXT PRIMARY KEY, timestamp INTEGER)')
        if value is not None:
            db.execute('INSERT OR REPLACE INTO feed_checkpoint VALUES (?, ?)', (key, value))
            db.commit()
            return value
        row = db.execute('SELECT timestamp FROM feed_checkpoint WHERE scope = ?', (key,)).fetchone()
        return row[0] if row else None


class MyCloud(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.settings = {}
        self.settings_window = None
        self.worker = None
        self.stop = threading.Event()
        self.next_scan = 0
        self.next_local = 0
        self.status = 'Sync is off'
        self.details = ''
        self.state = 'off'
        self.setup_busy = False
        self.quitting = False
        self.force_scan = False
        self.signin_window = None
        self.feed_signin_required = False
        self.feed_signin_prompted = False
        self.feed_settings_message = None
        self.watcher = None
        self.changes = LocalChanges()

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()
        self.indicator = Indicator.Indicator.new_with_path(
            APP_ID, 'cloud-off', Indicator.IndicatorCategory.APPLICATION_STATUS, str(ICONS),
        )
        self.indicator.set_title('CaesarSync')
        self.indicator.set_status(Indicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label=self.status)
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)
        self.transfer_item = Gtk.MenuItem()
        self.transfer_item.set_sensitive(False)
        menu.append(self.transfer_item)
        self.action(menu, 'Show activity', self.show_activity)
        self.folder_item = self.action(menu, 'Open myCloud folder', self.open_folder)
        self.pause_item = self.action(menu, 'Pause sync', self.toggle_sync)
        self.action(menu, 'Settings', self.show_settings)
        self.action(menu, 'Quit CaesarSync', self.quit_client)
        self.scan_item = self.action(menu, 'Check all folders now', self.request_full_scan)
        menu.show_all()
        self.indicator.set_menu(menu)
        try:
            self.settings = json.loads(CONFIG.read_text())
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            self.set_status('error', 'Could not read myCloud settings')
        self.refresh_menu()
        self.load_last_transfer()
        self.configure_watcher()
        self.tick()
        GLib.timeout_add_seconds(2, self.tick)
        GLib.timeout_add(350, self.check_local_changes)

    def configure_watcher(self):
        if self.watcher is not None:
            self.watcher.close()
            self.watcher = None
        self.changes = LocalChanges()
        if self.settings.get('enabled'):
            self.watcher = DirectoryWatcher(
                self.settings['root'], self.settings['folders'], self.changes,
            )

    def check_local_changes(self):
        if self.changes.ready():
            self.tick()
        return GLib.SOURCE_CONTINUE

    def do_activate(self):
        # The tray owns the UI. Desktop launches leave it in the background.
        pass

    def action(self, menu, label, handler):
        item = Gtk.MenuItem(label=label)
        item.connect('activate', lambda _: handler())
        menu.append(item)
        return item

    def set_status(self, state, message, details=''):
        self.status = message
        self.state = state
        self.details = details
        self.status_item.set_label(message)
        self.indicator.set_icon_full('cloud-' + state, message)
        return False

    def refresh_menu(self):
        self.folder_item.set_sensitive(bool(self.settings))
        self.pause_item.set_sensitive(bool(self.settings.get('folders')))
        self.pause_item.set_label('Pause sync' if self.settings.get('enabled') else 'Resume sync')
        self.scan_item.set_sensitive(bool(self.settings.get('enabled')))

    def request_full_scan(self):
        self.force_scan = True
        self.next_scan = 0
        self.tick()

    def load_last_transfer(self):
        transfer = None
        if {'address', 'username', 'root'} <= self.settings.keys():
            try:
                transfer = last_transfer(DATABASE, sync_scope(self.settings))
            except sqlite3.Error:
                logging.exception('Could not read the last myCloud transfer')
                self.transfer_item.set_label('Last sync unavailable')
                return
        self.set_last_transfer(transfer)

    def set_last_transfer(self, transfer):
        if transfer is None:
            self.transfer_item.set_label('No sync activity recorded yet')
        else:
            path, size, direction = transfer
            name = Path(path).name
            action = {'Uploaded': 'Uploaded to myCloud',
                      'Downloaded': 'Downloaded locally'}.get(direction, direction)
            size_label = f' ({GLib.format_size(size)})' if size is not None else ''
            self.transfer_item.set_label(f'Last completed · {action}: {name}{size_label}')

    def show_last_transfer(self, scope, path, size, direction):
        if scope == sync_scope(self.settings):
            self.set_last_transfer((path, size, direction))
        return False

    def open_folder(self):
        path = Path(self.settings['root'])
        path.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    def toggle_sync(self):
        self.settings['enabled'] = not self.settings['enabled']
        save(self.settings)
        self.refresh_menu()
        self.configure_watcher()
        if self.settings['enabled']:
            self.next_scan = 0
            self.set_status('busy', 'Waiting to sync')
            self.tick()
        else:
            self.stop.set()
            self.set_status('off', 'Sync paused')

    def quit_client(self):
        self.quitting = True
        self.stop.set()
        if self.watcher is not None:
            self.watcher.close()
        if self.worker is None:
            self.quit()
        else:
            self.set_status('off', 'Finishing the current transfer before quitting')

    def tick(self):
        now = time.monotonic()
        full = now >= self.next_scan
        if (not self.quitting and self.settings.get('enabled') and self.worker is None
                and (full or (now >= self.next_local and self.changes.ready()))):
            self.stop = threading.Event()
            previous_status = (self.state, self.status, self.details)
            self.worker = threading.Thread(
                target=self.sync,
                args=(dict(self.settings), self.stop, full, self.changes, previous_status, self.force_scan),
                daemon=True,
            )
            self.force_scan = False
            if full:
                if not self.settings.get('feed_enabled'):
                    self.set_status('busy', 'Checking for changes')
            self.worker.start()
        return True

    def sync(self, settings, stop, full, changes, previous_status, force_scan):
        delay = 300
        sign_in_required = False
        try:
            credentials = account(settings)
            password = Secret.password_lookup_sync(SCHEMA, credentials, None)
            if not password:
                raise ValueError('Enter your WebDAV password in Settings.')
            dav = WebDAV(**credentials, password=password)
            scope = sync_scope(settings)

            def report(message):
                if not stop.is_set():
                    GLib.idle_add(self.show_transfer, stop, message)

            def transferred(path, size, direction):
                GLib.idle_add(self.show_last_transfer, scope, path, size, direction)

            def scanning(folder):
                if not stop.is_set():
                    phase = 'Initial reconciliation' if initializing else 'Checking for changes'
                    GLib.idle_add(self.show_transfer, stop, f'Checking {folder}', phase)

            engine = Sync(
                dav, settings['root'], DATABASE, scope, report, stop.is_set,
                transferred=transferred, scanning=scanning, take_changes=changes.take,
            )
            baseline = full
            initializing = False
            paths = ()
            mark = None
            feed_error = None
            if full and settings.get('feed_enabled'):
                delay = 15
                baseline = force_scan
                try:
                    token = Secret.password_lookup_sync(FEED_SCHEMA, credentials, None)
                    if not token:
                        sign_in_required = True
                        raise ValueError('Open Settings and sign in to Swisscom again.')
                    since = checkpoint(settings)
                    if since is None:
                        delay = 300
                    mark, paths = read_events(
                        token, since if since is not None else int(time.time() * 1000) - 60_000,
                    )
                    baseline = force_scan or since is None
                    initializing = since is None
                    if initializing:
                        scanning('selected folders for the initial reconciliation')
                except HTTPError as exc:
                    with exc:
                        if exc.code in (401, 403):
                            sign_in_required = True
                            feed_error = 'Swisscom rejected the session. Sign in again in Settings.'
                            delay = 300
                        else:
                            feed_error = f'Swisscom file-change check returned HTTP {exc.code}.'
                except (ValueError, OSError) as exc:
                    feed_error = str(exc)
            if not Path(settings['root']).is_dir():
                raise ValueError('The local sync folder is unavailable.')
            waiting, errors = engine.run(settings['folders'], full=baseline, remote_paths=paths)
            if mark is not None and not errors and not waiting and not stop.is_set():
                checkpoint(settings, mark)
                delay = 15
            if feed_error:
                errors.insert(0, feed_error)
            if errors:
                status = ('error', 'Sync needs attention', '\n'.join(errors + waiting))
            elif waiting:
                status = ('wait', 'Incoming updates waiting', '\n'.join(waiting))
            else:
                status = ('ok', 'Up to date', '') if full else previous_status
        except Exception as exc:
            status = ('error', 'Sync needs attention', str(exc))
        GLib.idle_add(self.sync_finished, stop, status, full, delay, sign_in_required)

    def show_transfer(self, stop, message, status='Syncing myCloud'):
        if not stop.is_set():
            self.set_status('busy', status, message)
        return False

    def sync_finished(self, stop, status, full, delay, sign_in_required=False):
        self.worker = None
        if self.quitting:
            self.quit()
        elif stop.is_set():
            self.next_scan = 0
        else:
            self.next_local = time.monotonic() + (8 if status[0] == 'error' else 0)
            if full:
                self.next_scan = 0 if self.force_scan else time.monotonic() + delay
            self.set_status(*status)
            if sign_in_required:
                self.feed_signin_required = True
                if self.feed_settings_message is not None:
                    self.feed_settings_message.show()
                if not self.feed_signin_prompted:
                    self.feed_signin_prompted = True
                    self.show_settings()
        return False

    def show_activity(self):
        dialog = Gtk.Dialog(title='CaesarSync activity', transient_for=self.settings_window)
        dialog.add_button('Close', Gtk.ResponseType.CLOSE)
        dialog.set_default_size(470, 235)
        content = dialog.get_content_area()
        content.set_border_width(16)
        summary = Gtk.Label(label=self.status, xalign=0)
        content.pack_start(summary, False, False, 8)
        scroll = Gtk.ScrolledWindow()
        details = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll.add(details)
        content.pack_start(scroll, True, True, 0)

        def refresh():
            if not dialog.get_visible():
                return False
            summary.set_text(self.status)
            details.get_buffer().set_text(self.details)
            return True

        dialog.connect('response', lambda window, _: window.destroy())
        dialog.show_all()
        refresh()
        timer = GLib.timeout_add(300, refresh)
        dialog.connect('destroy', lambda _: GLib.source_remove(timer))

    def show_settings(self):
        if self.settings_window is not None:
            self.settings_window.present()
            return
        window = Gtk.ApplicationWindow(application=self, title=f'CaesarSync {VERSION} Settings')
        window.set_default_size(590, 490)
        window.connect('destroy', lambda _: setattr(self, 'settings_window', None))
        self.settings_window = window
        window.connect('destroy', lambda _: setattr(self, 'feed_settings_message', None))
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=18)
        window.add(layout)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        layout.pack_start(grid, False, False, 0)
        fields = {}
        for row, (key, label, default) in enumerate((
            ('address', 'WebDAV address', ''),
            ('username', 'Username', ''),
            ('password', 'WebDAV password', ''),
            ('root', 'Local folder', str(Path.home() / 'CaesarSync')),
        )):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            entry = Gtk.Entry(text=self.settings.get(key, default), hexpand=True)
            grid.attach(entry, 1, row, 1, 1)
            fields[key] = entry
        fields['password'].set_visibility(False)
        if self.settings:
            fields['password'].set_placeholder_text('Leave blank to use the saved password')
        fields['address'].set_placeholder_text('Copy the address from your myCloud WebDAV settings')
        choose = Gtk.Button(label='Browse…')
        grid.attach(choose, 2, 3, 1, 1)

        def browse(_):
            chooser = Gtk.FileChooserDialog(
                title='Choose a local folder', transient_for=window,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            chooser.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Choose', Gtk.ResponseType.OK)
            if chooser.run() == Gtk.ResponseType.OK:
                fields['root'].set_text(chooser.get_filename())
            chooser.destroy()

        choose.connect('clicked', browse)
        test = Gtk.Button(label='Test connection and load folders')
        layout.pack_start(test, False, False, 0)
        signin = Gtk.Button(label='Sign in to Swisscom for faster downloads')
        signin.set_sensitive(bool(self.settings.get('username')))
        layout.pack_start(signin, False, False, 0)
        feed_notice = Gtk.Label(
            label='Swisscom sign-in is needed again. Sign in to restore fast downloads.',
            xalign=0, wrap=True, no_show_all=True,
        )
        layout.pack_start(feed_notice, False, False, 0)
        self.feed_settings_message = feed_notice
        feed_notice.set_visible(self.feed_signin_required)
        layout.pack_start(Gtk.Label(label='Folders to sync', xalign=0), False, False, 0)
        scroll = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=8)
        scroll.add(choices)
        layout.pack_start(scroll, True, True, 0)
        feedback = Gtk.Label(label='Enter your WebDAV details to list cloud folders.',
                             xalign=0, wrap=True)
        layout.pack_start(feedback, False, False, 0)
        start = Gtk.Button(label='Start sync' if not self.settings.get('enabled') else 'Save and sync')
        start.set_sensitive(False)
        layout.pack_start(start, False, False, 0)
        connected = {}
        checks = {}

        def start_signin(_):
            if self.signin_window is not None:
                self.signin_window.present()
                return
            credentials = account(self.settings)

            def store_token(token):
                if credentials != account(self.settings):
                    feedback.set_text('The configured WebDAV account changed. Sign in again.')
                    return
                try:
                    if not Secret.password_store_sync(
                        FEED_SCHEMA, credentials, Secret.COLLECTION_DEFAULT,
                        'CaesarSync Swisscom sign-in', token, None,
                    ):
                        raise ValueError('Could not save the Swisscom session in the keyring.')
                    settings = dict(self.settings, feed_enabled=True)
                    save(settings)
                except (OSError, ValueError, GLib.Error) as exc:
                    feedback.set_text(str(exc))
                    return
                self.stop.set()
                self.settings = settings
                self.feed_signin_required = False
                self.feed_signin_prompted = False
                feed_notice.hide()
                self.next_scan = 0
                feedback.set_text('Swisscom sign-in complete. Incremental downloads are enabled.')
                self.tick()

            try:
                from signin import SignIn
                self.signin_window = SignIn(window, credentials['username'], store_token)
            except (ImportError, ValueError) as exc:
                feedback.set_text(f'Could not open Swisscom sign-in ({exc}).')
                return
            self.signin_window.connect('destroy', lambda _: setattr(self, 'signin_window', None))
            self.signin_window.show_all()
            self.signin_window.present()

        signin.connect('clicked', start_signin)

        def values():
            return {key: field.get_text().strip() if key != 'password' else field.get_text()
                    for key, field in fields.items()}

        def changed(_):
            start.set_sensitive(False)
            connected.clear()

        for key in ('address', 'username', 'password'):
            fields[key].connect('changed', changed)

        def complete(result, password, folders, error):
            self.setup_busy = False
            if window is not self.settings_window:
                return False
            test.set_sensitive(True)
            if account(values()) != account(result) or values()['password'] != result['password']:
                feedback.set_text('Credentials changed. Test the connection again.')
            elif error:
                feedback.set_text(error)
            else:
                connected.update(account(result), password=password)
                checks.clear()
                for child in choices.get_children():
                    choices.remove(child)
                selected = self.settings.get('folders', []) if all(
                    self.settings.get(key) == result[key] for key in ('address', 'username')
                ) else []
                for name in folders:
                    button = Gtk.CheckButton(label=name)
                    button.set_active(name in selected)
                    choices.pack_start(button, False, False, 0)
                    checks[name] = button
                choices.show_all()
                feedback.set_text('Choose the folders to sync.' if folders else 'No cloud folders found.')
                start.set_sensitive(True)
            return False

        def connect(_):
            result = values()
            if not result['username']:
                feedback.set_text('Enter your WebDAV username.')
                return
            if self.setup_busy:
                feedback.set_text('A connection check is already running. Try again shortly.')
                return
            connected.clear()
            self.setup_busy = True
            test.set_sensitive(False)
            start.set_sensitive(False)
            feedback.set_text('Checking the connection…')

            def load():
                password = result['password']
                try:
                    if not password:
                        password = Secret.password_lookup_sync(SCHEMA, account(result), None)
                    if not password:
                        raise ValueError('Enter your WebDAV password.')
                    dav = WebDAV(**account(result), password=password)
                    folders = sorted(name for name, item in dav.list().items() if item is None)
                    GLib.idle_add(complete, result, password, folders, None)
                except Exception as exc:
                    GLib.idle_add(complete, result, '', [], str(exc))

            threading.Thread(target=load, daemon=True).start()

        def apply(_):
            folders = [name for name, button in checks.items() if button.get_active()]
            result = values()
            if not connected:
                return
            try:
                if not result['root']:
                    raise ValueError('Choose a local folder.')
                root = Path(result['root']).expanduser().resolve()
                if root == root.parent or root == Path.home():
                    raise ValueError('Choose a dedicated folder inside your home folder or on another drive.')
                root.mkdir(parents=True, exist_ok=True)
                credentials = account(connected)
                previous_account = {key: self.settings.get(key) for key in credentials}
                if any(root.iterdir()) and (
                    self.settings.get('root') != str(root) or credentials != previous_account
                ):
                    raise ValueError('Choose an empty local folder for this sync setup.')
                if not Secret.password_store_sync(
                    SCHEMA, credentials, Secret.COLLECTION_DEFAULT,
                    'CaesarSync WebDAV', connected['password'], None,
                ):
                    raise ValueError('Could not store the password in your Linux keyring.')
                settings = dict(credentials, root=str(root), folders=folders, enabled=bool(folders))
                if credentials == previous_account and self.settings.get('feed_enabled'):
                    settings['feed_enabled'] = True
                save(settings)
            except (OSError, ValueError, GLib.Error) as exc:
                feedback.set_text(str(exc))
                return
            self.stop.set()
            self.settings = settings
            if credentials != previous_account:
                self.feed_signin_required = False
                self.feed_signin_prompted = False
            self.next_scan = 0
            self.refresh_menu()
            self.load_last_transfer()
            self.configure_watcher()
            self.set_status('busy', 'Waiting to sync') if folders else self.set_status('off', 'Sync is off')
            window.destroy()
            self.tick()

        test.connect('clicked', connect)
        start.connect('clicked', apply)
        window.show_all()
        window.present()


if __name__ == '__main__':
    if sys.argv[1:] == ['--version']:
        print(f'CaesarSync {VERSION}')
        raise SystemExit(0)
    raise SystemExit(MyCloud().run())
