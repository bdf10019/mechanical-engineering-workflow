"""Independent loopback workflow; does not construct legacy Jobs or CAD engines."""
import argparse
import http.server
import secrets
import socket
import threading
from pathlib import Path
from urllib.parse import urlsplit
from app.photo3d.data import ROOT, MAX_BYTES
from app.workflow.core import Workflow, Conflict, VERSION, encoded, exact, parse
from app.workflow.cloud import Cloud

WEB = Path(__file__).parent / 'web'
PORT = 8171


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def handler(workflow, cloud, port, token):
    origin = f'http://127.0.0.1:{port}'

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def drain_small_body(self):
            if self.command not in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS') or getattr(self, '_body_read', False):
                return
            self._body_read = True
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if 0 < size <= 4096:
                    self.connection.settimeout(1)
                    self.rfile.read(size)
            except (ValueError, OSError, TimeoutError):
                pass

        def reply(self, content, status=200, mime='application/json; charset=utf-8', attachment=None):
            # Bounded discard also covers 415/405, not just authorization rejects.
            self.drain_small_body()
            if isinstance(content, (list, dict)):
                content = encoded(content)
            if isinstance(content, str):
                content = content.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if attachment:
                self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(content)

        def guard(self, post):
            if self.headers.get('Host') != f'127.0.0.1:{port}':
                raise PermissionError('只接受本機操作')
            navigation = not post and self.path == '/' and self.headers.get('Sec-Fetch-Mode') == 'navigate'
            if self.headers.get('Sec-Fetch-Site') == 'cross-site' and not navigation:
                raise PermissionError('請從本機頁面操作')
            if post and (self.headers.get('Origin') != origin or not secrets.compare_digest(self.headers.get('X-Session-Token', ''), token)):
                raise PermissionError('請重新開啟本機頁面')

        def run(self, post):
            self.guard(post)
            url = urlsplit(self.path)
            if url.query or url.fragment:
                raise ValueError('路徑不接受額外參數')
            parts = url.path.strip('/').split('/')
            if post:
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.reply({'error': '只接受 JSON'}, 415)
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise ValueError('內容長度不符') from None
                limit = MAX_BYTES * 4 // 3 + 4096 if len(parts) == 4 and parts[3] == 'photos' else 1024 * 1024
                if not 0 < size <= limit:
                    return self.reply({'error': '內容容量超限'}, 413)
                self.connection.settimeout(30)
                raw = self.rfile.read(size)
                self._body_read = True
                if len(raw) != size:
                    raise ValueError('內容不完整')
                value = parse(raw)
                if parts == ['api', 'tasks']:
                    exact(value, ['name'])
                    return self.reply(workflow.create(**value), 201)
                if parts == ['api', 'connection']:
                    exact(value, ['provider', 'model', 'api_key', 'max_output_tokens'])
                    return self.reply(cloud.configure(**value))
                if parts == ['api', 'disconnect']:
                    exact(value, [])
                    return self.reply(cloud.disconnect())
                if len(parts) == 4 and parts[:2] == ['api', 'tasks']:
                    tid, operation = parts[2:]
                    methods = {
                        'save': (['expected_revision', 'name', 'requirements', 'dimensions'], workflow.save),
                        'photos': (['expected_revision', 'filename', 'base64'], None),
                        'remove-photo': (['expected_revision', 'photo_id'], workflow.remove_photo),
                        'prepare': (['expected_revision'], workflow.prepare),
                        'import': (['expected_revision', 'prepared_id', 'reply'], workflow.import_reply),
                        'review': (['expected_revision', 'response_id', 'decision'], workflow.review),
                        'analyze': (['expected_revision', 'prepared_id', 'sha256', 'consent', 'expected_connection_id'], cloud.analyze)}
                    if operation in methods:
                        fields, method = methods[operation]
                        exact(value, fields)
                        if operation == 'photos':
                            return self.reply(workflow.upload(tid, value['expected_revision'], value['filename'], value['base64']))
                        return self.reply(method(tid, **value))
                return self.reply({'error': '沒有此操作'}, 404)
            if url.path in ('/', '/workflow.css', '/workflow.js', '/workflow-state.mjs'):
                name = 'index.html' if url.path == '/' else parts[0]
                mime = 'text/html; charset=utf-8' if name.endswith('.html') else 'text/css; charset=utf-8' if name.endswith('.css') else 'text/javascript; charset=utf-8'
                return self.reply((WEB / name).read_bytes(), mime=mime)
            if parts == ['api', 'session']:
                return self.reply({'token': token})
            if parts == ['api', 'health']:
                return self.reply(dict(application='mechanical-workflow', version=VERSION, local_only=True,
                                       live_cloud_verified=False, photo_to_cad=False, drawing_output=False))
            if parts == ['api', 'connection']:
                return self.reply(cloud.public())
            if parts == ['api', 'tasks']:
                return self.reply(workflow.listing())
            if len(parts) == 3 and parts[:2] == ['api', 'tasks']:
                return self.reply(workflow.get(parts[2]))
            if len(parts) == 5 and parts[:2] == ['api', 'tasks'] and parts[3] == 'photos':
                return self.reply(workflow.photo(parts[2], parts[4]), mime='image/png')
            if len(parts) == 4 and parts[:2] == ['api', 'tasks']:
                if parts[3] == 'package':
                    return self.reply(workflow.package(parts[2]), mime='application/zip', attachment='engineering-handoff.zip')
                if parts[3] == 'response-template':
                    return self.reply(workflow.template(parts[2]), attachment='response-template.json')
            return self.reply({'error': '找不到資源'}, 404)

        def dispatch(self, post=False):
            try:
                self.run(post)
            except Conflict as exc:
                self.reply({'error': str(exc)}, 409)
            except PermissionError:
                self.reply({'error': '僅限本機同來源操作，請重新開啟頁面'}, 403)
            except FileNotFoundError:
                self.reply({'error': '找不到任務或資源'}, 404)
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
                # Parsing/provider errors must not reflect raw text or secret-bearing details.
                message = str(exc) if type(exc) is ValueError and not isinstance(exc, __import__('json').JSONDecodeError) else '資料格式不符'
                self.reply({'error': message[:200]}, 400)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
                pass
            except Exception:
                self.reply({'error': '本機操作未完成，原資料保留'}, 500)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch(True)

        def do_PUT(self):
            self.reply({'error': '不支援此方法'}, 405)
        do_DELETE = do_PATCH = do_OPTIONS = do_PUT
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    server = Server(('127.0.0.1', PORT), http.server.BaseHTTPRequestHandler)
    workflow = Workflow()
    cloud = Cloud(workflow)
    server.RequestHandlerClass = handler(workflow, cloud, PORT, secrets.token_urlsafe(32))
    print(f'LOCAL_URL=http://127.0.0.1:{PORT}/', flush=True)
    if args.open_browser:
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(f'http://127.0.0.1:{PORT}/',), daemon=True).start()
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        cloud.config = None
        server.server_close()


if __name__ == '__main__':
    main()
