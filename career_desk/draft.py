"""One explicit request; no retry, no submission, no browser automation."""
import argparse, asyncio, json, sys
from pathlib import Path
from urllib.parse import urlparse
from aiohttp import ClientSession, ClientTimeout

async def run(args):
    profile = json.loads(Path(args.profile).read_text(encoding='utf-8'))
    jd = Path(args.jd).read_text(encoding='utf-8')
    prompt = '根据下列已确认资料和岗位描述，起草一份申请材料。不要编造经历或数字；未知事实列为待确认问题。只输出草稿，不执行任何投递。\n已确认资料：' + json.dumps(profile, ensure_ascii=False) + '\n岗位描述：' + jd
    headers = {'X-Chat-Name': args.chat} if args.chat else {}
    # HTTP headers support ASCII; non-ASCII chat names use a JSON-safe escape representation.
    headers = {k: json.dumps(v, ensure_ascii=True)[1:-1] for k, v in headers.items()}
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=5)) as session:
        async with session.post(args.endpoint.rstrip('/') + '/api/chat', headers=headers,
            json={'model': args.model, 'messages': [{'role': 'user', 'content': prompt}], 'stream': False}) as response:
            response.raise_for_status()
            result = await response.json()
    text = result.get('message', {}).get('content') or result.get('response', '')
    with Path(args.output).open('x', encoding='utf-8') as handle: handle.write(text)
    print(f'草稿已保存：{args.output}，请审核。')

def main():
    p = argparse.ArgumentParser(description='交给本地模型起草材料，完成后交回人工审核')
    p.add_argument('--model', required=True)
    p.add_argument('--jd', required=True)
    p.add_argument('--profile', default='data/profile.json')
    p.add_argument('--output', default='draft.txt')
    p.add_argument('--chat', default='')
    p.add_argument('--endpoint', default='http://127.0.0.1:11439')
    args = p.parse_args()
    if urlparse(args.endpoint).hostname not in ('localhost', '127.0.0.1', '::1'): p.error('只支持本机模型地址')
    if Path(args.output).exists(): p.error('输出文件已存在，请换一个文件名')
    try: asyncio.run(run(args))
    except KeyboardInterrupt: sys.exit('已取消，不会重新发送请求。')
    except Exception as e: sys.exit(f'调用未完成：{type(e).__name__}；不会自动重发，请先检查模型端状态。')

if __name__ == '__main__': main()
