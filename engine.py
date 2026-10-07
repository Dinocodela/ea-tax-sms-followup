import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from providers import Mixmax, RingCentral, APIError

FLAGS = ('replied', 'paused', 'converted', 'paid', 'signed', 'opted_out')


def stamp(value):
    if isinstance(value, (float, int)):
        return value / 1000 if value > 1e11 else float(value)
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def phone(value):
    value = re.sub(r'[\s().-]', '', value or '')
    return value if re.fullmatch(r'\+[1-9]\d{7,14}', value) else ''


def phone_label(field):
    # mobilePhone2 -> "Mobile 2", businessPhone -> "Business"
    words = re.sub(r'(?i)phone', ' ', field)
    words = re.sub(r'([a-z])([A-Z0-9])', r'\1 \2', words).split()
    return ' '.join(w.capitalize() for w in words) or 'Phone'


def contact_numbers(email, contacts):
    """All valid numbers on the lead's single matching RingCentral contact, as [{label, number}]."""
    matches = [r for r in contacts if email.strip().casefold() in {str(r.get(k, '')).strip().casefold() for k in ('email', 'email2', 'email3')}]
    if len(matches) != 1:
        return [], 'No unique contact match'
    options, seen = [], set()
    for field, value in matches[0].items():
        number = phone(value) if isinstance(value, str) and 'phone' in field.casefold() and 'fax' not in field.casefold() else ''
        if number and number not in seen:
            seen.add(number)
            options.append({'label': phone_label(field), 'number': number})
    options.sort(key=lambda o: (not o['label'].startswith('Mobile'), o['label']))
    return options, '' if options else 'No valid international phone number on contact'


def pick_number(chosen, options):
    """Chosen number if still on the contact; otherwise the only Mobile number; otherwise '' (needs a choice)."""
    numbers = [o['number'] for o in options]
    if chosen:
        return chosen if chosen in numbers else ''
    mobiles = [o['number'] for o in options if o['label'].startswith('Mobile')]
    return mobiles[0] if len(mobiles) == 1 else ''


