"""WebDAV transport and selective file reconciliation."""
import base64
from contextlib import closing
import hashlib
import mimetypes
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
from contextlib import closing
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


@dataclass(frozen=True)
class RemoteFile:
    version: str
    etag: str


class WebDAV:
    def __init__(self, address, username, password):
        url = urlsplit(address)
        if (url.scheme != 'https' or not url.hostname or url.username is not None
                or url.query or url.fragment):
            raise ValueError('Enter the HTTPS WebDAV address shown in myCloud settings.')
        self.address = address.rstrip('/') + '/'
        self.authorization = 'Basic ' + base64.b64encode(
            f'{username}:{password}'.encode()
        ).decode('ascii')
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, data=None, **headers):
        url = self.address + quote(path, safe='/')
        request = Request(url, data=data, method=method, headers={
            'Authorization': self.authorization, **headers,
        })
        return self.opener.open(request, timeout=20)

    def list(self, folder=''):
        path = folder.rstrip('/') + '/' if folder else ''
        with self.request('PROPFIND', path, b'''<?xml version="1.0"?>
<propfind xmlns="DAV:"><prop><resourcetype/><getetag/><getlastmodified/>
<getcontentlength/></prop></propfind>''', Depth='1', **{'Content-Type': 'application/xml'}) as response:
            tree = ET.parse(response)
        root = unquote(urlsplit(self.address).path) + path
        children = {}
        for entry in tree.findall('{DAV:}response'):
            href = unquote(urlsplit(entry.findtext('{DAV:}href', '')).path)
            if href.rstrip('/') == root.rstrip('/'):
                continue
            if not href.startswith(root):
                raise ValueError('myCloud returned a path outside the requested folder.')
            name = href[len(root):].rstrip('/')
            if not name or name in {'.', '..'} or '/' in name or '\x00' in name:
                raise ValueError('myCloud returned an invalid folder entry.')
            props = {}
            for propstat in entry.findall('{DAV:}propstat'):
                if ' 200 ' in propstat.findtext('{DAV:}status', ''):
                    prop = propstat.find('{DAV:}prop')
                    if prop is not None:
                        props.update((child.tag, child) for child in prop)
            resource_type = props.get('{DAV:}resourcetype')
            if resource_type is None:
                raise ValueError(f'myCloud did not return the resource type for {name}.')
            if resource_type.find('{DAV:}collection') is not None:
                children[name] = None
                continue
            etag = props.get('{DAV:}getetag')
            modified = props.get('{DAV:}getlastmodified')
            size = props.get('{DAV:}getcontentlength')
            tag = (etag.text or '') if etag is not None else ''
            if not tag and (modified is None or size is None):
                raise ValueError(f'myCloud did not return version information for {name}.')
            version = tag or f'{modified.text}|{size.text}'
            children[name] = RemoteFile(version, tag)
        return children

    def download(self, path, remote):
        headers = {'If-Match': remote.etag} if remote.etag and not remote.etag.startswith('W/') else {}
        return self.request('GET', path, **headers)

    def upload(self, path, stream, size, remote):
        headers = self.write_precondition(remote)
        guess_type = getattr(mimetypes, 'guess_file_type', mimetypes.guess_type)
        content_type = guess_type(path)[0] or 'application/octet-stream'
        with self.request('PUT', path, stream, **{
            'Content-Length': str(size), 'Content-Type': content_type, **headers,
        }) as response:
            return response.headers.get('ETag')

    def mkdir(self, path):
        with self.request('MKCOL', path + '/'):
            pass

    @staticmethod
    def write_precondition(remote):
        if remote is None:
            return {'If-None-Match': '*'}
        etag = remote.etag
        if len(etag) < 2 or not etag.startswith('"') or not etag.endswith('"') or '"' in etag[1:-1]:
            raise ValueError('The cloud did not provide a strong version tag. Skipping the write.')
        return {'If-Match': etag}

    def delete(self, path, remote):
        headers = self.write_precondition(remote)
        try:
            with self.request('DELETE', path, **headers):
                pass
        except HTTPError as exc:
            if exc.code != 404:
                raise

def fingerprint(path):
    try:
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            after = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise ValueError(f'{path.name} changed while it was being read. Will retry.')
    return digest


def open_on_disk(path):
    result = subprocess.run(
        ['lsof', '-t', '--', str(path)], capture_output=True, timeout=5,
    )
    if result.returncode not in (0, 1):
        raise ValueError(f'Could not check whether {path.name} is open.')
    return bool(result.stdout.strip())


