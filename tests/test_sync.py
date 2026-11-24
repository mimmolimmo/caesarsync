import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from urllib.parse import quote, unquote
import xml.etree.ElementTree as ET

from sync import Sync, WebDAV, last_transfer


def tag(content):
    return '"' + hashlib.sha256(content).hexdigest() + '"'


class DAVHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body=b'', **headers):
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def path_key(self):
        return unquote(self.path).removeprefix('/dav/').rstrip('/')

    def do_PROPFIND(self):
        self.rfile.read(int(self.headers['Content-Length']))
        if self.headers['Depth'] != '1':
            self.reply(400)
            return
        folder = self.path_key()
        if folder not in self.server.files or self.server.files[folder] is not None:
            self.reply(404)
            return
        listing = ET.Element('{DAV:}multistatus')
        prefix = folder + '/' if folder else ''
        for name, content in self.server.files.items():
            if name != folder and (
                not name.startswith(prefix) or '/' in name[len(prefix):] or not name
            ):
                continue
            entry = ET.SubElement(listing, '{DAV:}response')
            ET.SubElement(entry, '{DAV:}href').text = '/dav/' + quote(name)
            propstat = ET.SubElement(entry, '{DAV:}propstat')
            ET.SubElement(propstat, '{DAV:}status').text = 'HTTP/1.1 200 OK'
            prop = ET.SubElement(propstat, '{DAV:}prop')
            kind = ET.SubElement(prop, '{DAV:}resourcetype')
            if content is None:
                ET.SubElement(kind, '{DAV:}collection')
            else:
                ET.SubElement(prop, '{DAV:}getetag').text = tag(content)
        self.reply(207, ET.tostring(listing))

    def do_GET(self):
        content = self.server.files.get(self.path_key())
        if content is None:
            self.reply(404)
        elif self.headers.get('If-Match', tag(content)) != tag(content):
            self.reply(412)
        else:
            self.reply(200, content, ETag=tag(content))

    def do_PUT(self):
        content = self.rfile.read(int(self.headers['Content-Length']))
        current = self.server.files.get(self.path_key())
        if (self.headers.get('If-None-Match') == '*' and current is not None
                or self.headers.get('If-Match') not in (None, tag(current or b''))
                or self.headers.get('If-Match') is not None and current is None):
            self.reply(412)
            return
        if self.path_key().endswith('.png') and self.headers.get('Content-Type') != 'image/png':
            self.reply(500)
            return
        self.server.files[self.path_key()] = content
        self.reply(201, **getattr(self.server, 'upload_headers', {}))

    def do_MKCOL(self):
        self.server.files[self.path_key()] = None
        self.reply(201)

    def do_DELETE(self):
        key = self.path_key()
        content = self.server.files.get(key)
        if content is None:
            self.reply(404)
        elif self.headers.get('If-Match', tag(content)) != tag(content):
            self.reply(412)
        else:
            del self.server.files[key]
            self.reply(204)


