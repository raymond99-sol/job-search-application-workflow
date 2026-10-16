"""Bounded private history. Disk work runs only on the background writer."""
import json, os, tempfile, threading
from pathlib import Path
from .activity_details import coalesce_model_records
ROOT = Path(os.environ.get('CAREER_DESK_DATA', 'data')).resolve()
LIMIT = 100
TERMINAL = {'completed', 'interrupted', 'cancelled', 'failed', 'unknown'}
_lock = threading.Lock()


def normalize(rows):
    """One explicit invocation ID is one operation; equal labels are not IDs."""
    merged = {}
    for event in rows:
        if not isinstance(event, dict) or not event.get('id') or event.get('status') not in TERMINAL:
            continue
        row = dict(event)
        identity = row.get('model_call_id') or row['id']
        aliases = row.get('aliases', [])
        aliases = [x for x in aliases if isinstance(x, str)] if isinstance(aliases, list) else []
        if row['id'] != identity: aliases.append(row['id'])
        previous = merged.get(identity, {})
        row['aliases'] = list(dict.fromkeys([*previous.get('aliases', []), *aliases]))[:8]
        row.update(id=identity, finished=True)
        if (previous.get('heartbeat') or 0) > (row.get('heartbeat') or 0):
            merged[identity] = {**row, **previous, 'aliases': row['aliases']}
        else:
            merged[identity] = {**previous, **row}
    rows = coalesce_model_records(list(merged.values()))
    mirrors = {alias for row in rows for alias in row.get('aliases', []) if alias != row['id']}
    rows = [row for row in rows if row['id'] not in mirrors]
    rows.sort(key=lambda row: row.get('heartbeat') or 0, reverse=True)
    return rows[:LIMIT]


def save(event):
    # A genuine model request has a visible outcome, including validation calls.
    if not event.get('finished') or event.get('status') not in TERMINAL:
        return True
    with _lock:
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        rows = read()
        # Never persist auth headers, full inputs or complete model answers.
        fields = ('id', 'model_call_id', 'model', 'label', 'tool', 'chat', 'role', 'kind',
                  'thread_id', 'started', 'heartbeat', 'status', 'finished', 'aliases',
                  'error_type', 'error_message', 'http_status', 'monitor_error', 'reason_code')
        item = {key: event[key] for key in fields if key in event}
        rows = normalize([*rows, item])
        descriptor, temporary = tempfile.mkstemp(dir=ROOT, prefix='.recent-', suffix='.tmp')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(rows, handle, ensure_ascii=False)
            os.replace(temporary, ROOT / 'recent.json')
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    return True


def read():
    rows = []
    # Migrate the old 25-success archive and its separate last interruption in memory.
    for name in ('recent.json', 'last_interruption.json'):
        try:
            data = json.loads((ROOT / name).read_text(encoding='utf-8'))
            if isinstance(data, list): rows.extend(data)
        except (OSError, ValueError):
            pass
    return normalize(rows)
