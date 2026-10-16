"""Local record helpers for a human-supervised workflow; never submits applications."""
import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile

FIELDS = ['id', 'market', 'company', 'role', 'job_id', 'status', 'applied_at', 'location', 'url', 'note', 'created', 'updated']

def now(): return datetime.now(timezone.utc).isoformat()

def atomic_text(path, text):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name, suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle: handle.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def read_json(path): return json.loads(path.read_text(encoding='utf-8'))
def save_json(path, value): atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')

@contextmanager
def editing(data):
    lock = data / '.records-lock'
    try: lock.mkdir()
    except FileExistsError: raise ValueError('另一个记录操作正在写入，请等它结束；不会覆盖或自动重试。')
    try: yield
    finally: lock.rmdir()


def execute(args):
    data = Path(args.data).resolve()
    if args.command == 'init':
        # Refuse to reset any existing directory, including one containing personal records.
        data.mkdir(parents=True, exist_ok=False, mode=0o700)
        save_json(data / 'profile.json', {'facts': [], 'confirmed': {'CN': [], 'US': []}})
        save_json(data / 'pending.json', [])
        atomic_text(data / 'applications.csv', ','.join(FIELDS) + '\n')
        (data / 'inputs').mkdir()
        (data / 'drafts').mkdir()
        print('个人工作区已创建，申请记录和事实资料均为空。')
        return
    if not all((data / name).is_file() for name in ('profile.json', 'pending.json', 'applications.csv')):
        raise ValueError('数据目录不完整；请指定正确目录，或用 init 新建空白工作区。')
    with editing(data):
        if args.command == 'question':
            rows = read_json(data / 'pending.json')
            old = next((x for x in rows if x['id'] == args.id), {})
            if old.get('resolved'): raise ValueError('问题已确认，请使用新编号登记新的问题。')
            row = {**old, 'id': args.id, 'market': args.market, 'company': args.company,
                   'role': args.role, 'question': args.text, 'resolved': False,
                   'created': old.get('created', now()), 'updated': now()}
            save_json(data / 'pending.json', [x for x in rows if x['id'] != args.id] + [row])
        elif args.command == 'answer':
            rows = read_json(data / 'pending.json')
            question = next((x for x in rows if x['id'] == args.id), None)
            if not question: raise ValueError('未找到问题编号。')
            if question.get('resolved'): raise ValueError('问题已确认，不会再次写入；如答案改变，请登记新的问题。')
            profile = read_json(data / 'profile.json')
            fact = {'id': 'answer-' + args.id, 'text': args.text, 'question': question['question'],
                    'source': 'user_confirmed', 'updated': now()}
            profile['facts'] = [x for x in profile.get('facts', []) if x.get('id') != fact['id']] + [fact]
            confirmed = profile.setdefault('confirmed', {})
            market = question['market']
            confirmed[market] = [x for x in confirmed.get(market, []) if x.get('id') != fact['id']] + [fact]
            # Save the fact first. If the second write fails, the question remains visible.
            save_json(data / 'profile.json', profile)
            question.update(resolved=True, answer=args.text, updated=now())
            save_json(data / 'pending.json', rows)
        elif args.command == 'application':
            if args.status == 'submitted' and not args.note:
                raise ValueError('标为已提交前，必须在 --note 写明提交确认依据；准备完成不算提交。')
            with (data / 'applications.csv').open(encoding='utf-8-sig', newline='') as handle:
                reader = csv.DictReader(handle)
                fields = list(reader.fieldnames or FIELDS)
                rows = list(reader)
            old = next((x for x in rows if x['id'] == args.id), {})
            if old and old['market'] != args.market: raise ValueError('此编号已属于另一个地区，请换编号。')
            row = {**{key: '' for key in fields}, **old, 'id': args.id, 'market': args.market,
                   'company': args.company, 'role': args.role, 'status': args.status,
                   'created': old.get('created') or now(), 'updated': now()}
            for key in ('job_id', 'applied_at', 'location', 'url', 'note'):
                value = getattr(args, key)
                if value is not None: row[key] = value
            fields += [key for key in row if key not in fields]
            if args.status == 'submitted' and not row['applied_at']: row['applied_at'] = now()
            rows = [row if x['id'] == args.id else x for x in rows] if old else rows + [row]
            buffer = io.StringIO(newline='')
            writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator='\n')
            writer.writeheader();writer.writerows(rows)
            atomic_text(data / 'applications.csv', buffer.getvalue())
    print('本地记录已更新，工作台下一次刷新显示。')


def main():
    parser = argparse.ArgumentParser(description='由本人或 Work 维护资料、待确认问题和申请记录；不执行投递')
    parser.add_argument('--data', default='data/personal')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='新建空白个人工作区，不覆盖已有目录')
    question = commands.add_parser('question', help='登记一个需要本人确认的问题')
    question.add_argument('--text', required=True)
    answer = commands.add_parser('answer', help='将本人明确回答保存为事实，并移除待确认问题')
    answer.add_argument('--text', required=True)
    application = commands.add_parser('application', help='新增或更新一条申请记录，不操作招聘网站')
    application.add_argument('--status', choices=['submitted', 'attention', 'excluded'], required=True)
    for name in ('job-id', 'applied-at', 'location', 'url', 'note'): application.add_argument('--' + name)
    for command in (question, answer, application): command.add_argument('--id', required=True)
    for command in (question, application):
        command.add_argument('--market', choices=['CN', 'US'], required=True)
        command.add_argument('--company', required=True)
        command.add_argument('--role', required=True)
    args = parser.parse_args()
    try: execute(args)
    except (OSError, ValueError, KeyError, TypeError) as error: parser.exit(1, str(error) + '\n')

if __name__ == '__main__': main()
