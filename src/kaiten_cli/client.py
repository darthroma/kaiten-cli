"""Direct Kaiten REST API transport. No subprocess, redirect or automatic retry."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .policy import KaitenError, normalize_tenant


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class KaitenClient:
    def __init__(self, credentials, *, open_request=None, timeout=30):
        self.credentials = credentials
        self.base_url = "https://" + normalize_tenant(credentials.tenant) + "/api/latest"
        self.open_request = open_request or build_opener(NoRedirect()).open
        self.timeout = timeout

    def request(self, method, path, *, params=None, body=None):
        if method not in {"GET", "POST", "PATCH"} or not re.fullmatch(r"/[a-z]+(?:/(?:[a-z]+|[1-9][0-9]*))*", path):
            raise KaitenError("request_invalid", "Use a supported relative API resource path.")
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=payload, method=method, headers={
            "Authorization": "Bearer " + self.credentials.token,
            "Accept": "application/json", "Content-Type": "application/json",
            "X-kaiten-Client": "kaiten-agent-cli", "X-kaiten-Client-Version": __version__,
        })
        try:
            with self.open_request(request, timeout=self.timeout) as response:
                status = response.status
                if not 200 <= status < 300:
                    self._http_error(status)
                raw = response.read(16 * 1024 * 1024 + 1)
        except HTTPError as exc:
            # Never read/echo error bodies or headers; they can contain secrets.
            try:
                self._http_error(exc.code)
            finally:
                exc.close()
        except (URLError, TimeoutError, OSError) as exc:
            raise KaitenError("network", "Kaiten API request failed; no automatic retry was attempted.", {"retry_safe": False}) from exc
        if len(raw) > 16 * 1024 * 1024:
            raise KaitenError("response_too_large", "Kaiten response exceeded the 16 MiB limit.")
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise KaitenError("bad_json", "Kaiten API did not return valid JSON.") from exc

    @staticmethod
    def _http_error(status):
        code = {401: "auth", 403: "forbidden", 404: "not_found", 429: "rate_limited"}.get(status,
                "network" if status >= 500 else "api_error")
        if 300 <= status < 400:
            code = "redirect_blocked"
        raise KaitenError(code, "Kaiten API request failed; response details are withheld.",
                          {"status_code": status, "retry_safe": False})
