"""A requests transport that answers from a Python function and records what was sent."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlsplit

import requests
from requests.adapters import BaseAdapter


class FakeTransport(BaseAdapter):
    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.sent: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        result = self.handler(request)
        status, body = result[0], result[1]
        headers = result[2] if len(result) > 2 else {}
        resp = requests.Response()
        resp.status_code = status
        resp._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        resp.headers.update({"Content-Type": "application/json", **headers})
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


def session_with(handler):
    transport = FakeTransport(handler)
    session = requests.Session()
    session.mount("https://", transport)
    session.mount("http://", transport)
    return session, transport


def query(request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(request.url).query, keep_blank_values=True))


def path(request) -> str:
    return urlsplit(request.url).path
