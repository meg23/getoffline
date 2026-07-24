"""Transport adapters used by the GetOffline SDK."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from http.client import HTTPResponse
from typing import IO, Any, Protocol, cast
import urllib.error
import urllib.parse
import urllib.request


@dataclass(frozen=True)
class Response:
    """Minimal response object shared by SDK transports."""

    status_code: int
    content: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    cookies: tuple[str, ...] = ()
    streaming: bool = False

    @property
    def ok(self) -> bool:
        return self.status_code < 400


class Transport(Protocol):
    """Sends an API request and returns a transport-neutral response."""

    def request(
        self,
        method: str,
        target: str,
        args: tuple[object, ...] = (),
        *,
        query: Mapping[str, object] | None = None,
        data: object | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response: ...


class DjangoResponseLike(Protocol):
    status_code: int

    def items(self) -> Iterable[tuple[str, str]]: ...


class DjangoTestClient(Protocol):
    def get(
        self, path: str, data: object | None = None, **extra: object
    ) -> DjangoResponseLike: ...

    def post(
        self, path: str, data: object | None = None, **extra: object
    ) -> DjangoResponseLike: ...


class DjangoTransport:
    """In-process transport for tests and monolith deployments."""

    def __init__(self, client: DjangoTestClient, *, api_prefix: str = "/api") -> None:
        self.client = client
        self.api_prefix = api_prefix.rstrip("/")

    def request(
        self,
        method: str,
        target: str,
        args: tuple[object, ...] = (),
        *,
        query: Mapping[str, object] | None = None,
        data: object | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        django_headers = _django_headers(headers or {})
        path = _django_path(target, args, self.api_prefix)
        if method.upper() == "POST":
            response = self.client.post(path, data=data or {}, **django_headers)
        else:
            response = self.client.get(path, data=query or {}, **django_headers)
        return Response(
            status_code=response.status_code,
            content=_django_response_content(response),
            headers={key: value for key, value in response.items()},
            cookies=_django_response_cookies(response),
            streaming=bool(getattr(response, "streaming", False)),
        )


class HttpTransport:
    """HTTP(S) transport for split frontend/API deployments."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        target: str,
        args: tuple[object, ...] = (),
        *,
        query: Mapping[str, object] | None = None,
        data: object | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        url = self._url(target, args, query if method.upper() == "GET" else None)
        body, content_type = _encoded_body(data)
        request_headers = dict(headers or {})
        if body is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = content_type
        req = urllib.request.Request(
            url, data=body, headers=request_headers, method=method.upper()
        )
        try:
            # The upstream API URL is configured by the deployment environment.
            upstream = urllib.request.build_opener(_NoRedirectHandler()).open(
                req, timeout=self.timeout_seconds
            )
            return _http_response(upstream)
        except urllib.error.HTTPError as exc:
            return _http_error_response(exc)

    def _url(
        self,
        target: str,
        args: tuple[object, ...],
        query: Mapping[str, object] | None,
    ) -> str:
        api_path = _api_path(target, args)
        url = f"{self.base_url}{api_path}"
        if query:
            return f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
        return url


def _api_path(target: str, args: tuple[object, ...]) -> str:
    if target.startswith("/"):
        suffix = "/".join(str(arg).strip("/") for arg in args)
        path = target if not suffix else f"{target.rstrip('/')}/{suffix}"
        return path if path.startswith("/") else f"/{path}"
    return _reverse_route(target, args).removeprefix("/api")


def _django_path(target: str, args: tuple[object, ...], api_prefix: str) -> str:
    if target.startswith("/"):
        return f"{api_prefix}{_api_path(target, args)}"
    return _reverse_route(target, args)


def _reverse_route(target: str, args: tuple[object, ...]) -> str:
    from django.urls import reverse

    return str(reverse(target, args=args))


def _django_response_content(response: object) -> bytes:
    if hasattr(response, "streaming_content"):
        return b"".join(cast(Any, response).streaming_content)
    return bytes(cast(Any, response).content)


def _django_headers(headers: Mapping[str, str]) -> dict[str, str]:
    converted: dict[str, str] = {}
    for key, value in headers.items():
        if key.lower() == "content-type":
            converted["content_type"] = value
        elif key.lower() == "range":
            converted["HTTP_RANGE"] = value
        else:
            converted[f"HTTP_{key.upper().replace('-', '_')}"] = value
    return converted


def _encoded_body(data: object | None) -> tuple[bytes | None, str]:
    if data is None:
        return None, "application/octet-stream"
    if not _contains_upload(data):
        form_data = cast(Any, data)
        return (
            urllib.parse.urlencode(form_data, doseq=True).encode("utf-8"),
            "application/x-www-form-urlencoded",
        )
    boundary = "----getoffline-sdk-boundary"
    chunks: list[bytes] = []
    for key, value in _iter_fields(data):
        if _is_upload(value):
            filename = getattr(value, "name", "upload")
            content_type = (
                getattr(value, "content_type", None) or "application/octet-stream"
            )
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode(),
                ]
            )
            chunks.extend(cast(Any, value).chunks())
            chunks.append(b"\r\n")
        else:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                    str(value).encode(),
                    b"\r\n",
                ]
            )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _contains_upload(data: object) -> bool:
    return any(_is_upload(value) for _, value in _iter_fields(data))


def _iter_fields(data: object) -> Iterable[tuple[str, object]]:
    fields = cast(Mapping[str, object], data)
    for key, raw_value in fields.items():
        if isinstance(raw_value, list | tuple):
            for value in raw_value:
                yield key, value
        else:
            yield key, raw_value


def _is_upload(value: object) -> bool:
    return callable(getattr(value, "chunks", None))


def _django_response_cookies(response: object) -> tuple[str, ...]:
    cookies = getattr(response, "cookies", None)
    if not cookies:
        return ()
    return tuple(morsel.OutputString() for morsel in cookies.values())


def _http_response(upstream: HTTPResponse) -> Response:
    headers = dict(upstream.headers.items())
    return Response(
        status_code=upstream.status,
        content=upstream.read(),
        headers=headers,
        cookies=tuple(upstream.headers.get_all("Set-Cookie", [])),
    )


def _http_error_response(exc: urllib.error.HTTPError) -> Response:
    headers = dict(exc.headers.items())
    return Response(
        status_code=exc.code,
        content=exc.read(),
        headers=headers,
        cookies=tuple(exc.headers.get_all("Set-Cookie", [])),
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Leave redirect responses for the browser-facing proxy to handle."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None
