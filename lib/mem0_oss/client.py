"""mem0 OSS server(server/main.py)的 REST 客户端。

契约来自 mem0ai/mem0@43dde3b。注意与 Mem0 Platform 不同:
路径没有 /v1、/v2 前缀,认证头是 X-API-Key 而非 Authorization: Token。
"""

import json
import urllib.error
import urllib.parse
import urllib.request


class Mem0Error(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def normalize_results(payload):
    """把响应收敛成一个 dict 列表。形状不对就当空结果,不抛异常。"""
    if isinstance(payload, dict):
        results = payload.get("results")
    elif isinstance(payload, list):
        results = payload
    else:
        results = None
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict)]


class Client:
    def __init__(self, base_url, api_key="", timeout_ms=30000):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_ms = timeout_ms

    def _request(self, method, path, body=None, timeout_ms=None):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-API-Key"] = self.api_key

        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        timeout = (self.timeout_ms if timeout_ms is None else timeout_ms) / 1000.0

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # HTTPError 是 URLError 的子类,必须先捕获,否则状态码会丢。
            # HTTPError 本身是个类文件对象(未被 with 语句接管),不显式
            # close() 的话,它会被隐式异常链(__context__)一直吊着引用,
            # 直到某次延迟的循环 GC 才被回收,期间会触发 ResourceWarning。
            exc.close()
            raise Mem0Error("HTTP %d" % exc.code, status=exc.code) from None
        except Exception as exc:
            raise Mem0Error(str(exc) or exc.__class__.__name__) from None

        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Mem0Error("response was not valid JSON")

    def search(self, query, user_id, agent_id="", top_k=5, timeout_ms=None):
        filters = {"user_id": user_id}
        if agent_id:
            filters["agent_id"] = agent_id
        body = {"query": query, "filters": filters, "top_k": top_k}
        return normalize_results(self._request("POST", "/search", body, timeout_ms))

    def add(self, messages, user_id, agent_id="", metadata=None, infer=True, timeout_ms=None):
        body = {"messages": messages, "user_id": user_id, "infer": infer}
        if agent_id:
            body["agent_id"] = agent_id
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", "/memories", body, timeout_ms)

    def get_all(self, user_id, agent_id="", timeout_ms=None):
        params = {"user_id": user_id}
        if agent_id:
            params["agent_id"] = agent_id
        path = "/memories?" + urllib.parse.urlencode(params)
        return normalize_results(self._request("GET", path, None, timeout_ms))

    def delete(self, memory_id, timeout_ms=None):
        path = "/memories/" + urllib.parse.quote(str(memory_id), safe="")
        self._request("DELETE", path, None, timeout_ms)
