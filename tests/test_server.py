"""End-to-end tests through the MCP SDK's in-memory Client.

These exercise the real protocol path: argument validation against the
generated schema, tool dispatch, result wrapping and error reporting.
"""

import unittest

from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

import github_client as gh
import server
from fake_github import GitHubTestCase, repo_json

EXPECTED_TOOLS = {
    "search_repositories",
    "get_repo_details",
    "get_readme",
    "get_contributors",
    "get_commit_activity",
    "compare_repos",
}


class ToolErrorAdapterTests(unittest.TestCase):
    def test_github_errors_become_tool_errors_with_the_same_message(self):
        with self.assertRaises(ToolError) as ctx:
            with server._as_tool_error():
                raise gh.GitHubError("Repository a/b was not found.")
        self.assertEqual(str(ctx.exception), "Repository a/b was not found.")

    def test_unexpected_exceptions_are_not_disguised(self):
        with self.assertRaises(KeyError):
            with server._as_tool_error():
                raise KeyError("a bug")


class ServerTests(GitHubTestCase):
    async def test_every_tool_is_registered_with_a_description(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertEqual(set(tools), EXPECTED_TOOLS)
        for tool in tools.values():
            self.assertTrue(tool.description, f"{tool.name} has no description")

    async def test_tool_call_returns_structured_content(self):
        self.github.route("/repos/pallets/flask", (200, repo_json("pallets/flask", 69000)))
        async with Client(server.mcp, raise_exceptions=True) as client:
            result = await client.call_tool("get_repo_details", {"owner": "pallets", "repo": "flask"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["full_name"], "pallets/flask")
        self.assertEqual(result.structured_content["stars"], 69000)

    async def test_text_tool_returns_the_readme(self):
        self.github.route("/repos/pallets/flask/readme", (200, "# Flask\n"))
        async with Client(server.mcp, raise_exceptions=True) as client:
            result = await client.call_tool("get_readme", {"owner": "pallets", "repo": "flask"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.content[0].text, "# Flask\n")

    async def test_github_failures_reach_the_model_as_readable_tool_errors(self):
        async with Client(server.mcp, raise_exceptions=True) as client:
            result = await client.call_tool("get_repo_details", {"owner": "nobody", "repo": "nothing"})
        self.assertTrue(result.is_error)
        self.assertIn("nobody/nothing was not found", result.content[0].text)

    async def test_every_tool_succeeds_through_the_protocol(self):
        flask = repo_json("pallets/flask", 69000)
        self.github.route("/search/repositories", (200, {"total_count": 1, "items": [flask]}))
        self.github.route("/repos/pallets/flask", (200, flask))
        self.github.route("/repos/pallets/flask/readme", (200, "# Flask\n"))
        self.github.route("/repos/pallets/flask/contributors", (200, [
            {"login": "davidism", "contributions": 1200, "html_url": "https://github.com/davidism"},
        ]))
        self.github.route("/repos/pallets/flask/stats/commit_activity", (200, [
            {"week": 1756598400, "total": 3, "days": [0, 1, 0, 1, 1, 0, 0]},
        ]))
        calls = {
            "search_repositories": {"query": "web framework", "language": "python", "sort": "forks", "limit": 5},
            "get_repo_details": {"owner": "pallets", "repo": "flask"},
            "get_readme": {"owner": "pallets", "repo": "flask", "max_chars": 1000},
            "get_contributors": {"owner": "pallets", "repo": "flask", "limit": 3},
            "get_commit_activity": {"owner": "pallets", "repo": "flask", "weeks": 4},
            "compare_repos": {"repos": ["pallets/flask"]},
        }
        self.assertEqual(set(calls), EXPECTED_TOOLS)
        async with Client(server.mcp, raise_exceptions=True) as client:
            for name, arguments in calls.items():
                with self.subTest(tool=name):
                    result = await client.call_tool(name, arguments)
                    self.assertFalse(result.is_error, result.content[0].text)
        search = self.github.requests[0].url.params
        self.assertEqual((search.get("sort"), search.get("per_page")), ("forks", "5"))

    async def test_out_of_range_arguments_are_rejected_before_github_is_called(self):
        async with Client(server.mcp, raise_exceptions=True) as client:
            result = await client.call_tool("search_repositories", {"query": "rag", "limit": 500})
        self.assertTrue(result.is_error)
        self.assertEqual(self.github.requests, [])


if __name__ == "__main__":
    unittest.main()
