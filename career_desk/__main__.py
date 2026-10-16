import argparse, asyncio, os, shutil
from pathlib import Path
from urllib.parse import urlparse
from aiohttp import web
from .server import Dashboard
from .model_gateway import create_app
from . import events

async def serve(args):
    data = Path(args.data).resolve()
    if not data.exists():
        data.mkdir(parents=True, mode=0o700)
        examples = Path(__file__).resolve().parent.parent / 'examples'
        for name in ('applications.csv', 'profile.json', 'pending.json'):
            shutil.copyfile(examples / name, data / name)
            (data / name).chmod(0o600)
    events.ROOT = data
    proxy = f'http://127.0.0.1:{args.proxy_port}'
    dashboard = Dashboard(data, args.upstream.rstrip('/'), proxy)
    runners = []
    try:
        for app, port in ((create_app(args.upstream.rstrip('/')), args.proxy_port),
                          (dashboard.app(), args.port)):
            runner = web.AppRunner(app, handler_cancellation=True)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', port).start()
        print(f'Career Desk: http://127.0.0.1:{args.port}/\n本地模型监测地址: {proxy}\n按 Control+C 关闭。原 Ollama 服务不受接管。', flush=True)
        await asyncio.Event().wait()
    finally:
        for runner in reversed(runners): await runner.cleanup()

def main():
    parser = argparse.ArgumentParser(description='Career Desk — 本地模型活动与求职记录参考工作台')
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--proxy-port', type=int, default=11439)
    parser.add_argument('--upstream', default=os.getenv('OLLAMA_UPSTREAM', 'http://127.0.0.1:11434'))
    parser.add_argument('--data', default=os.getenv('CAREER_DESK_DATA', 'data'))
    args = parser.parse_args()
    parsed = urlparse(args.upstream)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password:
        parser.error('upstream 必须是本机 Ollama HTTP 地址')
    if args.port == args.proxy_port or not all(1024 <= p <= 65535 for p in (args.port, args.proxy_port)):
        parser.error('请设置两个不同的 1024–65535 端口')
    try: asyncio.run(serve(args))
    except KeyboardInterrupt: pass
    except OSError as error: parser.exit(1, f'启动失败：{error}；请换端口，程序不会关闭已有服务。\n')

if __name__ == '__main__': main()
