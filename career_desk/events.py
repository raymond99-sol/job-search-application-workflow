"""Private, bounded completion archive. Called only by the background writer."""
import json, os, tempfile, threading
from pathlib import Path
ROOT = Path(os.environ.get('CAREER_DESK_DATA', 'data')).resolve()
_lock = threading.Lock()

def save(event):
    if not event.get('finished') or event.get('purpose') == 'monitor_verification':
        return True
    with _lock:
        ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = ROOT / 'recent.json'
        if event.get('status') != 'completed':
            path = ROOT / 'last_interruption.json'
            rows = []
        else:
            rows = read()
        # Retain summaries only; never store request headers or complete answers.
        item = {k: event.get(k) for k in ('id', 'model', 'label', 'tool', 'chat', 'status', 'started', 'heartbeat')}
        rows = [item] + [x for x in rows if x.get('id') != item['id']]
        rows.sort(key=lambda x: x.get('heartbeat') or 0, reverse=True)
        descriptor, temporary = tempfile.mkstemp(dir=ROOT, prefix='.recent-', suffix='.tmp')
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                json.dump(rows[:25], handle, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    return True

def read():
    try:
        rows = json.loads((ROOT / 'recent.json').read_text(encoding='utf-8'))
        return rows[:25] if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []
