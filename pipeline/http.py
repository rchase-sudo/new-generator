"""Shared HTTP helpers with retries and a clear user agent."""
import time
import requests

class ClientError(RuntimeError):
    pass


UA = {"User-Agent": "FormaPipeline/0.1 (forma.sitedesign@gmail.com)"}


def get(url, params=None, timeout=60, tries=3, **kw):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers={**UA, **kw.pop("headers", {})}, **kw)
            if r.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last


def post(url, data=None, json=None, timeout=90, tries=3, headers=None):
    last = None
    for i in range(tries):
        try:
            r = requests.post(url, data=data, json=json, timeout=timeout, headers={**UA, **(headers or {})})
            if r.status_code in (429, 502, 503, 504, 529):
                raise requests.HTTPError(f"{r.status_code} from {url}")
            if 400 <= r.status_code < 500:   # a client error will not fix itself: fail now, with the reason
                raise ClientError(f"{r.status_code} from {url}: {r.text[:600]}")
            r.raise_for_status()
            return r
        except ClientError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise last
