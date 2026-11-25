"""Debounced directory notifications for selective sync."""
import os
from pathlib import Path
import threading
import time

from gi.repository import Gio, GLib


class LocalChanges:
    def __init__(self):
        self.paths = {}
        self.lock = threading.Lock()

    def add(self, path):
        with self.lock:
            self.paths[path] = time.monotonic() + 0.75

    def ready(self):
        with self.lock:
            return any(due <= time.monotonic() for due in self.paths.values())

    def take(self):
        now = time.monotonic()
        with self.lock:
            batch = [path for path, due in self.paths.items() if due <= now][:32]
            for path in batch:
                del self.paths[path]
        return batch


class DirectoryWatcher:
    def __init__(self, root, folders, changes):
        self.root = Path(root)
        self.selected = {self.root / name for name in folders}
        self.changes = changes
        self.monitors = {}
        self.watch(self.root)
        for folder in self.selected:
            self.watch_tree(folder)

    def watch(self, folder):
        if folder in self.monitors or not folder.is_dir() or folder.is_symlink():
            return
        try:
            monitor = Gio.File.new_for_path(str(folder)).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, None,
            )
        except GLib.Error:
            return  # The periodic scan also covers directories without a watch.
        monitor.connect('changed', self.changed, folder)
        self.monitors[folder] = monitor

    def watch_tree(self, folder):
        if folder.is_symlink():
            return
        for directory, children, _ in os.walk(folder):
            path = Path(directory)
            children[:] = [name for name in children if not (path / name).is_symlink()]
            self.watch(path)

    def close(self):
        for monitor in self.monitors.values():
            monitor.cancel()
        self.monitors.clear()

    def changed(self, monitor, file, other, event, folder):
        if event not in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT, Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED, Gio.FileMonitorEvent.RENAMED,
            Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.MOVED_OUT,
        ):
            return
        for entry in (file, other):
            if entry is None or entry.get_path() is None:
                continue
            path = Path(entry.get_path())
            if not path.is_relative_to(self.root) or path.name.startswith('.mycloud-transfer-'):
                continue
            if not any(path.is_relative_to(selected) for selected in self.selected):
                continue
            if path.is_dir() and not path.is_symlink():
                self.watch_tree(path)
            elif path in self.monitors:
                for directory in list(self.monitors):
                    if directory.is_relative_to(path):
                        self.monitors.pop(directory).cancel()
            elif folder != self.root:
                self.changes.add(path.relative_to(self.root).as_posix())
