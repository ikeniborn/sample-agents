"""Minimal Connect RPC client using JSON protocol over httpx."""
import httpx
from google.protobuf.json_format import MessageToJson, ParseDict
from connectrpc.errors import ConnectError
from connectrpc.code import Code


class ConnectClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def call(self, service: str, method: str, request, response_type):
        url = f"{self._base_url}/{service}/{method}"
        body = MessageToJson(request)
        resp = httpx.post(
            url,
            content=body,
            headers={"Content-Type": "application/json"},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            try:
                err = resp.json()
                msg = err.get("message", resp.text)
                code_str = err.get("code", "unknown")
            except Exception:
                msg = resp.text
                code_str = "unknown"
            raise ConnectError(Code[code_str.upper()] if code_str.upper() in Code.__members__ else Code.UNKNOWN, msg)
        return ParseDict(resp.json(), response_type(), ignore_unknown_fields=True)
