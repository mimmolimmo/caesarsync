"""Embedded Swisscom login with an in-memory browser session."""
import threading
from urllib.error import HTTPError

import gi
gi.require_version('WebKit2', '4.1')
from gi.repository import Gio, GLib, Gtk, WebKit2

from feed import BROKER, CALLBACK_HOSTS, LOGIN, authenticate, login_code


class SignIn(Gtk.Window):
    def __init__(self, parent, username, done):
        super().__init__(title='Swisscom sign-in', transient_for=parent, destroy_with_parent=True)
        self.set_default_size(850, 660)
        self.username = username
        self.done = done
        self.closed = False
        self.redeeming = False
        self.connect('destroy', lambda _: setattr(self, 'closed', True))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.add(box)
        self.status = Gtk.Label(label='Loading Swisscom sign-in…', xalign=0, wrap=True)
        box.pack_start(self.status, False, False, 0)
        self.view = WebKit2.WebView.new_with_context(WebKit2.WebContext.new_ephemeral())
        box.pack_start(self.view, True, True, 0)
        self.view.connect('notify::uri', self.navigation)
        self.view.connect('load-failed', self.load_failed)
        manager = self.view.get_user_content_manager()
        manager.register_script_message_handler('loginBroker')
        manager.connect('script-message-received::loginBroker', self.open_broker)
        manager.add_script(WebKit2.UserScript.new(
            'window.openLoginBrokerWindowFromDC = function(path) {'
            'window.webkit.messageHandlers.loginBroker.postMessage(path); };',
            WebKit2.UserContentInjectedFrames.TOP_FRAME, WebKit2.UserScriptInjectionTime.START,
            [f'https://{host}/*' for host in sorted(CALLBACK_HOSTS)], None,
        ))
        self.view.load_uri(LOGIN)

    def open_broker(self, _, message):
        path = message.get_js_value().to_string()
        if not path.startswith('/') or path.startswith('//') or '\\' in path:
            return
        Gio.AppInfo.launch_default_for_uri(BROKER + path)
        self.status.set_text('Complete Swisscom sign-in in the browser window that opened.')

    def load_failed(self, *_):
        if not self.redeeming:
            self.status.set_text('Swisscom sign-in could not load. Close this window and try again.')
        return False

    def navigation(self, view, _):
        if self.redeeming:
            return
        code = login_code(view.get_uri() or '')
        if code:
            self.redeeming = True
            view.stop_loading()
            self.status.set_text('Checking the Swisscom session…')
            threading.Thread(target=self.exchange, args=(code,), daemon=True).start()

    def exchange(self, code):
        try:
            token = authenticate(code, self.username)
        except HTTPError as error:
            with error:
                GLib.idle_add(self.finish, None, f'Swisscom sign-in returned HTTP {error.code}.')
        except ValueError as error:
            GLib.idle_add(self.finish, None, str(error) if error.args and
                          str(error).startswith('Swisscom ') else 'Unexpected Swisscom sign-in response.')
        except OSError:
            GLib.idle_add(self.finish, None, 'Swisscom sign-in could not complete.')
        else:
            GLib.idle_add(self.finish, token, None)

    def finish(self, token, error):
        if not self.closed:
            if error:
                self.status.set_text(error + ' Close this window and try again.')
            else:
                self.done(token)
                self.destroy()
        return False