class SyncTests(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name) / 'myCloud'
        self.db = Path(workspace.name) / 'test.sqlite3'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), DAVHandler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.cloud = self.server.files = {
            '': None, 'Selected': None, 'Other': None,
            'Selected/Café notes.txt': b'original', 'Other/skip.txt': b'untouched',
        }
        self.dav = WebDAV('https://example.invalid/dav/', 'test', 'test')
        self.dav.address = f'http://127.0.0.1:{self.server.server_port}/dav/'
        self.path = self.root / 'Selected/Café notes.txt'

    def scan(self, **kwargs):
        sync = Sync(self.dav, self.root, self.db, 'test', lambda _: None, lambda: False, **kwargs)
        waiting, errors = sync.run(['Selected'])
        self.assertEqual(errors, [])
        return waiting

    def test_last_transfer_tracks_completed_file_sizes(self):
        relative = 'Selected/Café notes.txt'
        completions = []
        self.assertIsNone(last_transfer(self.db, 'test'))
        self.scan(transferred=lambda *transfer: completions.append(transfer))
        original = (relative, 8, 'Downloaded')
        self.assertEqual(completions, [original])
        self.assertEqual(last_transfer(self.db, 'test'), original)
        self.assertIsNone(last_transfer(self.db, 'other'))

        self.cloud[relative] = b'remote update'
        self.assertEqual(self.scan(is_open=lambda _: True), [relative])
        self.assertEqual(last_transfer(self.db, 'test'), original)
        self.scan(is_open=lambda _: False)
        self.assertEqual(last_transfer(self.db, 'test'), (relative, 13, 'Downloaded'))

        self.path.write_bytes(b'')
        self.scan()
        uploaded = (relative, 0, 'Uploaded')
        self.assertEqual(last_transfer(self.db, 'test'), uploaded)
        self.scan()
        self.path.unlink()
        self.scan()
        self.assertEqual(last_transfer(self.db, 'test'), (relative, None, 'Deleted from myCloud'))

    def test_upload_with_different_listing_etag_is_not_reported_as_download(self):
        relative = 'Selected/new.txt'
        self.root.joinpath('Selected').mkdir(parents=True)
        path = self.root / relative
        path.write_bytes(b'local upload')
        self.server.upload_headers = {'ETag': '"upload-response-tag"'}
        self.cloud.pop('Selected/Café notes.txt')
        self.scan()
        uploaded = (relative, 12, 'Uploaded')
        self.assertEqual(last_transfer(self.db, 'test'), uploaded)
        inode = path.stat().st_ino
        self.scan()
        self.assertEqual(last_transfer(self.db, 'test'), uploaded)
        self.assertEqual(path.stat().st_ino, inode)

    def test_selected_folder_and_nested_upload(self):
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.path.read_bytes(), b'original')
        self.assertFalse((self.root / 'Other').exists())
        nested = self.path.parent / 'New folder'
        nested.mkdir()
        (nested / 'preview.png').write_bytes(b'png fixture')
        self.scan()
        self.assertEqual(self.cloud['Selected/New folder/preview.png'], b'png fixture')

    def test_simultaneous_edits_preserve_both_copies(self):
        self.scan()
        with self.path.open('rb'):
            self.cloud['Selected/Café notes.txt'] = b'Mac edit'
            self.assertEqual(self.scan(), ['Selected/Café notes.txt'])
            self.assertEqual(self.path.read_bytes(), b'original')
            self.path.write_bytes(b'Linux save')
            sync = Sync(self.dav, self.root, self.db, 'test', lambda _: None, lambda: False)
            waiting, errors = sync.run(['Selected'])
            self.assertEqual(waiting, [])
            self.assertIn('Sync conflict', errors[0])
            self.assertEqual(self.cloud['Selected/Café notes.txt'], b'Mac edit')
            self.assertEqual(self.path.read_bytes(), b'Linux save')

    def test_upload_rejects_cloud_edit_after_listing(self):
        self.scan()
        relative = 'Selected/Café notes.txt'
        self.path.write_bytes(b'local edit')
        upload = self.dav.upload

        def race(*args):
            self.cloud[relative] = b'new cloud edit'
            return upload(*args)

        self.dav.upload = race
        sync = Sync(self.dav, self.root, self.db, 'test', lambda _: None, lambda: False)
        _, errors = sync.run(['Selected'])
        self.assertIn('Cloud copy changed before upload', errors[0])
        self.assertEqual(self.cloud[relative], b'new cloud edit')
        self.assertEqual(self.path.read_bytes(), b'local edit')

    def test_incoming_update_applies_after_file_handle_closes(self):
        self.scan()
        with self.path.open('rb'):
            self.cloud['Selected/Café notes.txt'] = b'Mac edit'
            self.assertEqual(self.scan(), ['Selected/Café notes.txt'])
        self.assertEqual(self.scan(), [])
        self.assertEqual(self.path.read_bytes(), b'Mac edit')

    def test_local_deletion_propagates_and_stays_deleted(self):
        self.scan()
        self.path.unlink()
        self.scan()
        self.scan()
        self.assertFalse(self.path.exists())
        self.assertNotIn('Selected/Café notes.txt', self.cloud)
        self.assertIn('Other/skip.txt', self.cloud)

    def test_cloud_deletion_waits_for_open_local_file(self):
        self.scan()
        with self.path.open('rb'):
            del self.cloud['Selected/Café notes.txt']
            self.assertEqual(self.scan(), ['Selected/Café notes.txt'])
            self.assertTrue(self.path.exists())
        self.scan()
        self.assertFalse(self.path.exists())
        self.assertEqual(last_transfer(self.db, 'test'),
                         ('Selected/Café notes.txt', None, 'Removed locally'))

    def test_local_deletion_preserves_a_concurrent_remote_edit(self):
        self.scan()
        self.path.unlink()
        self.cloud['Selected/Café notes.txt'] = b'Mac edit'
        self.scan()
        self.assertEqual(self.path.read_bytes(), b'Mac edit')


if __name__ == '__main__':
    unittest.main()
