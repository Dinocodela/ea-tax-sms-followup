"""Outbound-only API adapters. No HTTP send retry: acceptance may be ambiguous."""
import base64
import json
import time
import re
import socket
import ssl
from urllib.request import Request, urlopen
from urllib.parse import urlencode, quote
from urllib.error import HTTPError, URLError


class APIError(Exception):
    pass


def safe_detail(value, secrets=()):
    text = str(value)
    for secret in sorted((str(x) for x in secrets if x), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(bearer|basic)\s+\S+', r'\1 [REDACTED]', text)
    text = re.sub(r'(?i)((?:access_token|refresh_token|client_secret|assertion|jwt|api[_-]?token)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+', '[EMAIL]', text)
    text = re.sub(r'\+\d[\d ()-]{7,}', '[PHONE]', text)
    return ' '.join(text.split())[:500]


def request(url, headers=None, data=None, form=False, timeout=20):
    headers = dict(headers or {})
    secrets = list(headers.values())
    auth = headers.get('Authorization', '').split(' ', 1)
    if len(auth) == 2:
        secrets.append(auth[1])
        if auth[0] == 'Basic':
            try:
                secrets.extend(base64.b64decode(auth[1]).decode().split(':', 1))
            except (ValueError, UnicodeError):
                pass
    if isinstance(data, dict):
        secrets.extend(data.get(k) for k in ('assertion', 'client_secret', 'access_token', 'refresh_token'))
    if data is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        data = (urlencode(data) if form else json.dumps(data)).encode()
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=timeout) as response:
            return json.load(response)
    except HTTPError as e:
        details = []
        try:
            body = json.loads(e.read(16384))
            if isinstance(body, dict):
                sources = [body]
                if isinstance(body.get('error'), dict):
                    sources.append(body['error'])
                if isinstance(body.get('errors'), list):
                    sources.extend(x for x in body['errors'][:3] if isinstance(x, dict))
                for source in sources:
                    for key in ('errorCode', 'code', 'error', 'error_description', 'message'):
                        if isinstance(source.get(key), (str, int)):
                            detail = f'{key}: {safe_detail(source[key], secrets)}'
                            if detail not in details:
                                details.append(detail)
        except (ValueError, OSError):
            pass
        hints = {
            400: 'Check request configuration and the provider error details.',
            401: 'Check credentials and the configured provider environment; credentials may be invalid or expired.',
            403: 'Check app permissions and account access for this operation.',
            404: 'Check the requested resource and account access.',
            429: 'Wait before retrying; the provider rate limit was reached.',
        }
        retry = e.headers.get('Retry-After') if e.headers else None
        if retry:
            details.append('Retry-After: ' + safe_detail(retry, secrets))
        hint = hints.get(e.code, 'Retry later if the provider is unavailable.' if e.code >= 500 else 'Check the provider configuration.')
        raise APIError(f'HTTP {e.code}' + ('; ' + '; '.join(details) if details else '; no structured provider error details available') + f' Next step: {hint}') from None
    except (URLError, TimeoutError, ssl.SSLError, OSError) as e:
        reason = e.reason if isinstance(e, URLError) else e
        if isinstance(reason, socket.gaierror):
            detail = 'DNS lookup failed. Check your network connection and DNS settings.'
        elif isinstance(reason, ssl.SSLCertVerificationError):
            detail = 'TLS certificate verification failed. Check your system clock, CA certificates, or HTTPS proxy.'
        elif isinstance(reason, ssl.SSLError):
            detail = 'TLS connection failed. Check HTTPS connectivity and proxy settings.'
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            detail = 'Request timed out. Check connectivity or retry when the provider responds.'
        elif isinstance(reason, ConnectionRefusedError):
            detail = 'Connection refused. Check connectivity, firewall, and proxy settings.'
        else:
            detail = 'Connection failed. Check network, firewall, and proxy settings.'
        raise APIError(detail) from None
    except (ValueError, UnicodeError):
        raise APIError('Provider returned invalid JSON. Check provider availability or an intervening proxy.') from None


def records(response, operation):
    if not isinstance(response, dict) or not isinstance(response.get('records'), list) or not all(isinstance(r, dict) for r in response['records']):
        raise APIError(f'{operation}: expected a JSON object containing a records list of objects')
    return response['records']