def local_entries(folder):
    entries = {}
    for path in folder.iterdir():
        if path.is_symlink() or path.name.startswith('.mycloud-transfer-'):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            entries[path.name] = True
        elif stat.S_ISREG(mode):
            entries[path.name] = False
    return entries


def last_transfer(database, scope):
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transfers'").fetchone():
            row = db.execute('''
                SELECT t.path, t.size, t.direction, f.path IS NOT NULL
                    AND f.local IS NULL AND f.remote IS NULL
                FROM transfers t LEFT JOIN files f
                    ON t.scope = f.scope AND t.path = f.path
                WHERE t.scope = ?
            ''', (scope,)).fetchone()
            if row is not None:
                path, size, direction, removed = row
                if removed and direction in ('Uploaded', 'Downloaded'):
                    return path, None, 'Removed from sync'
                return path, size, direction
    return None


class Sync:
    def __init__(self, dav, root, database, scope, report, stopped, is_open=open_on_disk,
                 *, transferred=None, scanning=None, take_changes=None):
        self.dav = dav
        self.root = Path(root)
        self.database = database
        self.scope = scope
        self.report = report
        self.stopped = stopped
        self.is_open = is_open
        self.transferred = transferred
        self.scanning = scanning
        self.take_changes = take_changes
        self.waiting = []
        self.errors = []

    def run(self, folders, *, full=True, remote_paths=()):
        if self.root.is_symlink():
            raise ValueError('The configured local sync folder became a symbolic link.')
        self.root.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS files (
                scope TEXT, path TEXT, local TEXT, remote TEXT,
                PRIMARY KEY(scope, path))''')
            db.execute('''CREATE TABLE IF NOT EXISTS transfers (
                scope TEXT PRIMARY KEY, path TEXT, size INTEGER, direction TEXT)''')
            self.selected = set(folders)
            self.sync_changes(db)
            if remote_paths and not self.stopped():
                self.sync_remote(db, remote_paths)
            if not full or self.stopped():
                return self.waiting, self.errors
            available = self.dav.list()
            for folder in folders:
                if self.stopped():
                    break
                if folder not in available or available[folder] is not None:
                    self.errors.append(f'Cloud folder unavailable: {folder}')
                    continue
                try:
                    self.walk(db, folder)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.errors.append(f'{folder}: {exc}')
            self.sync_changes(db)
        return self.waiting, self.errors

    def sync_remote(self, db, paths):
        listings = {}

        def listing(folder):
            if folder not in listings:
                listings[folder] = self.dav.list(folder)
            return listings[folder]

        for relative in sorted(set(paths)):
            if self.stopped():
                return
            parts = relative.split('/')
            if parts[0] not in self.selected or any(p in {'', '.', '..'} for p in parts):
                continue
            try:
                # Verify every missing ancestor against a successful parent listing.
                parent = '/'.join(parts[:-1])
                try:
                    entries = listing(parent)
                except HTTPError as error:
                    if error.code != 404 or not parent:
                        raise
                    error.close()
                    # A deleted directory may have events for descendants as well.
                    for index in range(1, len(parts)):
                        ancestor = '/'.join(parts[:index])
                        entries = listing('/'.join(parts[:index - 1]))
                        if parts[index - 1] not in entries:
                            self.apply_remote_entry(db, ancestor, entries)
                            break
                    else:
                        raise ValueError('Cloud parent changed while checking the update. Will retry.')
                else:
                    self.apply_remote_entry(db, relative, entries)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.errors.append(f'{relative}: {exc}')

    def tracked(self, db, relative):
        return db.execute(
            'SELECT local, remote FROM files WHERE scope = ? AND path = ?',
            (self.scope, relative),
        ).fetchone()

    def apply_remote_entry(self, db, relative, entries):
        path = self.root / relative
        if path.resolve() != path:
            raise ValueError('A symbolic link occupies the local path.')
        name = path.name
        if name in entries and entries[name] is None:
            if path.exists() and not path.is_dir():
                raise ValueError('A local file has the same name as a cloud folder.')
            path.parent.mkdir(parents=True, exist_ok=True)
            self.walk(db, relative, create_remote_folders=False)
            return
        if name in entries:
            if path.is_dir():
                raise ValueError('A local folder has the same name as a cloud file.')
            path.parent.mkdir(parents=True, exist_ok=True)
            self.reconcile(db, relative, path, entries[name], self.tracked(db, relative))
            return
        if '/' not in relative:
            raise ValueError('Selected cloud folder is no longer available.')
        prefix = relative + '/'
        if path.is_dir():
            rows = db.execute(
                'SELECT path, local, remote FROM files WHERE scope = ? AND substr(path, 1, ?) = ?',
                (self.scope, len(prefix), prefix),
            ).fetchall()
            directories = {path}
            for child, local_hash, cloud_version in rows:
                if self.stopped():
                    return
                child_path = self.root / child
                if child_path.resolve() != child_path:
                    continue
                directories.update(p for p in child_path.parents if p.is_relative_to(path))
                try:
                    self.reconcile(db, child, child_path, None, (local_hash, cloud_version))
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.errors.append(f'{child}: {exc}')
            for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
                if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                    directory.rmdir()
        else:
            self.reconcile(db, relative, path, None, self.tracked(db, relative))

    def sync_changes(self, db):
        if self.take_changes is None or self.stopped():
            return
        by_parent = {}
        for relative in self.take_changes():
            parts = Path(relative).parts
            if len(parts) < 2 or parts[0] not in self.selected or '..' in parts:
                continue
            parent, name = relative.rsplit('/', 1)
            by_parent.setdefault(parent, []).append(name)
        for parent, names in by_parent.items():
            folder = self.root / parent
            if not folder.is_dir() or folder.resolve() != folder:
                continue
            remote = None
            for name in names:
                if self.stopped():
                    return
                relative = parent + '/' + name
                path = folder / name
                try:
                    if path.is_symlink() or (path.exists() and not path.is_file()):
                        continue
                    previous = db.execute(
                        'SELECT local, remote FROM files WHERE scope = ? AND path = ?',
                        (self.scope, relative),
                    ).fetchone()
                    if fingerprint(path) == (previous[0] if previous else None):
                        continue
                    if remote is None:
                        remote = self.dav.list(parent)
                    if name in remote and remote[name] is None:
                        raise ValueError('A cloud folder has the same name as the local file.')
                    self.reconcile(db, relative, path, remote.get(name), previous)
                except HTTPError as exc:
                    self.errors.append(f'{relative}: {exc}')
                    exc.close()
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.errors.append(f'{relative}: {exc}')

    def walk(self, db, relative, *, create_remote_folders=True):
        self.sync_changes(db)
        if self.stopped():
            return
        if self.scanning is not None:
            self.scanning(relative)
        folder = self.root / relative
        if folder.is_symlink():
            raise ValueError('A symbolic link occupies the local folder.')
        folder.mkdir(exist_ok=True)
        remote = self.dav.list(relative)
        local = local_entries(folder)
        prefix = relative + '/'
        tracked = {
            path[len(prefix):]: (local_hash, remote_version)
            for path, local_hash, remote_version in db.execute(
                'SELECT path, local, remote FROM files WHERE scope = ? AND substr(path, 1, ?) = ?',
                (self.scope, len(prefix), prefix),
            )
            if '/' not in path[len(prefix):]
        }
        folders = {name for name, entry in remote.items() if entry is None}
        folders.update(name for name, is_folder in local.items() if is_folder)
        names = remote.keys() | local.keys() | tracked.keys()
        for name in sorted(names, key=lambda name: (name in folders, name)):
            if self.stopped():
                return
            child = prefix + name
            path = folder / name
            if path.is_symlink():
                continue
            cloud = remote.get(name)
            try:
                if name in remote and cloud is None:
                    if local.get(name) is False:
                        raise ValueError('A local file has the same name as a cloud folder.')
                    self.walk(db, child, create_remote_folders=create_remote_folders)
                elif local.get(name) is True:
                    if cloud is not None:
                        raise ValueError('A cloud file has the same name as a local folder.')
                    if not create_remote_folders:
                        self.apply_remote_entry(db, child, remote)
                        continue
                    self.dav.mkdir(child)
                    self.walk(db, child)
                else:
                    self.reconcile(db, child, path, cloud, tracked.get(name))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                self.errors.append(f'{child}: {exc}')

    def remember(self, db, relative, local, remote, *, transfer=None):
        db.execute('INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)',
                   (self.scope, relative, local, remote))
        if transfer is not None:
            size, direction = transfer
            db.execute('INSERT OR REPLACE INTO transfers VALUES (?, ?, ?, ?)',
                       (self.scope, relative, size, direction))
        db.commit()
        if transfer is not None and self.transferred is not None:
            self.transferred(relative, size, direction)

    def reconcile(self, db, relative, path, cloud, previous):
        local = fingerprint(path)
        version = cloud.version if cloud else None
        if previous is None:
            if local is not None and cloud is not None:
                with self.dav.download(relative, cloud) as stream:
                    identical = hashlib.file_digest(stream, 'sha256').hexdigest() == local
                if identical:
                    self.remember(db, relative, local, version)
                else:
                    self.errors.append(f'{relative}: Existing copies differ. Move the local copy aside to choose the cloud copy, or rename it to keep both.')
                return
            old_local, old_remote = None, None
        else:
            old_local, old_remote = previous
        if local != old_local:
            if local is None:
                if cloud is not None and version != old_remote:
                    downloaded = self.receive(relative, path, cloud, None)
                    if downloaded is not None:
                        digest, size = downloaded
                        self.remember(db, relative, digest, version, transfer=(size, 'Downloaded'))
                else:
                    if cloud is not None:
                        self.report(f'Deleting {relative} from myCloud')
                        if self.stopped() or path.exists() or path.is_symlink():
                            return
                        self.dav.delete(relative, cloud)
                        activity = 'Deleted from myCloud'
                    else:
                        activity = 'Removed from sync'
                    self.remember(db, relative, None, None, transfer=(None, activity))
                return
            if version != old_remote:
                if cloud is None:
                    raise ValueError('Sync conflict: edited locally and deleted in the cloud. Local copy kept.')
                with self.dav.download(relative, cloud) as stream:
                    cloud_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
                if cloud_hash == local:
                    self.remember(db, relative, local, version)
                    return
                if cloud_hash != old_local:
                    raise ValueError('Sync conflict: local and cloud edits differ. Both copies kept.')
            self.report(f'Uploading {relative}')
            with tempfile.TemporaryFile() as snapshot:
                with path.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        snapshot.write(chunk)
                size = snapshot.tell()
                snapshot.seek(0)
                uploaded_hash = hashlib.file_digest(snapshot, 'sha256').hexdigest()
                snapshot.seek(0)
                if uploaded_hash != local or fingerprint(path) != local:
                    raise ValueError('File changed during preparation. Will retry.')
                if self.stopped():
                    return
                try:
                    tag = self.dav.upload(relative, snapshot, size, cloud)
                except HTTPError as exc:
                    if exc.code == 412:
                        exc.close()
                        raise ValueError('Cloud copy changed before upload. Local copy kept.') from exc
                    raise
            cloud_file = self.dav.list(relative.rsplit('/', 1)[0]).get(path.name)
            if cloud_file is None:
                raise ValueError('Upload could not be confirmed. Will retry.')
            if tag != cloud_file.version:
                with self.dav.download(relative, cloud_file) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != uploaded_hash:
                        raise ValueError('The cloud file changed immediately after upload. Will retry.')
            self.remember(db, relative, uploaded_hash, cloud_file.version, transfer=(size, 'Uploaded'))
        elif version != old_remote:
            if local is not None and self.is_open(path):
                self.waiting.append(relative)
                return
            if cloud is None:
                if self.stopped() or path.is_symlink() or fingerprint(path) != local:
                    return
                path.unlink(missing_ok=True)
                self.remember(db, relative, None, None, transfer=(None, 'Removed locally'))
            else:
                downloaded = self.receive(relative, path, cloud, local)
                if downloaded is not None:
                    digest, size = downloaded
                    transfer = (size, 'Downloaded') if digest != local else None
                    self.remember(db, relative, digest, version, transfer=transfer)

    def receive(self, relative, path, cloud, local):
        self.report(f'Downloading {relative}')
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.mycloud-transfer-', delete=False) as stream:
            digest = hashlib.sha256()
            temporary = Path(stream.name)
            try:
                with self.dav.download(relative, cloud) as response:
                    while chunk := response.read(1024 * 1024):
                        if self.stopped():
                            return None
                        stream.write(chunk)
                        digest.update(chunk)
                    length = response.headers.get('Content-Length')
                    if length is not None and stream.tell() != int(length):
                        raise ValueError('Download ended before all bytes arrived. Will retry.')
                stream.flush()
                if self.stopped():
                    return None
                if (path.is_symlink() or (local is not None and self.is_open(path))
                        or fingerprint(path) != local):
                    self.waiting.append(relative)
                    return None
                received = digest.hexdigest()
                if received != local:
                    if local is not None:
                        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
                    os.replace(temporary, path)
                return received, stream.tell()
            finally:
                temporary.unlink(missing_ok=True)
