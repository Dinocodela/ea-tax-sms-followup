"""Draft SMS follow-ups from Mixmax stage emails. Drafts are suggestions only; nothing is saved or sent here."""
import html
import json
import re
from providers import APIError, request

# Override with OPENAI_MODEL in .env.
DEFAULT_MODEL = 'gpt-5-mini'
MAX_SMS = 320
SENDER = 'Lukas from EA Tax Resolutions'
OPT_OUT = 'Reply STOP to opt out.'


class DraftError(Exception):
    pass


def email_text(body):
    """Mixmax stage bodies are HTML; reduce them to readable plain text."""
    text = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', body or '')
    text = re.sub(r'(?i)<br\s*/?>|</(p|div|li|h\d)>', '\n', text)
    text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    lines = (' '.join(line.split()) for line in text.splitlines())
    return '\n'.join(line for line in lines if line)


def valid_template(text):
    try:
        text.format(name='Test', email='test@example.com')
    except (KeyError, IndexError, ValueError):
        return False
    return 0 < len(text) <= MAX_SMS


def with_opt_out(text):
    text = text.strip()
    return text if 'reply stop' in text.casefold() else f'{text} {OPT_OUT}'


def basic_draft(stage):
    subject = ' '.join((stage.get('subject') or '').split())[:80]
    about = f' about "{subject}"' if subject else ''
    return f'Hi {{name}}, {SENDER} here. I just sent you an email{about}. Reply here with any questions. {OPT_OUT}'


def chat_json(config, prompt, schema):
    """Ask ChatGPT for a JSON reply matching schema."""
    if not config.get('OPENAI_API_KEY'):
        raise DraftError('Add OPENAI_API_KEY to .env and restart the app to use AI drafts.')
    try:
        response = request('https://api.openai.com/v1/chat/completions', {'Authorization': 'Bearer ' + config['OPENAI_API_KEY']}, {
            'model': config.get('OPENAI_MODEL') or DEFAULT_MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'reply', 'strict': True, 'schema': schema}},
        }, timeout=120)
    except APIError as e:
        raise DraftError('ChatGPT request failed: ' + str(e)) from None
    try:
        message = response['choices'][0]['message']
        if message.get('refusal'):
            raise DraftError('ChatGPT declined this request. Write the text manually.')
        return json.loads(message['content'])
    except (ValueError, KeyError, TypeError, IndexError):
        raise DraftError('ChatGPT returned an unexpected reply. Try again.') from None


RULES = (f'- is friendly, plain and under {MAX_SMS} characters, with no links, emojis or pressure tactics\n'
         f'- ends with exactly "{OPT_OUT}"\n')


def ai_drafts(config, stages):
    """One ChatGPT call for the whole campaign so the messages read as a sequence."""
    emails = '\n\n'.join(f'<stage id="{s["id"]}" number="{s["number"]}">\nSubject: {s.get("subject", "")}\n{email_text(s.get("body", ""))[:6000]}\n</stage>' for s in stages)
    prompt = (
        f'You write SMS follow-ups for {SENDER}, a tax resolution firm. Each SMS is sent after the matching email in a '
        'Mixmax sequence has gone out, to nudge the lead to read it and reply.\n\n'
        'For every stage below, write one SMS that:\n'
        f'- starts with "Hi {{name}}, {SENDER} here." ({{name}} is filled in with the lead\'s first name)\n'
        '- refers to the specific point of that stage\'s email, so each stage sounds different\n' + RULES +
        '- uses no placeholders other than {name} and {email}\n\n' + emails)
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['drafts'], 'properties': {'drafts': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['stage_id', 'text'],
        'properties': {'stage_id': {'type': 'string'}, 'text': {'type': 'string'}}}}}}
    try:
        drafted = {d['stage_id']: with_opt_out(d['text']) for d in chat_json(config, prompt, schema)['drafts']}
    except (KeyError, TypeError):
        raise DraftError('ChatGPT returned an unexpected reply. Try again.') from None
    return {s['id']: drafted[s['id']] for s in stages if valid_template(drafted.get(s['id'], ''))}


def personalize(config, ctx):
    """Rewrite one recipient's text to match the email they received. Returns final text (no placeholders)."""
    details = '\n'.join(f'{k}: {v}' for k, v in ctx['variables'].items()) or '(none)'
    prompt = (
        f'You write SMS follow-ups for {SENDER}, a tax resolution firm. Rewrite the draft SMS below for one specific '
        'recipient so it matches the email they just received and feels written for them.\n\n'
        f'Recipient name: {ctx["name"]}\nTheir Mixmax details:\n{details}\n\n'
        f'Email they received (stage {ctx["stage_number"]}):\nSubject: {ctx["subject"]}\n{email_text(ctx["body"])[:6000]}\n\n'
        f'Current draft SMS:\n{ctx["text"]}\n\n'
        'Write one SMS that:\n'
        f'- starts with "Hi <their first name>, {SENDER} here." using their real first name, not a placeholder\n'
        '- keeps the purpose of the current draft and refers to the email\'s specific point\n' + RULES)
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['text'], 'properties': {'text': {'type': 'string'}}}
    try:
        text = with_opt_out(str(chat_json(config, prompt, schema)['text']).replace('{name}', ctx['name']))
    except (KeyError, TypeError):
        raise DraftError('ChatGPT returned an unexpected reply. Try again.') from None
    if not 0 < len(text) <= MAX_SMS:
        raise DraftError(f'ChatGPT wrote a text over {MAX_SMS} characters. Try again or edit it manually.')
    return text


def draft_campaign(config, stages):
    """Returns ({stage_id: text}, note). Falls back to subject-based drafts for any stage the AI could not cover."""
    note = ''
    try:
        drafts = ai_drafts(config, stages)
        missing = [s for s in stages if s['id'] not in drafts]
        if missing:
            note = 'Some stages got a basic draft; review them.'
    except DraftError as e:
        drafts, note = {}, str(e) + ' Basic drafts were used instead.'
    return {s['id']: drafts.get(s['id']) or basic_draft(s) for s in stages}, note
