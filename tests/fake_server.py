"""假的 mem0 OSS server,用来断言我们真正发出去的 wire 格式。

不 mock urllib —— mock 掉传输层就测不出 header 名字写错、
路径少个斜杠这类问题,而那恰恰是最可能出错的地方。
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 保持测试输出干净

    def _handle(self):
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except ValueError:
                body = raw.decode("utf-8", "replace")

        fake.requests.append(
            {
                "method": self.command,
                "path": self.path,
                # 不转成 plain dict:urllib.request 的 do_open() 会把所有
                # 头名统一转成 str.title()(如 "X-API-Key" -> "X-Api-Key"),
                # 这是 HTTP 头本就大小写不敏感的体现(RFC 7230 3.2),不是 bug。
                # self.headers 是 email.message.Message,原生大小写不敏感的
                # __getitem__/__contains__ 让断言按头名匹配而不按大小写匹配。
                "headers": self.headers,
                "body": body,
            }
        )

        if fake.delay_seconds:
            time.sleep(fake.delay_seconds)

        status, payload = fake.response_for(self.command, self.path)
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        else:
            data = str(payload).encode("utf-8")
            content_type = "text/plain"

        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionError, OSError):
            # 客户端超时后主动断开是预期行为(见 test_timeout_raises):
            # 服务端线程睡完 delay_seconds 醒来后,往一个客户端早已关闭的
            # socket 写数据只会拿到 EPIPE/ECONNRESET,不是需要暴露的 bug。
            #
            # 这里没有照抄 brief 建议的、在 _Handler 上加一个空 handle_error
            # 的方案:stdlib 里 handle_error 是 socketserver.BaseServer 的
            # 方法,ThreadingMixIn.process_request_thread 里调用的是
            # self.handle_error(...)(self 是 server 而非 handler 实例),
            # 在 _Handler 上定义同名方法根本不在这条异常传播路径上,是死代码
            # (读 CPython 3.14 的 socketserver.py/http/server.py 源码确认
            # 过)。实测这条竞态在跑完整测试套件时约 25% 概率复现出脏
            # traceback,所以直接在真正会抛异常的写入点吞掉它。
            pass

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


class FakeMem0Server:
    def __init__(self):
        self.requests = []
        self.responses = {}
        self.default_response = (200, {"results": []})
        self.delay_seconds = 0.0
        self._server = None
        self._thread = None

    def set_response(self, method, path, status, payload):
        self.responses[(method.upper(), path)] = (status, payload)

    def response_for(self, method, full_path):
        path = full_path.split("?", 1)[0]
        return self.responses.get((method.upper(), path), self.default_response)

    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def base_url(self):
        host, port = self._server.server_address
        return "http://%s:%d" % (host, port)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()
        return False
