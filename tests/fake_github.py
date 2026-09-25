"""Offline stand-in for api.github.com, built on httpx2.MockTransport."""

import os
import unittest
from unittest import mock

import httpx2

import github_client as gh


class FakeGitHub:
    """Serves canned responses per URL path and records every request.

    route(path, *responses): each response is (status, body) or
    (status, body, headers), or an exception instance to raise. Responses are
    served in order and the last one repeats. Unrouted paths get GitHub's 404.
    """

    def __init__(self):
        self.routes = {}
        self.requests = []

    def route(self, path, *responses):
        self.routes[path] = list(responses)

    def handler(self, request):
        self.requests.append(request)
        queue = self.routes.get(request.url.path)
        if not queue:
            return httpx2.Response(404, json={"message": "Not Found"})
        spec = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(spec, Exception):
            raise spec
        status, body, *rest = spec
        headers = rest[0] if rest else None
        if isinstance(body, (dict, list)):
            return httpx2.Response(status, json=body, headers=headers)
        if isinstance(body, str):
            return httpx2.Response(status, text=body, headers=headers)
        return httpx2.Response(status, headers=headers)


class GitHubTestCase(unittest.IsolatedAsyncioTestCase):
    """Routes github_client through FakeGitHub, with no token and no retry delay."""

    def setUp(self):
        self.github = FakeGitHub()
        self._patch(mock.patch.object(gh, "transport", httpx2.MockTransport(self.github.handler)))
        self._patch(mock.patch.object(gh, "STATS_RETRY_DELAY", 0))
        self._patch(mock.patch.dict(os.environ))  # restores the environment afterwards
        os.environ.pop("GITHUB_TOKEN", None)

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)


def repo_json(full_name, stars, **overrides):
    """A trimmed-down GitHub repository payload."""
    owner, name = full_name.split("/")
    data = {
        "full_name": full_name,
        "description": f"The {name} project.",
        "html_url": f"https://github.com/{full_name}",
        "stargazers_count": stars,
        "forks_count": stars // 5,
        "language": "Python",
        "topics": ["python"],
        "archived": False,
        "pushed_at": "2026-09-01T12:00:00Z",
        "open_issues_count": 7,
        "subscribers_count": 250,
        "license": {"key": "mit", "name": "MIT License", "spdx_id": "MIT"},
        "default_branch": "main",
        "fork": False,
        "created_at": "2018-12-08T08:00:00Z",
        "homepage": "",
        "owner": {"login": owner},
    }
    data.update(overrides)
    return data
