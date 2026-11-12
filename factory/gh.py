"""A small GitHub REST and GraphQL client, standard library only.

Why it exists: the workflows that run this code may not install packages at
run time, and every caller needs the same three things: a token passed in
(never read from global state), retries on the failures GitHub documents as
transient, and pagination. Every method raises ApiError on a non-2xx answer,
so a caller that expects a refusal (a 405 from the merge API, say) catches it
and records it.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DEFAULT_API = "https://api.github.com"
API_VERSION = "2022-11-28"

# A transport takes (method, url, headers, body bytes or None) and returns
# (status, headers as a lower-cased dict, body bytes). Tests pass their own.
Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, dict, bytes]"]


class ApiError(Exception):
    """A non-2xx answer from GitHub, or a GraphQL answer carrying errors."""

    def __init__(self, status: int, body: Any, path: str):
        self.status = status
        self.body = body
        self.path = path
        message = body.get("message") if isinstance(body, dict) else body
        super().__init__(f"GitHub API {status} on {path}: {message}")

    @property
    def message(self) -> str:
        if isinstance(self.body, dict):
            return str(self.body.get("message", ""))
        return str(self.body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Redirects are followed by hand so that the Authorization header is not
    # sent to the storage host an artifact download redirects to.
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None,
                     timeout: float = 60.0) -> tuple[int, dict, bytes]:
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""


_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHub:
    """REST + GraphQL client. One instance per token; no module-level state."""

    def __init__(self, token: str | None, api: str = DEFAULT_API, *,
                 transport: Transport | None = None, retries: int = 4,
                 backoff: float = 2.0, sleep: Callable[[float], None] = time.sleep,
                 user_agent: str = "factory-controls"):
        self.token = token
        self.api = api.rstrip("/")
        self.transport = transport or urllib_transport
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self.user_agent = user_agent

    # -- plumbing -----------------------------------------------------------

    def _url(self, path: str, params: dict | None = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = f"{self.api}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                sep = "&" if "?" in url else "?"
                url = url + sep + urllib.parse.urlencode(clean)
        return url

    def _headers(self, accept: str | None, has_body: bool, auth: bool = True) -> dict:
        h = {
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": self.user_agent,
        }
        if self.token and auth:
            h["Authorization"] = f"Bearer {self.token}"
        if has_body:
            h["Content-Type"] = "application/json"
        return h

    def _retryable(self, method: str, status: int, headers: dict, body: bytes) -> float | None:
        """Seconds to wait before a retry, or None when the answer is final.

        Rate limits (429, or 403 with a retry-after or an exhausted
        x-ratelimit-remaining) are retried for every method, because GitHub
        did not process the request. 5xx is retried only for methods where a
        repeat is harmless or detectable (not POST, which could create a
        second issue or comment).
        """
        if status == 429 or (status == 403 and (
                "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
                or b"secondary rate limit" in body.lower())):
            ra = headers.get("retry-after")
            if ra and ra.isdigit():
                return min(float(ra), 120.0)
            reset = headers.get("x-ratelimit-reset")
            if reset and reset.isdigit():
                return max(1.0, min(float(reset) - time.time(), 120.0))
            return 60.0
        if status in (500, 502, 503, 504) and method != "POST":
            return None  # caller computes backoff
        return -1.0  # sentinel: final

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: Any = None, accept: str | None = None) -> tuple[int, dict, bytes]:
        url = self._url(path, params)
        data = None if body is None else json.dumps(body).encode()
        attempt = 0
        while True:
            try:
                status, headers, raw = self.transport(method, url, self._headers(accept, data is not None), data)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                if attempt >= self.retries or method == "POST":
                    raise
                self.sleep(self.backoff * (2 ** attempt))
                attempt += 1
                continue
            if 200 <= status < 300 or (300 <= status < 400):
                return status, headers, raw
            wait = self._retryable(method, status, headers, raw)
            if wait is not None and wait < 0:
                raise ApiError(status, _decode(raw), path)
            if attempt >= self.retries:
                raise ApiError(status, _decode(raw), path)
            self.sleep(wait if wait is not None else self.backoff * (2 ** attempt))
            attempt += 1

    # -- REST ---------------------------------------------------------------

    def get(self, path: str, /, **params) -> Any:
        status, headers, raw = self._request("GET", path, params)
        if 300 <= status < 400:
            raise ApiError(status, {"message": "unexpected redirect", "location": headers.get("location")}, path)
        return _decode(raw)

    def paginate(self, path: str, /, **params) -> list:
        """All pages of a list endpoint.

        Some endpoints wrap the list in an object (workflow runs, jobs, check
        runs, artifacts, installation repositories); the list is the one
        list-valued member of that object.
        """
        params = dict(params)
        params.setdefault("per_page", 100)
        url = self._url(path, params)
        out: list = []
        while url:
            status, headers, raw = self._request("GET", url)
            data = _decode(raw)
            if isinstance(data, dict):
                lists = [v for v in data.values() if isinstance(v, list)]
                if len(lists) != 1:
                    raise ApiError(status, {"message": "paginate: no single list in response"}, path)
                data = lists[0]
            out.extend(data)
            m = _LINK_NEXT.search(headers.get("link", ""))
            url = m.group(1) if m else None
        return out

    def post(self, path: str, body: Any = None) -> Any:
        return _decode(self._request("POST", path, body=body if body is not None else {})[2])

    def put(self, path: str, body: Any = None) -> Any:
        return _decode(self._request("PUT", path, body=body if body is not None else {})[2])

    def patch(self, path: str, body: Any = None) -> Any:
        return _decode(self._request("PATCH", path, body=body if body is not None else {})[2])

    def delete(self, path: str, body: Any = None) -> Any:
        return _decode(self._request("DELETE", path, body=body)[2])

    def get_raw(self, path: str, /, **params) -> bytes:
        """Bytes of an endpoint that answers with a redirect to storage.

        The redirect target gets no Authorization header.
        """
        status, headers, raw = self._request("GET", path, params)
        hops = 0
        while 300 <= status < 400 and hops < 5:
            loc = headers.get("location")
            if not loc:
                raise ApiError(status, {"message": "redirect without location"}, path)
            status, headers, raw = self.transport("GET", loc, {"User-Agent": self.user_agent}, None)
            hops += 1
        if not 200 <= status < 300:
            raise ApiError(status, _decode(raw), path)
        return raw

    # -- GraphQL ------------------------------------------------------------

    def graphql(self, query: str, variables: dict, *, accept: str | None = None) -> dict:
        """`accept` names a preview media type for fields still under one."""
        data = _decode(self._request("POST", "graphql", body={"query": query, "variables": variables},
                                     accept=accept)[2])
        if isinstance(data, dict) and data.get("errors"):
            raise ApiError(200, data, "graphql")
        return data.get("data", {}) if isinstance(data, dict) else {}


class DryRunGitHub:
    """Wraps a GitHub client: reads go through, writes are recorded, not sent.

    Used by `--dry-run` so the detector and the merge step can be run against
    the live repositories without changing anything. A write returns a stub
    object; a caller that needs the real answer of a write cannot be dry-run
    past that point, and the stub says so.
    """

    def __init__(self, inner: GitHub):
        self.inner = inner
        self.writes: list[dict] = []

    def get(self, path, /, **params):
        return self.inner.get(path, **params)

    def paginate(self, path, /, **params):
        return self.inner.paginate(path, **params)

    def get_raw(self, path, /, **params):
        return self.inner.get_raw(path, **params)

    def _write(self, method, path, body):
        self.writes.append({"method": method, "path": path, "body": body})
        return {"dry_run": True, "method": method, "path": path}

    def post(self, path, body=None):
        return self._write("POST", path, body)

    def put(self, path, body=None):
        return self._write("PUT", path, body)

    def patch(self, path, body=None):
        return self._write("PATCH", path, body)

    def delete(self, path, body=None):
        return self._write("DELETE", path, body)

    def graphql(self, query, variables, **kwargs):
        if query.lstrip().startswith("mutation"):
            return self._write("GRAPHQL", "graphql", {"query": query, "variables": variables})
        return self.inner.graphql(query, variables, **kwargs)


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key_pem: str, now: int | None = None) -> str:
    """An App JWT (RS256), signed with the openssl CLI.

    The standard library has no RSA, and this path is for local dry runs
    only; workflows use actions/create-github-app-token.
    """
    now = int(time.time()) if now is None else now
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)},
                                 separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    fd, keyfile = tempfile.mkstemp(prefix="app-key-", suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(private_key_pem)
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", keyfile],
                             input=signing_input, capture_output=True, check=True).stdout
    finally:
        os.unlink(keyfile)
    return f"{header}.{payload}.{_b64url(sig)}"


def app_installation_token(app_id: str, private_key_pem: str, owner: str,
                           repositories: list[str], *, api: str = DEFAULT_API,
                           transport: Transport | None = None) -> str:
    """A one-hour installation token scoped to `repositories` of `owner`.

    Used only where actions/create-github-app-token is not available (local
    dry runs).
    """
    if not repositories:
        raise ValueError("repositories must name at least one repository")
    jwt_client = GitHub(app_jwt(app_id, private_key_pem), api, transport=transport)
    inst = jwt_client.get(f"repos/{owner}/{repositories[0]}/installation")
    tok = jwt_client.post(f"app/installations/{inst['id']}/access_tokens",
                          {"repositories": list(repositories)})
    return tok["token"]
