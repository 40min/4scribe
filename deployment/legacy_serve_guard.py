#!/usr/bin/env python3
"""Bounded Tailscale Serve shape and preservation guard for legacy retirement.

Serve JSON stores each handler as an object, for example:
    "/4scribe": {"Proxy": "http://127.0.0.1:8093"}
This helper validates that shape, then proves that post-retirement config equals
the pre-retirement config with only the two legacy paths removed. Unrelated
handlers are preserved, not required to disappear.
"""

import copy
import json
import sys

LEGACY_PATHS = (
    "/parakeet-test",
    "/v1/audio/transcriptions/realtime",
)

EXPECTED_HANDLERS = {
    "/": "http://127.0.0.1:18789",
    "/4scribe": "http://127.0.0.1:8093",
    "/parakeet-test": "http://127.0.0.1:8091",
    "/v1/audio/transcriptions/realtime":
        "http://100.72.25.9:8090/v1/audio/transcriptions/realtime",
}

PRESERVED_HANDLERS = {
    "/": "http://127.0.0.1:18789",
    "/4scribe": "http://127.0.0.1:8093",
}


def _handlers(doc):
    web = doc.get("Web")
    if not isinstance(web, dict):
        raise SystemExit("Serve JSON has no Web object")
    hosts = [key for key in web if key.endswith(":443")]
    if len(hosts) != 1:
        raise SystemExit(f"expected exactly one 443 Serve host, got {hosts}")
    handlers = web[hosts[0]].get("Handlers")
    if not isinstance(handlers, dict):
        raise SystemExit(f"Serve {hosts[0]} has no Handlers object")
    return handlers


def _require_proxy(handlers, path, expected):
    handler = handlers.get(path)
    if not isinstance(handler, dict):
        raise SystemExit(f"Serve {path} handler is not an object: {handler!r}")
    proxy = handler.get("Proxy")
    if not isinstance(proxy, str):
        raise SystemExit(f"Serve {path} Proxy is not a string: {proxy!r}")
    if proxy != expected:
        raise SystemExit(f"Serve {path} target changed: {proxy!r}")


def check_pre(doc):
    if "443" not in doc.get("TCP", {}):
        raise SystemExit("Serve TCP 443 entry is absent")
    handlers = _handlers(doc)
    for path, target in EXPECTED_HANDLERS.items():
        _require_proxy(handlers, path, target)


def check_post(pre, post):
    check_pre(pre)

    post_handlers = _handlers(post)
    for path in LEGACY_PATHS:
        if path in post_handlers:
            raise SystemExit(f"legacy Serve path still present: {path}")
    for path, target in PRESERVED_HANDLERS.items():
        _require_proxy(post_handlers, path, target)

    expected = copy.deepcopy(pre)
    handlers = _handlers(expected)
    for path in LEGACY_PATHS:
        handlers.pop(path, None)
    if expected != post:
        raise SystemExit(
            "Tailscale Serve changed beyond the two legacy paths; "
            "unrelated handlers must be preserved"
        )


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv):
    if len(argv) == 3 and argv[1] == "pre":
        check_pre(_load(argv[2]))
        return
    if len(argv) == 4 and argv[1] == "post":
        check_post(_load(argv[2]), _load(argv[3]))
        return
    raise SystemExit(
        "usage: legacy_serve_guard.py pre <serve.json> | "
        "post <serve-pre.json> <serve-post.json>"
    )


if __name__ == "__main__":
    main(sys.argv)