class Engine:
    def __init__(self, root, config, mix=None, rc=None):
        self.root, self.config = Path(root), config
        self.demo = config.get('DATA_MODE', 'demo') == 'demo'
        self.test = config.get('TEST_MODE', 'true').lower() != 'false'
        self.lock = threading.RLock()
        self.cycle_lock = threading.Lock()
        self.db = sqlite3.connect(self.root / ('demo.sqlite3' if self.demo else 'live.sqlite3'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS leads (email TEXT PRIMARY KEY,name TEXT,phone TEXT,replied INTEGER DEFAULT 0,paused INTEGER DEFAULT 0,converted INTEGER DEFAULT 0,paid INTEGER DEFAULT 0,signed INTEGER DEFAULT 0,opted_out INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS rules (campaign TEXT,stage TEXT,enabled INTEGER,delay REAL,template TEXT,PRIMARY KEY(campaign,stage));
        CREATE TABLE IF NOT EXISTS events (id TEXT,email TEXT,PRIMARY KEY(id,email));
        CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY,event_id TEXT,email TEXT,campaign TEXT,stage TEXT,recipient_id TEXT,phone TEXT,text TEXT,due REAL,status TEXT,reason TEXT,provider_id TEXT,approved INTEGER DEFAULT 0,created REAL,UNIQUE(event_id,email));
        CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY,at REAL,message TEXT);
        CREATE TABLE IF NOT EXISTS inbound (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS phone_stops (phone TEXT PRIMARY KEY, reason TEXT);
        CREATE TABLE IF NOT EXISTS roster (campaign TEXT, recipient_id TEXT, email TEXT, name TEXT, state TEXT, reason TEXT, last_stage INTEGER, next_stage INTEGER, next_stage_id TEXT, next_at REAL, replied INTEGER DEFAULT 0, created REAL, PRIMARY KEY(campaign, recipient_id));
        CREATE INDEX IF NOT EXISTS roster_email ON roster(email);
        ''')
        if 'stage_number' not in {r[1] for r in self.db.execute('PRAGMA table_info(rules)')}:
            self.db.execute('ALTER TABLE rules ADD COLUMN stage_number INTEGER')
        self.db.execute('CREATE INDEX IF NOT EXISTS jobs_email ON jobs(email)')
        self.db.execute('CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)')
        if 'mode' not in {r[1] for r in self.db.execute('PRAGMA table_info(rules)')}:
            self.db.execute("ALTER TABLE rules ADD COLUMN mode TEXT DEFAULT 'auto'")
        if 'review' not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN review INTEGER DEFAULT 0')
        lead_columns = {r[1] for r in self.db.execute('PRAGMA table_info(leads)')}
        if 'phone_options' not in lead_columns:
            self.db.execute("ALTER TABLE leads ADD COLUMN phone_options TEXT DEFAULT '[]'")
        if 'chosen_phone' not in lead_columns:
            self.db.execute("ALTER TABLE leads ADD COLUMN chosen_phone TEXT DEFAULT ''")
        self.db.execute("UPDATE jobs SET status='unknown',reason='App stopped during send; check RingCentral before retrying' WHERE status='sending'")
        if not self.meta('baseline'):
            self.setmeta('baseline', str(time.time()))
        if self.demo:
            for stage in ('1', '3'):
                self.db.execute('INSERT OR IGNORE INTO rules(campaign,stage,enabled,delay,template) VALUES (?,?,?,?,?)', ('demo', stage, 1, 1, 'Hi {name}, Lukas from EA Tax Resolutions here. Please check my recent email and let me know if you have questions.'))
        self.db.commit()
        self.mix = mix or Mixmax(config)
        self.rc = rc or RingCentral(config)
        self.last_poll = 0
        self.next_poll = 0
        self.health = 'Demo ready' if self.demo else 'Waiting for first API poll'
        self.cycle_ok = self.demo

    def meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else ''

    def setmeta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, value))
        self.db.commit()

    def log(self, message):
        self.db.execute('INSERT INTO logs(at,message) VALUES (?,?)', (time.time(), message))
        self.db.commit()

    def rules(self):
        return [dict(x) for x in self.db.execute('SELECT * FROM rules ORDER BY campaign,stage')]

    def campaigns(self):
        if self.demo:
            return [{'id': 'demo', 'name': 'Demo campaign', 'stages': [{'id': str(n), 'number': n} for n in range(1, 4)]}]
        result = []
        for row in self.mix.collection('sequences'):
            if not isinstance(row, dict) or not (row.get('_id') or row.get('id')) or not isinstance(row.get('stages'), list):
                raise APIError('Mixmax campaigns: expected campaign IDs and an ordered stages list')
            stages = []
            for number, stage in enumerate(row['stages'], 1):
                ident = (stage.get('_id') or stage.get('id')) if isinstance(stage, dict) else stage
                if not isinstance(ident, str) or not ident:
                    raise APIError('Mixmax campaigns: stage is missing its ID')
                stages.append({'id': ident, 'number': number})
            if len({x['id'] for x in stages}) != len(stages):
                raise APIError('Mixmax campaigns: duplicate stage IDs')
            result.append({'id': str(row.get('_id') or row.get('id')), 'name': str(row.get('name') or 'Unnamed campaign'), 'stages': stages})
        return result

    def save_selected_rule(self, data):
        # Refresh outside the DB lock; reject deleted or reordered selections.
        campaigns = self.campaigns()
        campaign = next((x for x in campaigns if x['id'] == data.get('campaign')), None)
        stage = next((x for x in campaign['stages'] if x['id'] == data.get('stage')), None) if campaign else None
        if not stage:
            raise ValueError('This campaign or stage is no longer available. Refresh campaigns and choose again.')
        if stage['number'] != int(data.get('stage_number', 0)):
            raise ValueError('The stages have been reordered. Refresh campaigns and review your selection.')
        values = dict(data, _stage_number=stage['number'])
        with self.lock:
            original = data.get('original')
            if original:
                old_key = (original['campaign'], original['stage'])
                if not self.db.execute('SELECT 1 FROM rules WHERE campaign=? AND stage=?', old_key).fetchone():
                    raise ValueError('The original rule no longer exists. Reload the dashboard.')
                if old_key != (data['campaign'], data['stage']) and self.db.execute('SELECT 1 FROM rules WHERE campaign=? AND stage=?', (data['campaign'], data['stage'])).fetchone():
                    raise ValueError('A rule already exists for that stage. Edit that rule instead.')
            with self.db:
                self.save_rule(values, commit=False)
                if original and old_key != (data['campaign'], data['stage']):
                    self.db.execute("UPDATE jobs SET status='cancelled',reason='Rule moved to another stage' WHERE campaign=? AND stage=? AND status IN ('pending','needs_number','draft')", old_key)
                    self.db.execute('DELETE FROM rules WHERE campaign=? AND stage=?', old_key)

    def save_rule(self, data, commit=True):
        campaign, stage = str(data['campaign']).strip(), str(data['stage']).strip()
        delay = float(data['delay'])
        template = str(data['template']).strip()
        if not campaign or not stage or not 0 <= delay <= 43200 or not template or len(template) > 1000:
            raise ValueError('Provide campaign, stage, template and delay between 0 and 43200 minutes')
        # Reject unsupported template variables before jobs are generated.
        try:
            template.format(name='Test', email='test@example.com')
        except (KeyError, IndexError, ValueError):
            raise ValueError('SMS text can only use the placeholders {name} and {email}, each in curly braces') from None
        existing = self.db.execute('SELECT mode FROM rules WHERE campaign=? AND stage=?', (campaign, stage)).fetchone()
        mode = data.get('mode') or (existing['mode'] if existing else '') or 'auto'
        if mode not in ('auto', 'review'):
            raise ValueError('Send mode must be automatic or review')
        self.db.execute('INSERT OR REPLACE INTO rules(campaign,stage,enabled,delay,template,stage_number,mode) VALUES (?,?,?,?,?,?,?)', (campaign, stage, int(bool(data.get('enabled'))), delay, template, data.get('_stage_number'), mode))
        if not data.get('enabled'):
            self.db.execute("UPDATE jobs SET status='cancelled',reason='Stage disabled' WHERE campaign=? AND stage=? AND status IN ('pending','needs_number','draft')", (campaign, stage))
        if commit:
            self.db.commit()

    def campaign_stages(self, campaign_id):
        """Every stage of one campaign, in order, with its email subject and body."""
        if self.demo:
            if campaign_id != 'demo':
                raise ValueError('Unknown campaign')
            topics = ('your free tax consultation', 'the documents we need from you', 'your options for resolving your tax debt')
            return [{'id': str(n), 'number': n, 'subject': f'Following up on {t}', 'body': f'<p>Hi there, I wanted to follow up on {t}.</p>'} for n, t in enumerate(topics, 1)]
        row = self.mix.get('sequences/' + quote(str(campaign_id), safe=''))
        if not isinstance(row, dict) or not isinstance(row.get('stages'), list):
            raise APIError('Mixmax campaign: expected an ordered stages list')
        stages = []
        for number, stage in enumerate(row['stages'], 1):
            stage = stage if isinstance(stage, dict) else {'_id': stage}
            stages.append({'id': str(stage.get('_id') or stage.get('id') or ''), 'number': number, 'subject': str(stage.get('subject') or ''), 'body': str(stage.get('body') or '')})
        if not all(s['id'] for s in stages):
            raise APIError('Mixmax campaign: stage is missing its ID')
        return stages

    def save_campaign_rules(self, data):
        """Save one rule per stage of a campaign in a single transaction."""
        campaign = next((x for x in self.campaigns() if x['id'] == data.get('campaign')), None)
        if not campaign:
            raise ValueError('This campaign is no longer available. Refresh campaigns and choose again.')
        numbers = {s['id']: s['number'] for s in campaign['stages']}
        rules = data.get('rules')
        if not isinstance(rules, list) or not rules:
            raise ValueError('No stages to save')
        for rule in rules:
            if rule.get('stage') not in numbers:
                raise ValueError('A stage is no longer available. Reload the campaign and try again.')
            if numbers[rule['stage']] != int(rule.get('stage_number', 0)):
                raise ValueError('The stages have been reordered. Reload the campaign and review your messages.')
        with self.lock, self.db:
            for rule in rules:
                self.save_rule(dict(rule, campaign=campaign['id'], _stage_number=numbers[rule['stage']]), commit=False)

    def ingest(self, message, recipient, contacts):
        destination = recipient.get('to')
        destination = destination if isinstance(destination, dict) else {}
        email = str(destination.get('email') or recipient.get('email') or '').strip().casefold()
        ident = str(message.get('_id') or message.get('id') or '')
        seq = message.get('sequence', {})
        if not email or '@' not in email or not ident or not message.get('sent') or not seq.get('id') or not seq.get('stageId'):
            if message.get('sent') and (not email or '@' not in email):
                self.log('Sent message skipped: recipient address missing or invalid (expected to.email or email)')
            return
        if stamp(message['sent']) < float(self.meta('baseline')):
            return
        if self.db.execute('SELECT 1 FROM events WHERE id=? AND email=?', (ident, email)).fetchone():
            return
        options, error = contact_numbers(email, contacts)
        name = destination.get('name') or recipient.get('name') or (recipient.get('variables') or {}).get('firstName') or email.split('@')[0]
        self.db.execute('INSERT OR IGNORE INTO leads(email,name,phone) VALUES (?,?,?)', (email, name, ''))
        number = self.update_lead_numbers(email, options)
        rule = self.db.execute('SELECT * FROM rules WHERE campaign=? AND stage=?', (seq['id'], seq['stageId'])).fetchone()
        reason = error or ('Stage not enabled' if not rule or not rule['enabled'] else '')
        lead = self.db.execute('SELECT * FROM leads WHERE email=?', (email,)).fetchone()
        reason = next((f for f in FLAGS if lead[f]), reason)
        stop = self.db.execute('SELECT reason FROM phone_stops WHERE phone=?', (number,)).fetchone()
        reason = reason or (stop[0] if stop else '')
        text = rule['template'].format(name=name, email=email) if rule else ''
        due = stamp(message['sent']) + (rule['delay'] * 60 if rule else 0)
        review = int(bool(rule and rule['mode'] == 'review'))
        status = 'skipped' if reason else 'needs_number' if not number else 'draft' if review else 'pending'
        reason = reason or {'needs_number': 'Several numbers on contact; choose one under Recipients', 'draft': 'Waiting for review'}.get(status, '')
        self.db.execute('INSERT OR IGNORE INTO jobs(event_id,email,campaign,stage,recipient_id,phone,text,due,status,reason,created,review) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (ident, email, seq['id'], seq['stageId'], seq.get('recipientId', ''), number, text, due, status, reason, time.time(), review))
        self.db.execute('INSERT INTO events VALUES (?,?)', (ident, email))
        self.db.commit()

    def update_lead_numbers(self, email, options):
        """Store the numbers found on the contact; returns the number texts should go to ('' if a choice is needed)."""
        lead = self.db.execute('SELECT chosen_phone FROM leads WHERE email=?', (email,)).fetchone()
        number = pick_number(lead['chosen_phone'] if lead else '', options)
        self.db.execute('UPDATE leads SET phone_options=?,phone=? WHERE email=?', (json.dumps(options), number, email))
        return number

    def choose_phone(self, email, number):
        """Pick which of the lead's numbers gets texts; waiting texts move to it and need fresh live approval."""
        with self.lock:
            lead = self.db.execute('SELECT * FROM leads WHERE email=?', (email,)).fetchone()
            options = json.loads(lead['phone_options'] or '[]') if lead else []
            if number not in [o['number'] for o in options]:
                raise ValueError('That number is not on this lead’s RingCentral contact. Poll again and retry.')
            with self.db:
                self.db.execute('UPDATE leads SET chosen_phone=?,phone=? WHERE email=?', (number, number, email))
                self.db.execute("""UPDATE jobs SET phone=?,approved=0,
                    status=CASE WHEN status='needs_number' THEN (CASE WHEN review=1 THEN 'draft' ELSE 'pending' END) ELSE status END,
                    reason=CASE WHEN status IN ('needs_number','draft') AND review=1 THEN 'Waiting for review' ELSE '' END
                    WHERE email=? AND status IN ('pending','needs_number','draft') AND phone!=?""", (number, email, number))
            self.log(f'Text number chosen for a lead: {number}')

    def job_action(self, ident, action, text=None):
        """Cancel, send now, approve, review or edit one text that has not gone out yet."""
        with self.lock:
            job = self.db.execute('SELECT * FROM jobs WHERE id=?', (int(ident),)).fetchone()
            allowed = {'cancel': ('pending', 'needs_number', 'draft'), 'now': ('pending',), 'approve': ('pending',),
                       'edit': ('pending', 'needs_number', 'draft'), 'approve_draft': ('draft',)}
            if action not in allowed:
                raise ValueError('Unknown action')
            if not job or job['status'] not in allowed[action]:
                raise ValueError('This text has already been sent or stopped; reload to see its status')
            if text is not None or action == 'edit':
                text = str(text or '').strip()
                if not 0 < len(text) <= 1000:
                    raise ValueError('The text cannot be empty or longer than 1000 characters')
            with self.db:
                if text is not None and text != job['text']:
                    # An approval covered the old wording, so a live send needs approving again.
                    self.db.execute('UPDATE jobs SET text=?,approved=CASE WHEN status=\'pending\' THEN 0 ELSE approved END WHERE id=?', (text, job['id']))
                    self.log(f'Text edited before sending (text #{job["id"]})')
                if action == 'cancel':
                    self.db.execute("UPDATE jobs SET status='cancelled',reason='Manually cancelled' WHERE id=?", (job['id'],))
                elif action == 'now':
                    self.db.execute('UPDATE jobs SET due=? WHERE id=?', (time.time(), job['id']))
                elif action == 'approve':
                    self.db.execute('UPDATE jobs SET approved=1 WHERE id=?', (job['id'],))
                elif action == 'approve_draft':
                    # Reviewing a draft is its approval, including for live sending.
                    self.db.execute("UPDATE jobs SET status='pending',approved=1,reason='Reviewed and approved' WHERE id=?", (job['id'],))

    def bulk(self, action, ids):
        """Apply one action to many texts; texts that changed meanwhile are skipped, not failed."""
        if action not in ('approve_draft', 'cancel'):
            raise ValueError('Unknown bulk action')
        done = 0
        for ident in ids[:500]:
            try:
                self.job_action(ident, action)
                done += 1
            except ValueError:
                pass
        return {'done': done, 'skipped': len(ids[:500]) - done}

    def personalize_context(self, ident):
        """What the AI needs to tailor one text: the stage email, the subject actually sent, and the recipient's Mixmax details."""
        with self.lock:
            job = self.db.execute('SELECT * FROM jobs WHERE id=?', (int(ident),)).fetchone()
            lead = self.db.execute('SELECT name FROM leads WHERE email=?', (job['email'],)).fetchone() if job else None
        if not job:
            raise ValueError('Text not found')
        stage = next((s for s in self.campaign_stages(job['campaign']) if s['id'] == job['stage']), {})
        subject, variables = stage.get('subject', ''), {}
        if not self.demo:
            try:
                subject = self.mix.get('messages/' + quote(job['event_id'], safe='')).get('subject') or subject
                match = [r for r in self.mix.recipients(job['campaign']) if str(r.get('_id') or r.get('id')) == job['recipient_id']]
                variables = (match[0].get('variables') or {}) if match else {}
            except APIError:
                pass  # Fall back to the stage template; the reviewer still sees and edits the result.
        return {'name': lead['name'] if lead else '', 'stage_number': stage.get('number'), 'subject': subject,
                'body': stage.get('body', ''), 'variables': {k: v for k, v in variables.items() if isinstance(v, (str, int, float))}, 'text': job['text']}

    def flag(self, email, flag, value=True):
        if flag not in FLAGS:
            raise ValueError('Unknown lead flag')
        if not self.db.execute('SELECT 1 FROM leads WHERE email=?', (email,)).fetchone():
            member = self.db.execute('SELECT name FROM roster WHERE email=?', (email,)).fetchone()
            if not member:
                raise ValueError('Recipient not found')
            # In a sequence but not texted yet: keep the flag so future texts are skipped.
            self.db.execute('INSERT INTO leads(email,name,phone) VALUES (?,?,?)', (email, member['name'], ''))
        self.db.execute(f'UPDATE leads SET {flag}=? WHERE email=?', (int(value), email))
        if value:
            self.db.execute("UPDATE jobs SET status='cancelled',reason=? WHERE email=? AND status IN ('pending','needs_number','draft')", (flag, email))
        self.db.commit()
        self.log(f'Lead flag changed: {flag}')

    def clear_phone_stop(self, number, note):
        number = phone(number)
        if not number or not str(note).strip():
            raise ValueError('Provide a valid phone number and a review note')
        with self.lock:
            self.db.execute('DELETE FROM phone_stops WHERE phone=?', (number,))
            self.log(f'Phone suppression cleared for {number}: {str(note).strip()}')
        # Lead flags and cancelled jobs deliberately require separate review.

    def sync_replies(self):
        with self.lock:
            token, since = self.meta('rc_sync'), iso(float(self.meta('baseline')))
        result = self.rc.sync(token, since)
        with self.lock:
            for record in result.get('records', []):
                ident = str(record.get('id', ''))
                if not ident or self.db.execute('SELECT 1 FROM inbound WHERE id=?', (ident,)).fetchone():
                    continue
                if record.get('direction') == 'Inbound' and record.get('type') == 'SMS':
                    source = phone(record.get('from', {}).get('phoneNumber', ''))
                    if source:
                        opted_out = str(record.get('subject', '')).strip().upper() in ('STOP', 'STOPALL', 'UNSUBSCRIBE', 'CANCEL', 'END', 'QUIT')
                        reason = 'SMS opt-out received' if opted_out else 'Inbound SMS received; follow-up stopped'
                        self.db.execute('INSERT OR REPLACE INTO phone_stops VALUES (?,?)', (source, reason))
                        for lead in self.db.execute('SELECT email FROM leads WHERE phone=? UNION SELECT email FROM jobs WHERE phone=?', (source, source)).fetchall():
                            self.flag(lead['email'], 'replied')
                            if opted_out:
                                self.flag(lead['email'], 'opted_out')
                self.db.execute('INSERT INTO inbound VALUES (?)', (ident,))
            self.setmeta('rc_sync', result['syncInfo']['syncToken'])

    def poll(self):
        with self.lock:
            if self.demo:
                self.health = 'Demo ready'; self.cycle_ok = True
                return
            self.cycle_ok = False
            campaigns = {r['campaign'] for r in self.rules()}
        contacts = self.rc.contacts()
        recipients = {campaign: self.mix.recipients(campaign) for campaign in campaigns}
        messages = self.mix.messages()
        with self.lock:
            for message in messages:
                seq = message.get('sequence') or {}
                if seq.get('id') not in recipients:
                    continue
                matches = [r for r in recipients[seq['id']] if str(r.get('_id') or r.get('id')) == str(seq.get('recipientId'))]
                if len(matches) == 1:
                    self.ingest(message, matches[0], contacts)
                elif message.get('sent'):
                    self.log('Message skipped: recipient ID could not be matched uniquely')
        for campaign, rows in recipients.items():
            self.sync_roster(campaign, rows)
        self.sync_other_rosters(campaigns)
        self.sync_replies()
        with self.lock:
            self.last_poll = time.time()
            self.cycle_ok = True
            self.health = 'API poll successful'

    def sync_roster(self, campaign, rows):
        """Store Mixmax's view of each recipient (status, stages, next email) and stop texts for new email replies."""
        now = time.time()
        with self.lock:
            previous = {r['recipient_id']: r['replied'] for r in self.db.execute('SELECT recipient_id, replied FROM roster WHERE campaign=?', (campaign,))}
            with self.db:
                self.db.execute('DELETE FROM roster WHERE campaign=?', (campaign,))
                for r in rows:
                    ident = str(r.get('_id') or r.get('id') or '')
                    to = r.get('to') if isinstance(r.get('to'), dict) else {}
                    email = str(to.get('email') or r.get('email') or '').strip().casefold()
                    if not ident or '@' not in email:
                        continue
                    stages = sorted((x for x in r.get('stages') or [] if isinstance(x, dict)), key=lambda x: x.get('ordinal') or 0)
                    upcoming = next((x for x in stages if x.get('state') == 'scheduled' and x.get('scheduledAt')), None) if r.get('state') == 'active' else None
                    replied = int(any(x.get('replied') for x in stages) or r.get('completedReason') == 'replied')
                    name = to.get('name') or (r.get('variables') or {}).get('firstName') or email.split('@')[0]
                    self.db.execute('INSERT OR REPLACE INTO roster VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                        campaign, ident, email, name, str(r.get('state') or ''), str(r.get('completedReason') or ''),
                        r.get('lastStageSentOrdinal') or 0, upcoming.get('ordinal') if upcoming else None,
                        str(upcoming.get('stageId') or '') if upcoming else '', stamp(upcoming['scheduledAt']) if upcoming else None,
                        replied, stamp(r['createdAt']) if r.get('createdAt') else now))
                    if replied and not previous.get(ident):
                        # Only on a new reply, so a manual "un-replied" is not overridden every poll.
                        self.db.execute('INSERT OR IGNORE INTO leads(email,name,phone) VALUES (?,?,?)', (email, name, ''))
                        self.flag(email, 'replied')
                        self.log(f'Email reply detected in Mixmax; texts stopped for {email}')

    def sync_other_rosters(self, done):
        """Sequences without texts only feed the Recipients view, so refresh them every 15 minutes at most."""
        if time.time() - float(self.meta('roster_full_at') or 0) < 900:
            return
        try:
            for campaign in self.campaigns():
                if campaign['id'] not in done:
                    self.sync_roster(campaign['id'], self.mix.recipients(campaign['id']))
            with self.lock:
                self.setmeta('roster_full_at', str(time.time()))
        except APIError as e:
            with self.lock:
                self.log(f'Recipient list refresh skipped: {e}')

    def demo_roster(self, email, stage):
        """Demo stand-in for Mixmax's recipient list: the next stage is two days after this one."""
        stage = int(stage)
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO roster VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
                'demo', email, email, 'John', 'active' if stage < 3 else 'completed', '' if stage < 3 else 'exhausted', stage,
                stage + 1 if stage < 3 else None, str(stage + 1) if stage < 3 else '', time.time() + 2 * 86400 if stage < 3 else None, 0, time.time()))

    def dispatch(self):
        with self.lock:
            if not self.cycle_ok:
                return
            cutoff = time.time() - float(self.config.get('MAX_LATENESS_MINUTES', '1440')) * 60
            self.db.execute("UPDATE jobs SET status='expired',reason='Not reviewed in time; it would have arrived too late' WHERE status='draft' AND due<?", (cutoff,))
            self.db.execute("UPDATE jobs SET status='expired',reason='No number chosen in time; it would have arrived too late' WHERE status='needs_number' AND due<?", (cutoff,))
            self.db.commit()
            jobs = self.db.execute("SELECT * FROM jobs WHERE status='pending' AND due<=? ORDER BY due", (time.time(),)).fetchall()
        # Cache only within this dispatch batch; inbound replies are checked per job.
        contacts = None
        recipients = {}
        sender_validated = False
        cache_at = 0
        for snapshot in jobs:
            with self.lock:
                job = self.db.execute('SELECT * FROM jobs WHERE id=?', (snapshot['id'],)).fetchone()
                if job['status'] != 'pending':
                    continue
                reason = self.stop_reason(job)
                if reason:
                    self.finish_job(job['id'], 'cancelled', reason)
                    continue
                if time.time() - job['due'] > float(self.config.get('MAX_LATENESS_MINUTES', '1440')) * 60:
                    self.finish_job(job['id'], 'held', 'Overdue; review before rescheduling')
                    continue
                if not self.test:
                    allowed = {phone(x) for x in self.config.get('LIVE_ALLOWED_NUMBERS', '').split(',')}
                    if self.demo or job['phone'] not in allowed or not job['approved']:
                        continue
            error = ''
            if not self.demo:
                if time.monotonic() - cache_at > 5:
                    contacts = None
                    recipients.clear()
                    cache_at = time.monotonic()
                if contacts is None:
                    contacts = self.rc.contacts()
                options, error = contact_numbers(job['email'], contacts)
                with self.lock:
                    number = self.update_lead_numbers(job['email'], options)
                    self.db.commit()
                if job['campaign'] not in recipients:
                    recipients[job['campaign']] = self.mix.recipients(job['campaign'])
                recipient = [r for r in recipients[job['campaign']] if str(r.get('id') or r.get('_id')) == job['recipient_id']]
                error = error or ('Contact number changed' if number != job['phone'] else '')
                error = error or ('Sequence recipient not active' if len(recipient) != 1 or recipient[0].get('state') != 'active' else '')
                if not self.test and not sender_validated:
                    self.rc.validate_sender()
                    sender_validated = True
                self.sync_replies()
            with self.lock:
                # Manual cancellation, flags, and rule edits may occur during API calls.
                job = self.db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
                if job['status'] != 'pending':
                    continue
                reason = error or self.stop_reason(job)
                if reason:
                    self.finish_job(job['id'], 'cancelled', reason)
                    continue
                if job['due'] > time.time():
                    continue
                self.finish_job(job['id'], 'sending', '')
            try:
                result = {} if self.test else self.rc.send(job['phone'], job['text'])
                if not self.test and (not isinstance(result, dict) or not result.get('id')):
                    raise ValueError('Send response missing message ID')
                with self.lock:
                    self.db.execute('UPDATE jobs SET status=?,provider_id=?,reason=? WHERE id=?', ('simulated' if self.test else 'accepted', str(result.get('id', '')), 'Test mode: no SMS sent' if self.test else 'Accepted by RingCentral; verify delivery', job['id']))
                    self.db.commit()
            except Exception:
                with self.lock:
                    self.finish_job(job['id'], 'unknown', 'Send outcome uncertain; inspect RingCentral before retrying')

    def stop_reason(self, job):
        lead = self.db.execute('SELECT * FROM leads WHERE email=?', (job['email'],)).fetchone()
        rule = self.db.execute('SELECT * FROM rules WHERE campaign=? AND stage=?', (job['campaign'], job['stage'])).fetchone()
        stop = self.db.execute('SELECT reason FROM phone_stops WHERE phone=?', (job['phone'],)).fetchone()
        return next((f for f in FLAGS if lead[f]), '') or (stop[0] if stop else '') or ('Stage disabled' if not rule or not rule['enabled'] else '')

    def finish_job(self, ident, status, reason):
        self.db.execute('UPDATE jobs SET status=?,reason=? WHERE id=?', (status, reason, ident))
        self.db.commit()

    def cycle(self, force=False):
        # Repeated Poll clicks do not queue overlapping provider operations.
        if not self.cycle_lock.acquire(blocking=False):
            return
        try:
            if force or time.time() >= self.next_poll:
                self.poll()
                self.next_poll = time.time() + max(60, float(self.config.get('POLL_SECONDS', '120')))
            self.dispatch()
        except Exception as e:
            with self.lock:
                self.cycle_ok = False
                self.health = f'Paused: {str(e) if type(e).__name__ == "APIError" else type(e).__name__}'
                self.next_poll = time.time() + max(60, float(self.config.get('POLL_SECONDS', '120')))
                self.log(self.health)
        finally:
            self.cycle_lock.release()

    def recipients(self, query='', status='', page=1, campaign='', stage='', per_page=50):
        """One page of sequence memberships (person x sequence) for the Recipients view, with counts for the filters."""
        with self.lock:
            members = {(r['campaign'], r['email']): dict(r) for r in self.db.execute('SELECT * FROM roster')}
            for r in self.db.execute('SELECT DISTINCT campaign, email FROM jobs'):
                members.setdefault((r['campaign'], r['email']), {'campaign': r['campaign'], 'email': r['email'], 'name': '', 'state': '', 'reason': '',
                                                                'last_stage': None, 'next_stage': None, 'next_stage_id': '', 'next_at': None, 'replied': 0, 'created': 0})
            leads = {r['email']: dict(r) for r in self.db.execute('SELECT * FROM leads')}
            stopped = {r[0] for r in self.db.execute('SELECT phone FROM phone_stops')}
            rules = {(r['campaign'], r['stage']): dict(r) for r in self.db.execute('SELECT * FROM rules')}
            stats = {}
            for r in self.db.execute('SELECT campaign, email, status, COUNT(*) n, MIN(due) due, MAX(created) created, GROUP_CONCAT(id) ids FROM jobs GROUP BY campaign, email, status'):
                stats.setdefault((r['campaign'], r['email']), {})[r['status']] = dict(r)
        rows = []
        for key, m in members.items():
            if campaign and m['campaign'] != campaign:
                continue
            lead, js = leads.get(m['email'], {}), stats.get(key, {})
            ids = lambda *names: [int(i) for n in names for i in (js.get(n, {}).get('ids') or '').split(',') if i]
            if any(lead.get(f) for f in FLAGS) or (lead.get('phone') and lead['phone'] in stopped):
                text_status = 'stopped'
            else:
                text_status = next((s for s, n in (('needs_number', 'needs_number'), ('to_review', 'draft'), ('scheduled', 'pending')) if js.get(n)), None) \
                    or ('texted' if js.get('simulated') or js.get('accepted') else 'no_text')
            rule = rules.get((m['campaign'], m['next_stage_id'] or ''))
            if js.get('pending') or js.get('draft'):
                soonest = min((js[n] for n in ('pending', 'draft') if js.get(n)), key=lambda x: x['due'])
                next_text = {'at': soonest['due'], 'kind': 'draft' if soonest['status'] == 'draft' else 'scheduled'}
            elif m['next_at'] and rule and rule['enabled'] and text_status != 'stopped':
                next_text = {'at': m['next_at'] + rule['delay'] * 60, 'kind': 'review' if rule['mode'] == 'review' else 'projected', 'stage': m['next_stage'],
                             'no_number': not lead.get('phone') and not json.loads(lead.get('phone_options') or '[]')}
            else:
                next_text = None
            rows.append(dict(m, name=m['name'] or lead.get('name') or m['email'].split('@')[0], phone=lead.get('phone', ''), status=text_status,
                             next_text=next_text, replied=m['replied'] or lead.get('replied', 0), draft_ids=ids('draft'), open_ids=ids('pending', 'draft', 'needs_number'),
                             last_activity=max([m['created'] or 0] + [x['created'] or 0 for x in js.values()])))
        counts = {k: 0 for k in ('needs_number', 'to_review', 'scheduled', 'texted', 'no_text', 'stopped')}
        stage_counts = {}
        for row in rows:
            counts[row['status']] += 1
            stage_counts[row['last_stage'] or 0] = stage_counts.get(row['last_stage'] or 0, 0) + 1
        counts['all'] = len(rows)
        query = str(query).strip().casefold()
        if query:
            rows = [r for r in rows if query in ' '.join(str(r[k] or '') for k in ('name', 'email', 'phone')).casefold()]
        if status:
            rows = [r for r in rows if r['status'] == status]
        if str(stage).isdigit():
            rows = [r for r in rows if (r['last_stage'] or 0) == int(stage)]
        rows.sort(key=lambda r: (r['status'] not in ('needs_number', 'to_review'), -(r['last_activity'] or 0)))
        page = max(1, int(page) if str(page).isdigit() else 1)
        return {'counts': counts, 'stages': stage_counts, 'total': len(rows), 'page': page, 'per_page': per_page, 'rows': rows[(page - 1) * per_page:page * per_page]}

    def recipient(self, email):
        """Everything about one lead: numbers, flags and full text history."""
        with self.lock:
            lead = self.db.execute('SELECT * FROM leads WHERE email=?', (email,)).fetchone()
            if not lead:
                # In a Mixmax sequence but no email has been followed up yet.
                member = self.db.execute('SELECT name FROM roster WHERE email=?', (email,)).fetchone()
                if not member:
                    raise ValueError('Recipient not found')
                lead = {'email': email, 'name': member['name'], 'phone': '', 'phone_options': '[]', 'chosen_phone': '', **{f: 0 for f in FLAGS}}
            jobs = [dict(r) for r in self.db.execute('SELECT * FROM jobs WHERE email=? ORDER BY created DESC', (email,))]
            stop = self.db.execute('SELECT reason FROM phone_stops WHERE phone=? AND phone!=\'\'', (lead['phone'],)).fetchone()
            sequences = [dict(r) for r in self.db.execute('SELECT * FROM roster WHERE email=? ORDER BY created DESC', (email,))]
        return {'lead': dict(lead), 'jobs': jobs, 'phone_stop': stop[0] if stop else '', 'sequences': sequences}

    JOB_FILTERS = {'draft': ('draft',), 'needs_number': ('needs_number',), 'pending': ('pending',), 'sent': ('simulated', 'accepted'),
                   'review': ('held', 'unknown'), 'skipped': ('skipped',), 'cancelled': ('cancelled', 'expired')}

    def jobs_page(self, query='', status='', page=1, campaign='', per_page=50):
        """Texts grouped one row per person per sequence; a group is listed when any of its texts matches the filters."""
        where, args = [], []
        if status in self.JOB_FILTERS:
            where.append('status IN (%s)' % ','.join('?' * len(self.JOB_FILTERS[status])))
            args += self.JOB_FILTERS[status]
        if campaign:
            where.append('campaign=?')
            args.append(campaign)
        query = str(query).strip()
        if query:
            where.append('(email LIKE ? OR phone LIKE ? OR text LIKE ?)')
            args += ['%' + query + '%'] * 3
        sql = ' WHERE ' + ' AND '.join(where) if where else ''
        page = max(1, int(page) if str(page).isdigit() else 1)
        with self.lock:
            by_status = dict(self.db.execute('SELECT status, COUNT(*) FROM jobs' + (' WHERE campaign=?' if campaign else '') + ' GROUP BY status', [campaign] if campaign else []).fetchall())
            total, texts = self.db.execute('SELECT COUNT(*), COALESCE(SUM(n), 0) FROM (SELECT COUNT(*) n FROM jobs' + sql + ' GROUP BY email, campaign)', args).fetchone()
            groups = self.db.execute('''SELECT email, campaign, MAX(created) last,
                    MAX(CASE WHEN status IN ('draft','needs_number','held','unknown') THEN 1 ELSE 0 END) attention
                FROM jobs''' + sql + ''' GROUP BY email, campaign ORDER BY attention DESC, last DESC LIMIT ? OFFSET ?''',
                args + [per_page, (page - 1) * per_page]).fetchall()
            rows = []
            for g in groups:
                # The whole history for this person in this sequence, oldest stage first.
                jobs = [dict(r) for r in self.db.execute('SELECT * FROM jobs WHERE email=? AND campaign=? ORDER BY due, id', (g['email'], g['campaign']))]
                rows.append({'email': g['email'], 'campaign': g['campaign'], 'attention': g['attention'], 'jobs': jobs,
                             'phone': next((j['phone'] for j in reversed(jobs) if j['phone']), '')})
        counts = {k: sum(by_status.get(x, 0) for x in v) for k, v in self.JOB_FILTERS.items()}
        counts['all'] = sum(by_status.values())
        return {'counts': counts, 'total': total, 'texts': texts, 'page': page, 'per_page': per_page, 'rows': rows}

    def state(self):
        """Small summary polled every few seconds; lists are fetched page by page."""
        with self.lock:
            by_status = dict(self.db.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status').fetchall())
            members = self.db.execute('SELECT COUNT(*) FROM (SELECT campaign, email FROM roster UNION SELECT campaign, email FROM jobs)').fetchone()[0]
            return {'demo': self.demo, 'test': self.test, 'health': self.health, 'last_poll': self.last_poll, 'rules': self.rules(),
                    'job_counts': by_status, 'lead_count': members,
                    'phone_stops': [dict(r) for r in self.db.execute('SELECT * FROM phone_stops ORDER BY phone')],
                    'logs': [dict(r) for r in self.db.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 50')]}
