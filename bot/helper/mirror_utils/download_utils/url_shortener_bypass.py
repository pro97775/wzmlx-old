#!/usr/bin/env python3
"""Resolve shortener links to their target URL before the normal host handling."""
from re import search
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests

from bot.helper.ext_utils.exceptions import DirectDownloadLinkException

_OUO_DOMAINS = ("ouo.io", "ouo.press")

_CSRF_PATTERN = (
    r'<input[^>]*\bname="_token"[^>]*\bvalue="([^"]*)"'
    r'|<input[^>]*\bvalue="([^"]*)"[^>]*\bname="_token"'
)


def is_url_shortener(domain):
    return any(d in domain for d in _OUO_DOMAINS) if domain else False


def bypass_shortener(link):
    domain = urlparse(link).hostname or ""
    if any(d in domain for d in _OUO_DOMAINS):
        return _ouo(link)
    raise DirectDownloadLinkException(f"ERROR: No bypasser for {domain}")


def _extract_csrf(html):
    match = search(_CSRF_PATTERN, html)
    return next((g for g in match.groups() if g), "") if match else ""


def _ouo(link):
    normalized = link.replace("ouo.press", "ouo.io")
    parsed = urlparse(normalized)
    short_id = parsed.path.rsplit("/", 1)[-1]
    if not short_id:
        raise DirectDownloadLinkException("ERROR: ouo: empty id segment")

    base = f"{parsed.scheme}://{parsed.netloc}"
    go_url = f"{base}/go/{short_id}"
    final_url = f"{base}/xreallcygo/{short_id}"

    try:
        with cffi_requests.Session(impersonate="chrome136", timeout=30) as session:
            landing = session.get(normalized, allow_redirects=True)
            if landing.status_code == 403:
                raise DirectDownloadLinkException("ERROR: ouo.io blocked the request (403)")
            token = _extract_csrf(landing.text)
            if not token:
                raise DirectDownloadLinkException(
                    f"ERROR: ouo: _token not found on initial page (status={landing.status_code})")

            step = session.post(
                go_url,
                data={"_token": token, "x-token": "", "v-token": "vm"},
                headers={"Origin": "https://ouo.io", "Referer": normalized},
                allow_redirects=False,
            )
            if step.status_code == 403:
                raise DirectDownloadLinkException("ERROR: ouo.io blocked the request (403)")
            if step.status_code != 200:
                raise DirectDownloadLinkException(f"ERROR: ouo: /go/ status {step.status_code}")
            token = _extract_csrf(step.text)
            if not token:
                raise DirectDownloadLinkException("ERROR: ouo: _token not found on /go/ page")

            final = session.post(
                final_url,
                data={"_token": token, "x-token": ""},
                headers={"Origin": "https://ouo.io", "Referer": go_url},
                allow_redirects=False,
            )
            location = final.headers.get("Location", "")
            if final.status_code != 302 or not location:
                raise DirectDownloadLinkException(
                    f"ERROR: ouo: /xreallcygo/ status {final.status_code} (location={location!r})")
            return location
    except DirectDownloadLinkException:
        raise
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: ouo bypass failed: {e}") from e
