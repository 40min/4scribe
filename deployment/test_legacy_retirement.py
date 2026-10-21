#!/usr/bin/env python3
"""Stdlib checks for the bounded legacy retirement guard."""

import copy
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
GUARD = HERE / "legacy_serve_guard.py"
SCRIPT = HERE / "retire-legacy-stt.sh"

EXPECTED_TAGS = [
    "parakeet-stt:0.2.0-a12b237",
    "parakeet-stt:latest",
    "parakeet-stt:0.2.0-b809bbb",
    "nemo-speech-runtime-cpu:0.2.0-a12b237",
    "nemo-speech-runtime-cpu:latest",
    "nemo-speech-runtime-cpu:0.2.0-b809bbb",
    "parakeet-test:latest",
    "parakeet-test:rollback-2026-10-14",
    "parakeet-test:pre-mobile-20261007",
]

BASE_HANDLERS = {
    "/": {"Proxy": "http://127.0.0.1:18789"},
    "/4scribe": {"Proxy": "http://127.0.0.1:8093"},
    "/parakeet-test": {"Proxy": "http://127.0.0.1:8091"},
    "/v1/audio/transcriptions/realtime": {
        "Proxy": "http://100.72.25.9:8090/v1/audio/transcriptions/realtime"
    },
    # Deliberately unrelated pre-existing handler: must be preserved, not
    # treated as an error merely because it is not / or /4scribe.
    "/unrelated": {"Proxy": "http://127.0.0.1:19999"},
}


def serve_doc(handlers):
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            "40zen.tail055a00.ts.net:443": {
                "Handlers": copy.deepcopy(handlers),
            }
        },
    }


def post_doc():
    handlers = copy.deepcopy(BASE_HANDLERS)
    del handlers["/parakeet-test"]
    del handlers["/v1/audio/transcriptions/realtime"]
    return serve_doc(handlers)


class ServeGuardTests(unittest.TestCase):
    def run_guard(self, *args):
        return subprocess.run(
            [sys.executable, str(GUARD), *args],
            text=True,
            capture_output=True,
        )

    def write_json(self, directory, name, doc):
        path = pathlib.Path(directory) / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_pre_accepts_object_handlers_and_preserves_unrelated(self):
        with tempfile.TemporaryDirectory() as tmp:
            pre = self.write_json(tmp, "pre.json", serve_doc(BASE_HANDLERS))
            result = self.run_guard("pre", str(pre))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pre_rejects_string_handler_shape(self):
        handlers = copy.deepcopy(BASE_HANDLERS)
        handlers["/4scribe"] = "http://127.0.0.1:8093"
        with tempfile.TemporaryDirectory() as tmp:
            pre = self.write_json(tmp, "pre.json", serve_doc(handlers))
            result = self.run_guard("pre", str(pre))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an object", result.stderr)

    def test_post_accepts_exact_two_path_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            pre = self.write_json(tmp, "pre.json", serve_doc(BASE_HANDLERS))
            post = self.write_json(tmp, "post.json", post_doc())
            result = self.run_guard("post", str(pre), str(post))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_post_rejects_removal_of_unrelated_handler(self):
        post = post_doc()
        del post["Web"]["40zen.tail055a00.ts.net:443"]["Handlers"]["/unrelated"]
        with tempfile.TemporaryDirectory() as tmp:
            pre = self.write_json(tmp, "pre.json", serve_doc(BASE_HANDLERS))
            post_path = self.write_json(tmp, "post.json", post)
            result = self.run_guard("post", str(pre), str(post_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("beyond the two legacy paths", result.stderr)

    def test_post_rejects_added_unrelated_handler(self):
        post = post_doc()
        post["Web"]["40zen.tail055a00.ts.net:443"]["Handlers"]["/new"] = {
            "Proxy": "http://127.0.0.1:20000"
        }
        with tempfile.TemporaryDirectory() as tmp:
            pre = self.write_json(tmp, "pre.json", serve_doc(BASE_HANDLERS))
            post_path = self.write_json(tmp, "post.json", post)
            result = self.run_guard("post", str(pre), str(post_path))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("beyond the two legacy paths", result.stderr)

    def test_post_rejects_remaining_legacy_path(self):
        handlers = copy.deepcopy(BASE_HANDLERS)
        del handlers["/v1/audio/transcriptions/realtime"]
        with tempfile.TemporaryDirectory() as tmp:
            pre = self.write_json(tmp, "pre.json", serve_doc(BASE_HANDLERS))
            post = self.write_json(tmp, "post.json", serve_doc(handlers))
            result = self.run_guard("post", str(pre), str(post))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still present", result.stderr)


class ScriptInventoryTests(unittest.TestCase):
    def test_exact_image_tag_list(self):
        text = SCRIPT.read_text(encoding="utf-8")
        match = re.search(
            r"readonly LEGACY_IMAGE_TAGS=\((.*?)\n\)", text, re.DOTALL
        )
        self.assertIsNotNone(match)
        tags = re.findall(r"^\s*'([^']+)'\s*$", match.group(1), re.MULTILINE)
        self.assertEqual(tags, EXPECTED_TAGS)

    def test_plan_reference_is_docs_path(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("docs/legacy-retirement-plan.md", text)
        self.assertNotIn("deployment/legacy-retirement-plan.md", text)


if __name__ == "__main__":
    unittest.main()
