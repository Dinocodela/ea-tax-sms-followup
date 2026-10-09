"""Run with Python 3.10+: python app.py. Dashboard binds to localhost only."""
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
from urllib.parse import urlsplit, parse_qs
from engine import Engine
from drafts import DraftError, draft_campaign, email_text, personalize
from providers import APIError

ROOT = Path(__file__).resolve().parent


def configuration():
    config = {}
    if (ROOT / '.env').exists():
        for line in (ROOT / '.env').read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                config[k.strip()] = v.strip().strip('\"\'')
    config.update(os.environ)
    if config.get('DATA_MODE', 'demo') not in ('demo', 'live'):
        raise ValueError('DATA_MODE must be demo or live')
    return config


def authorized_mutation(headers, port, csrf):
    return (headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')
            and headers.get('X-CSRF-Token') == csrf
            and headers.get('Origin') in (f'http://127.0.0.1:{port}', f'http://localhost:{port}'))


def serve():
    config = configuration()
    # Acquire ownership before opening or recovering the database.
    lock_socket = socket.socket()
    try:
        lock_socket.bind(('127.0.0.1', int(config.get('LOCK_PORT', '5001'))))
        run_server(config)
    finally:
        lock_socket.close()


def run_server(config):
    engine = Engine(ROOT, config)
    csrf = secrets.token_urlsafe(32)
    port = int(config.get('PORT', '5000'))
    origin = f'http://127.0.0.1:{port}'
    stop = threading.Event()
    poll_requested = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, value, html=False):
            payload = value.encode() if html else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8' if html else 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def valid_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}')

        def do_GET(self):
            if not self.valid_host():
                return self.respond(403, {'error': 'Invalid host'})
            if self.path == '/':
                return self.respond(200, (ROOT / 'templates/dashboard.html').read_text().replace('__CSRF__', csrf), True)
            if self.path == '/api/campaigns':
                try:
                    return self.respond(200, {'campaigns': engine.campaigns()})
                except APIError as e:
                    return self.respond(502, {'error': 'Mixmax campaigns: ' + str(e)})
                except Exception:
                    return self.respond(502, {'error': 'Campaigns could not be loaded. Please try again.'})
            url = urlsplit(self.path)
            if url.path == '/api/campaign':
                try:
                    stages = engine.campaign_stages(parse_qs(url.query).get('id', [''])[0])
                    return self.respond(200, {'stages': [{'id': s['id'], 'number': s['number'], 'subject': s['subject'], 'preview': email_text(s['body'])[:400]} for s in stages]})
                except APIError as e:
                    return self.respond(502, {'error': 'Mixmax campaign: ' + str(e)})
                except Exception:
                    return self.respond(502, {'error': 'Campaign stages could not be loaded. Please try again.'})
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == '/api/recipients':
                    return self.respond(200, engine.recipients(query.get('q', ''), query.get('status', ''), query.get('page', 1), query.get('campaign', ''), query.get('stage', '')))
                if url.path == '/api/recipient':
                    return self.respond(200, engine.recipient(query.get('email', '')))
                if url.path == '/api/jobs':
                    return self.respond(200, engine.jobs_page(query.get('q', ''), query.get('status', ''), query.get('page', 1), query.get('campaign', '')))
            except ValueError as e:
                return self.respond(400, {'error': str(e)})
            if self.path == '/api/state':
                return self.respond(200, engine.state())
            self.respond(404, {'error': 'Not found'})

        def do_POST(self):
            if not authorized_mutation(self.headers, port, csrf):
                return self.respond(403, {'error': 'Local dashboard request required'})
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 16000:
                    raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(size))
                if self.path == '/api/rule':
                    engine.save_selected_rule(data)
                    return self.respond(200, {'ok': True})
                if self.path == '/api/track':
                    return self.respond(200, engine.set_tracked(str(data['campaign']), bool(data['on'])))
                if self.path == '/api/rules':
                    engine.save_campaign_rules(data)
                    return self.respond(200, {'ok': True})
                if self.path == '/api/bulk':
                    with engine.lock:
                        return self.respond(200, engine.bulk(data['action'], [int(i) for i in data['ids']]))
                if self.path == '/api/personalize':
                    # Suggestion only; the reviewer saves or approves it separately.
                    return self.respond(200, {'text': personalize(config, engine.personalize_context(data['id']))})
                if self.path == '/api/draft':
                    # Drafts are returned for review only; nothing is saved until the user saves the rules.
                    drafts, note = draft_campaign(config, engine.campaign_stages(str(data.get('campaign', ''))))
                    return self.respond(200, {'drafts': drafts, 'note': note})
                with engine.lock:
                    if self.path == '/api/flag':
                        engine.flag(data['email'], data['flag'], bool(data.get('value', True)))
                    elif self.path == '/api/lead-phone':
                        engine.choose_phone(data['email'], data['phone'])
                    elif self.path == '/api/phone-stop':
                        engine.clear_phone_stop(data['phone'], data['note'])
                    elif self.path == '/api/job':
                        engine.job_action(data['id'], data['action'], data.get('text'))
                    elif self.path == '/api/demo':
                        if not engine.demo:
                            raise ValueError('Demo events are disabled in live data mode')
                        email = data.get('email', 'john@example.com').strip().casefold()
                        event_id = data.get('event_id') or 'demo-' + secrets.token_hex(4)
                        engine.demo_roster(email, data.get('stage', '1'))
                        engine.ingest({'_id': event_id, 'sent': time.time(), 'sequence': {'id': 'demo', 'stageId': str(data.get('stage', '1')), 'recipientId': email}}, {'email': email, 'name': 'John'}, [dict({'email': email, 'mobilePhone': '+12025550123'}, **({'mobilePhone2': '+12025550188', 'homePhone': '+12025550177'} if 'multi' in email else {}))])
                    elif self.path == '/api/poll':
                        # API requests can take time; run off the HTTP request thread.
                        poll_requested.set()
                    else:
                        return self.respond(404, {'error': 'Not found'})
                self.respond(200, {'ok': True})
            except DraftError as e:
                self.respond(502, {'error': str(e)})
            except APIError as e:
                self.respond(502, {'error': 'Mixmax: ' + str(e)})
            except (ValueError, KeyError, TypeError) as e:
                self.respond(400, {'error': str(e)})
            except Exception:
                self.respond(500, {'error': 'Operation failed; inspect local logs'})

    def worker():
        while not stop.is_set():
            force = poll_requested.is_set()
            poll_requested.clear()
            engine.cycle(force=force)
            poll_requested.wait(5)

    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except Exception:
        engine.db.close()
        raise
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    print(f'EA Tax SMS PoC: {origin} | data={"demo" if engine.demo else "live"} | test={engine.test}', flush=True)
    webbrowser.open(origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        poll_requested.set()
        server.server_close()
        worker_thread.join()
        with engine.cycle_lock:
            engine.db.close()


if __name__ == '__main__':
    serve()
