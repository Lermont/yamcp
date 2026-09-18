"""Bounded HTTP checks for landing pages used by campaign audit."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
import zlib
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from weakref import WeakKeyDictionary

import httpx

MAX_PAGES = 30
MAX_HTML_BYTES = 2_000_000
MAX_CONCURRENT = 6
MAX_REDIRECTS = 5
BATCH_TIMEOUT_SECONDS = 40
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)
log = logging.getLogger("yadirect-mcp")


CACHE_SCOPE: ContextVar[str | None] = ContextVar("landing_cache_scope", default=None)
CACHE_TTL = 15 * 60
HOST_INTERVAL = 0.5
_CACHE: dict[tuple, tuple[float, dict]] = {}
_HOSTS: WeakKeyDictionary = WeakKeyDictionary()

class RetryLater(ValueError):
    def __init__(self, seconds: float):
        self.seconds = max(1, int(seconds + 0.999))
        super().__init__(f"Хост просит повторить через {self.seconds} с")


def retry_after(value: str | None) -> float:
    try:
        return max(0, float(value or ""))
    except ValueError:
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 30


@asynccontextmanager
async def _stream(client, target, address):
    hosts = _HOSTS.setdefault(asyncio.get_running_loop(), {})
    gate = hosts.setdefault(target.host, {"lock": asyncio.Lock(), "next": 0.0})
    async with gate["lock"]:
        wait = gate["next"] - time.monotonic()
        if wait > 5:
            raise RetryLater(wait)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with client.stream(
                "GET", target.copy_with(host=address),
                headers={"Host": target.netloc.decode("ascii")},
                extensions={"sni_hostname": target.raw_host.decode("ascii")},
            ) as response:
                if response.status_code in {429, 503}:
                    delay = retry_after(response.headers.get("Retry-After"))
                    gate["next"] = time.monotonic() + max(HOST_INTERVAL, delay)
                    raise RetryLater(delay)
                yield response
        finally:
            gate["next"] = max(gate["next"], time.monotonic() + HOST_INTERVAL)


async def _bounded_body(response):
    encoding = response.headers.get("content-encoding", "identity").lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise ValueError(f"Неподдерживаемое Content-Encoding: {encoding}")
    decoder = (zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
               if encoding != "identity" else None)
    raw = bytearray()
    received = 0
    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
        received += len(chunk)
        if decoder and received > MAX_HTML_BYTES:
            raise ValueError("Ответ превышает лимит сжатых данных")
        remaining = MAX_HTML_BYTES + 1 - len(raw)
        raw.extend(decoder.decompress(chunk, remaining) if decoder else chunk[:remaining])
        if len(raw) > MAX_HTML_BYTES:
            break
    if decoder and len(raw) <= MAX_HTML_BYTES and not decoder.eof:
        raise ValueError("Неполный сжатый ответ")
    return raw


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.meta_description: str | None = None
        self.canonical: str | None = None
        self.viewport = False
        self.has_form = False
        self.has_phone = False
        self.has_email = False
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "title":
            self._in_title = True
        elif tag.casefold() == "h1":
            self._in_h1 = True
        elif tag.casefold() == "form":
            self.has_form = True
        elif tag.casefold() == "meta":
            name = str(values.get("name") or "").casefold()
            if name == "description":
                self.meta_description = values.get("content")
            if name == "viewport":
                self.viewport = True
        elif tag.casefold() == "link":
            if str(values.get("rel") or "").casefold() == "canonical":
                self.canonical = values.get("href")
        elif tag.casefold() == "a":
            href = str(values.get("href") or "").casefold()
            self.has_phone = self.has_phone or href.startswith("tel:")
            self.has_email = self.has_email or href.startswith("mailto:")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False
        elif tag.casefold() == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)


def _text(parts: list[str]) -> str | None:
    value = " ".join(" ".join(parts).split())
    return value or None


async def _public_target(url: str) -> tuple[httpx.URL, str]:
    target = httpx.URL(url)
    if target.scheme not in {"http", "https"} or not target.host or target.userinfo:
        raise ValueError("Посадочная должна быть HTTP(S) URL без учётных данных")
    host = target.raw_host.decode("ascii")
    addresses = await asyncio.get_running_loop().getaddrinfo(
        host, target.port or (443 if target.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not addresses:
        raise ValueError("DNS не вернул адресов")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if (not ip.is_global or ip.is_multicast
                or (mapped is not None and not mapped.is_global)):
            raise ValueError("Доступ к непубличным IP-адресам запрещён")
    return target, addresses[0][4][0]


async def _inspect(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    current = url
    redirects = []
    for hop in range(MAX_REDIRECTS + 1):
        target, address = await _public_target(current)
        # Connect to the validated address, preserving Host and TLS verification.
        # No second DNS lookup is allowed between validation and connection.
        async with _stream(client, target, address) as response:
            if response.has_redirect_location:
                if hop == MAX_REDIRECTS:
                    raise ValueError("Превышен лимит перенаправлений")
                current = str(target.join(response.headers["location"]))
                redirects.append({"url": str(target), "status_code": response.status_code,
                                  "location": current})
                continue
            raw = await _bounded_body(response)
            truncated = len(raw) > MAX_HTML_BYTES
            html = raw[:MAX_HTML_BYTES].decode(response.encoding or "utf-8", errors="replace")
            from . import product_markup
            parser = _PageParser()
            parser.feed(html)
            title, h1 = _text(parser.title_parts), _text(parser.h1_parts)
            error_marker = bool(re.search(
                r"(?:^|ошибка\s+|error\s+)404(?:\s|$)|page\s+not\s+found|"
                r"страниц[аы]\s+не\s+найден|страница\s+отсутствует",
                f"{title or ''} {h1 or ''}", re.I,
            ))
            return {
                "url": url,
                "final_url": str(target),
                "status_code": response.status_code,
                "ok": 200 <= response.status_code < 300 and not error_marker,
                "soft_404": 200 <= response.status_code < 300 and error_marker,
                "redirects": redirects,
                "title": title,
                "h1": h1,
                "meta_description": parser.meta_description,
                "canonical": parser.canonical,
                "viewport": parser.viewport,
                "counter_ids": sorted({int(value) for value in re.findall(
                    r"(?:ym\s*\(|mc\.yandex\.ru/watch/)(\d+)", html
                )}),
                "conversion_actions": {
                    "form": parser.has_form, "phone": parser.has_phone, "email": parser.has_email,
                },
                "html_truncated": truncated,
                "product_markup": product_markup.present(html),
            }
    raise ValueError("Превышен лимит перенаправлений")


async def inspect_pages(
    pages: list[dict[str, Any]], *, timeout_seconds: float = 12,
    max_pages: int = MAX_PAGES, user_agent: str = DESKTOP_USER_AGENT,
) -> list[dict[str, Any]]:
    unique = list(dict.fromkeys(str(row["url"]) for row in pages if row.get("url")))[:max_pages]
    slots = asyncio.Semaphore(MAX_CONCURRENT)
    async with httpx.AsyncClient(
        follow_redirects=False, trust_env=False, timeout=timeout_seconds,
        # An IP can host several TLS origins; never reuse a connection across them.
        limits=httpx.Limits(max_connections=MAX_CONCURRENT, max_keepalive_connections=0),
        headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
    ) as client:
        async def run(url: str) -> dict[str, Any]:
            key = (CACHE_SCOPE.get(), url, user_agent)
            cached = _CACHE.get(key) if key[0] else None
            if cached and cached[0] > time.monotonic():
                return {**deepcopy(cached[1]), "cache_hit": True}
            try:
                async with slots:
                    async with asyncio.timeout(max(timeout_seconds, BATCH_TIMEOUT_SECONDS)):
                        result = await _inspect(client, url)
                if key[0] and result.get("ok") and not result.get("html_truncated"):
                    if len(_CACHE) >= 2000:
                        _CACHE.pop(next(iter(_CACHE)))
                    _CACHE[key] = (time.monotonic() + CACHE_TTL, deepcopy(result))
                return result
            except RetryLater as exc:
                return {
                    "url": url,
                    "ok": False,
                    "checked": False,
                    "retry_after_seconds": exc.seconds,
                    "error": str(exc),
                }
            except TimeoutError:
                return {"url": url, "ok": False, "error": "Истёк лимит времени проверки"}
            except (
                httpx.HTTPError,
                httpx.InvalidURL,
                ValueError,
                OSError,
                LookupError,
                zlib.error,
            ) as exc:
                return {"url": url, "ok": False, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - isolate malformed HTML per page
                log.exception("Ошибка разбора посадочной страницы")
                return {"url": url, "ok": False, "error": str(exc)}
        return list(await asyncio.gather(*(run(url) for url in unique)))