class Mixmax:
    def __init__(self, config):
        self.token = config.get('MIXMAX_API_TOKEN', '')

    def get(self, path, params=None):
        if not self.token:
            raise APIError('Missing MIXMAX_API_TOKEN')
        return request('https://api.mixmax.com/v1/' + path + ('?' + urlencode(params) if params else ''), {'X-API-Token': self.token})

    def collection(self, path):
        params = {'limit': 100}
        results = []
        for _ in range(50):
            page = self.get(path, params)
            if not isinstance(page, dict) or not isinstance(page.get('results'), list):
                raise APIError('Unexpected Mixmax collection format')
            results.extend(page['results'])
            if not page.get('hasNext'):
                return results
            if not page.get('next'):
                raise APIError('Missing Mixmax pagination cursor')
            params['next'] = page['next']
        raise APIError('Mixmax scan exceeds 50 pages; no dispatch this cycle')

    def recipients(self, sequence):
        result = []
        for offset in range(0, 10000, 50):
            page = self.get('sequences/' + quote(sequence, safe='') + '/recipients', {'limit': 50, 'offset': offset, 'includeVariables': 'true'})
            if not isinstance(page, list):
                raise APIError('Unexpected Mixmax recipient format')
            result.extend(page)
            if len(page) < 50:
                return result
        raise APIError('Recipient scan reached 10,000 limit')


class RingCentral:
    base_path = '/restapi/v1.0/account/~/extension/~/'

    def __init__(self, config):
        self.config = config
        self.base = config.get('RC_SERVER_URL', 'https://platform.ringcentral.com').rstrip('/')
        if self.base not in ('https://platform.ringcentral.com', 'https://platform.devtest.ringcentral.com'):
            raise APIError('RC_SERVER_URL must be an official RingCentral API host')
        self.token = ''
        self.expiry = 0

    def authenticate(self):
        if self.token and time.time() < self.expiry:
            return
        c = self.config
        if not all(c.get(k) for k in ('RC_CLIENT_ID', 'RC_CLIENT_SECRET', 'RC_JWT')):
            raise APIError('Missing RingCentral configuration: ' + ', '.join(k for k in ('RC_CLIENT_ID', 'RC_CLIENT_SECRET', 'RC_JWT') if not c.get(k)))
        basic = base64.b64encode((c['RC_CLIENT_ID'] + ':' + c['RC_CLIENT_SECRET']).encode()).decode()
        result = request(self.base + '/restapi/oauth/token', {'Authorization': 'Basic ' + basic}, {'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': c['RC_JWT']}, form=True)
        if not isinstance(result, dict) or not isinstance(result.get('access_token'), str) or not result.get('access_token'):
            raise APIError('RingCentral authentication: response missing access_token')
        try:
            lifetime = float(result['expires_in'])
        except (KeyError, TypeError, ValueError):
            raise APIError('RingCentral authentication: response missing valid expires_in') from None
        self.token = result['access_token']
        self.expiry = time.time() + lifetime - 60

    def call(self, path, params=None, data=None):
        self.authenticate()
        return request(self.base + self.base_path + path + ('?' + urlencode(params) if params else ''), {'Authorization': 'Bearer ' + self.token}, data)

    def contacts(self):
        result = []
        for page in range(1, 101):
            response = self.call('address-book/contact', {'page': page, 'perPage': 100})
            result.extend(records(response, 'RingCentral contacts'))
            if page >= response.get('paging', {}).get('totalPages', 1):
                return result
        raise APIError('Contact scan exceeds 100 pages')

    def validate_sender(self):
        sender = self.config.get('RC_SENDER_NUMBER', '')
        if not sender:
            raise APIError('Missing RC_SENDER_NUMBER; configure the sending extension phone number')
        response = self.call('phone-number', {'perPage': 100})
        if not any(r.get('phoneNumber') == sender and 'SmsSender' in r.get('features', []) for r in records(response, 'RingCentral sender validation')):
            raise APIError('Configured sender is not an SmsSender on this extension')

    def sync(self, token, since):
        params = {'syncType': 'ISync', 'syncToken': token} if token else {'syncType': 'FSync', 'dateFrom': since, 'messageType': 'SMS', 'recordCount': 250}
        result = self.call('message-sync', params)
        if result.get('syncInfo', {}).get('olderRecordsExist'):
            raise APIError('Reply sync truncated; resolve backlog before dispatch')
        if not result.get('syncInfo', {}).get('syncToken'):
            raise APIError('Reply sync missing token')
        return result

    def send(self, phone, text):
        return self.call('sms', data={'from': {'phoneNumber': self.config['RC_SENDER_NUMBER']}, 'to': [{'phoneNumber': phone}], 'text': text})
