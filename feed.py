"""Swisscom desktop sign-in and read-only incremental change queries."""
import base64
from email.utils import parsedate_to_datetime
import json
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


VERSION = '1.0'

LOGIN = (
    'https://support.prod.mdl.swisscom.ch/login?'
    'client_id=7726accd-55c7-4384-b7ab-e41c95d886f3&response_type=code&application_type=desktop'
)
BROKER = 'https://login.mycloud.swisscom.ch'
IDENTITY = 'https://identity.prod.mdl.swisscom.ch'
STREAM = 'https://syncstorage.prod.mdl.swisscom.ch/v3/sync/stream'
CALLBACK_HOSTS = {
    'www.mycloud.ch', 'www.mycloud.swisscom.ch', 'support.prod.mdl.swisscom.ch',
    'login.mycloud.swisscom.ch',
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def request(url, *, token=None, data=None):
    headers = {'User-Agent': f'CaesarSync/{VERSION}'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    return build_opener(NoRedirect()).open(Request(url, data, headers), timeout=12)


def login_code(uri):
    url = urlsplit(uri)
    if url.scheme != 'https' or url.netloc not in CALLBACK_HOSTS:
        return None
    if url.path.rstrip('/') not in {'/login', '/login/success'}:
        return None
    codes = (parse_qs(url.query) or parse_qs(url.fragment)).get('code', [])
    return codes[0] if len(codes) == 1 else None


def authenticate(code, username):
    form = urlencode({'grant_type': 'authorization_code', 'code': code}).encode()
    with request(IDENTITY + '/get-token', data=form) as response:
        token = json.load(response).get('access_token')
    if not isinstance(token, str) or not token:
        raise ValueError('Swisscom returned no session token.')
    with request(IDENTITY + '/me', token=token) as response:
        identity = json.load(response)
    names = {str(identity.get(field, '')).casefold()
             for field in ('Identifier', 'UserName', 'Email')}
    if username.casefold() not in names:
        raise ValueError('Swisscom sign-in does not match the configured WebDAV account.')
    return token


def read_events(token, since):
    drive = base64.urlsafe_b64encode(b'/Drive/').decode().rstrip('=')
    url = STREAM + '?' + urlencode({'p': drive, 'since': since})
    paths = set()
    with request(url, token=token) as response:
        if response.status != 200:
            raise ValueError('Swisscom returned an incomplete file-change response.')
        if response.headers.get_content_type() in {'text/html', 'application/xhtml+xml'}:
            raise ValueError('Swisscom returned an unexpected file-change response.')
        date = response.headers.get('Date')
        if not date:
            raise ValueError('Swisscom returned no timestamp for the file-change response.')
        # Keep an overlap for events that are still becoming visible on the server.
        checkpoint = int(parsedate_to_datetime(date).timestamp() * 1000) - 30_000
        for line in response:
            if not line.startswith(b'data:'):
                if line.strip() and not line.startswith((b':', b'event:', b'id:', b'retry:')):
                    raise ValueError('Swisscom returned an unexpected file-change record.')
                continue
            record = json.loads(line[5:])
            path = record.get('Path') if isinstance(record, dict) else None
            if not isinstance(path, str) or not path.startswith('/Drive/'):
                raise ValueError('Swisscom returned an unrecognized file-change path.')
            parts = path[len('/Drive/'):].rstrip('/').split('/')
            if parts == ['']:
                continue
            if any(part in {'', '.', '..'} or '\x00' in part for part in parts):
                raise ValueError('Swisscom returned an invalid file-change path.')
            paths.add('/'.join(parts))
    return max(since, checkpoint), paths
