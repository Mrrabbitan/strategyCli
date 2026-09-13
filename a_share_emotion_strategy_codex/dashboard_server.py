"""Loopback-only static workbench and fixed, read-only monitoring endpoints."""
from __future__ import annotations
import argparse
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote
from zoneinfo import ZoneInfo
from research_store import private_path, read_json
from monitor_feed import read_status, read_events

TZ=ZoneInfo('Asia/Shanghai')


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def reply(self,code,body=b'',kind='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)

    def json(self,value,code=200):
        self.reply(code,json.dumps(value,ensure_ascii=False,allow_nan=False).encode())

    def do_POST(self): self.json({'error':'read_only'},405)
    do_PUT=do_DELETE=do_PATCH=do_OPTIONS=do_POST
    def do_HEAD(self): self.do_GET()

    def do_GET(self):
        port=self.server.server_address[1]
        hosts={f'127.0.0.1:{port}',f'localhost:{port}'}
        origin=self.headers.get('Origin')
        if self.headers.get('Host') not in hosts or (origin and origin not in {f'http://{x}' for x in hosts}) or self.headers.get('Sec-Fetch-Site')=='cross-site':
            self.json({'error':'local_only'},403);return
        parsed=urlsplit(self.path);path=unquote(parsed.path)
        try:
            if path=='/api/monitor-status':
                if parsed.query:raise ValueError('unexpected query')
                self.json(read_status());return
            if path=='/api/monitor-events':
                query=parse_qs(parsed.query,strict_parsing=True,keep_blank_values=True) if parsed.query else {}
                if set(query)-{'date'} or any(len(v)!=1 for v in query.values()):raise ValueError('invalid query')
                day=dt.date.fromisoformat(query['date'][0]) if 'date' in query else None
                if day and day>dt.datetime.now(TZ).date():raise ValueError('future date')
                self.json(read_events(day=day));return
            root=getattr(self.server,'public_root',private_path('public'))
            manifest=read_json(root/'version.json',{})
            if path=='/api/dashboard-version':
                if parsed.query:raise ValueError('unexpected query')
                self.json({k:manifest.get(k) for k in ('revision','updated_at')});return
            if parsed.query:raise ValueError('unexpected query')
            relative='latest.html' if path=='/' else path.lstrip('/')
            if relative not in manifest.get('files',[]) or '\\' in path or '..' in Path(relative).parts:
                self.json({'error':'not_found'},404);return
            target=root/relative
            if target.is_symlink() or not target.is_file():
                self.json({'error':'not_found'},404);return
            target.resolve().relative_to(root.resolve())
            content_type=mimetypes.guess_type(relative)[0] or 'application/octet-stream'
            self.reply(200,target.read_bytes(),content_type+('; charset=utf-8' if content_type.startswith('text/') else ''))
        except (ValueError,TypeError):self.json({'error':'invalid_request'},400)
        except (OSError,KeyError,AttributeError):self.json({'error':'data_unavailable'},503)


def make_server(port=8767,root=None):
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    server.public_root=Path(root) if root else private_path('public')
    return server


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,default=8767)
    args=parser.parse_args()
    if not (private_path('public')/'version.json').exists():
        from build_investment_site import build
        build()
    server=make_server(args.port)
    print(f'http://127.0.0.1:{args.port}/latest.html',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
