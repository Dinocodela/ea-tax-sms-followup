"""Read-only account discovery with independent checks and safe diagnostics."""
from app import configuration
from providers import Mixmax, RingCentral, APIError, safe_detail


def main():
    counts = {'PASS': 0, 'FAIL': 0, 'SKIP': 0}
    secrets = []

    def report(status, label, detail=''):
        counts[status] += 1
        print(f'[{status}] {label}', flush=True)
        if detail:
            print('  ' + safe_detail(detail, secrets), flush=True)

    def check(label, action):
        print(f'[CHECK] {label}', flush=True)
        try:
            result = action()
        except Exception as e:
            detail = str(e) if isinstance(e, (APIError, ValueError, OSError)) else f'Unexpected {type(e).__name__}; the response shape or local code needs investigation.'
            report('FAIL', label, detail)
            return False, None
        report('PASS', label)
        return True, result

    def summary():
        print(f"Summary: {counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped.")
        return 1 if counts['FAIL'] else 0

    ok, config = check('Local configuration', configuration)
    if not ok:
        report('SKIP', 'Provider checks', 'Local configuration could not be loaded.')
        return summary()
    secrets.extend(config.get(k) for k in ('MIXMAX_API_TOKEN', 'RC_CLIENT_ID', 'RC_CLIENT_SECRET', 'RC_JWT'))

    def sequences():
        rows = Mixmax(config).collection('sequences')
        for sequence in rows:
            if not isinstance(sequence, dict) or not isinstance(sequence.get('stages', []), list):
                raise APIError('Mixmax sequences: expected sequence objects with a stages list')
            print('Campaign:', sequence.get('id') or sequence.get('_id'), '|', sequence.get('name'))
            for index, stage in enumerate(sequence.get('stages', []), 1):
                print('  Stage', index, ':', (stage.get('id') or stage.get('_id')) if isinstance(stage, dict) else stage)
        print('Accessible sequences:', len(rows))

    check('Mixmax — list sequences and stages', sequences)
    ok, rc = check('RingCentral — configuration', lambda: RingCentral(config))
    if ok:
        ok, _ = check('RingCentral — authentication', rc.authenticate)
    if not ok:
        report('SKIP', 'RingCentral — sender validation', 'RingCentral configuration or authentication failed.')
        report('SKIP', 'RingCentral — personal contacts', 'RingCentral configuration or authentication failed.')
    else:
        check('RingCentral — sender validation', rc.validate_sender)
        ok, contacts = check('RingCentral — personal contacts', rc.contacts)
        if ok:
            print('Visible personal contacts:', len(contacts))
    return summary()


if __name__ == '__main__':
    raise SystemExit(main())
