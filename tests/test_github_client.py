import os
import unittest
from datetime import datetime, timezone

import httpx2

import github_client as gh
from fake_github import GitHubTestCase, repo_json


class NameParsingTests(unittest.TestCase):
    def test_validate_name_accepts_github_names(self):
        self.assertEqual(gh.validate_name("pallets", "owner"), "pallets")
        self.assertEqual(gh.validate_name("  my-repo_1.0 ", "repository"), "my-repo_1.0")

    def test_validate_name_rejects_traversal_and_junk(self):
        for bad in ["", ".", "..", "../etc", "a/b", "has space", "x" * 101, "semi;colon"]:
            with self.subTest(name=bad), self.assertRaises(gh.GitHubError):
                gh.validate_name(bad, "owner")

    def test_parse_full_name_accepts_common_forms(self):
        for text in [
            "pallets/flask",
            " pallets/flask ",
            "pallets/flask.git",
            "github.com/pallets/flask",
            "https://github.com/pallets/flask",
            "http://www.github.com/pallets/flask/",
            "https://github.com/pallets/flask/tree/main/src",
        ]:
            with self.subTest(text=text):
                self.assertEqual(gh.parse_full_name(text), ("pallets", "flask"))

    def test_parse_full_name_rejects_non_repositories(self):
        for bad in ["flask", "a/b/c", "https://gitlab.com/a/b", "github.com/only-owner", "../../x"]:
            with self.subTest(text=bad), self.assertRaises(gh.GitHubError):
                gh.parse_full_name(bad)

    def test_sort_options_match_the_literal(self):
        self.assertEqual(gh.SORT_OPTIONS, ("stars", "forks", "updated", "best-match"))


class RequestTests(GitHubTestCase):
    async def test_sends_api_headers_and_no_token_by_default(self):
        self.github.route("/repos/pallets/flask", (200, repo_json("pallets/flask", 100)))
        await gh.get_repo_details("pallets", "flask")
        headers = self.github.requests[0].headers
        self.assertEqual(headers.get("accept"), "application/vnd.github+json")
        self.assertEqual(headers.get("x-github-api-version"), gh.API_VERSION)
        self.assertEqual(headers.get("user-agent"), gh.USER_AGENT)
        self.assertNotIn("authorization", headers)

    async def test_sends_bearer_token_when_set(self):
        os.environ["GITHUB_TOKEN"] = "ghp_test123"
        self.github.route("/repos/pallets/flask", (200, repo_json("pallets/flask", 100)))
        await gh.get_repo_details("pallets", "flask")
        self.assertEqual(self.github.requests[0].headers.get("authorization"), "Bearer ghp_test123")

    async def test_404_gives_a_specific_message(self):
        with self.assertRaisesRegex(gh.GitHubError, "nobody/nothing was not found"):
            await gh.get_repo_details("nobody", "nothing")

    async def test_primary_rate_limit_mentions_reset_time_and_token(self):
        headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1767225600"}
        self.github.route("/repos/pallets/flask", (403, {"message": "API rate limit exceeded"}, headers))
        with self.assertRaises(gh.GitHubError) as ctx:
            await gh.get_repo_details("pallets", "flask")
        message = str(ctx.exception)
        self.assertIn("rate limit", message)
        self.assertIn("resets at 00:00 UTC", message)  # 1767225600 = 2026-01-01T00:00:00Z
        self.assertIn("GITHUB_TOKEN", message)

    async def test_rate_limit_message_omits_token_hint_when_token_is_set(self):
        os.environ["GITHUB_TOKEN"] = "ghp_test123"
        self.github.route("/repos/pallets/flask", (429, {"message": "slow down"}, {"retry-after": "30"}))
        with self.assertRaises(gh.GitHubError) as ctx:
            await gh.get_repo_details("pallets", "flask")
        self.assertIn("Retry in 30 seconds", str(ctx.exception))
        self.assertNotIn("GITHUB_TOKEN", str(ctx.exception))

    async def test_secondary_rate_limit_is_detected_from_retry_after(self):
        self.github.route("/repos/pallets/flask", (403, {"message": "secondary"}, {"retry-after": "60"}))
        with self.assertRaisesRegex(gh.GitHubError, "Retry in 60 seconds"):
            await gh.get_repo_details("pallets", "flask")

    async def test_other_403_reports_githubs_own_message(self):
        self.github.route("/repos/pallets/flask", (403, {"message": "Resource not accessible"}))
        with self.assertRaisesRegex(gh.GitHubError, "HTTP 403.*Resource not accessible"):
            await gh.get_repo_details("pallets", "flask")

    async def test_non_json_error_body_is_reported(self):
        self.github.route("/repos/pallets/flask", (502, "Bad gateway from upstream"))
        with self.assertRaisesRegex(gh.GitHubError, "HTTP 502.*Bad gateway from upstream"):
            await gh.get_repo_details("pallets", "flask")

    async def test_401_explains_the_token_problem(self):
        self.github.route("/repos/pallets/flask", (401, {"message": "Bad credentials"}))
        with self.assertRaisesRegex(gh.GitHubError, "rejected the token"):
            await gh.get_repo_details("pallets", "flask")

    async def test_network_failure_becomes_github_error(self):
        self.github.route("/repos/pallets/flask", httpx2.ConnectError("connection refused"))
        with self.assertRaisesRegex(gh.GitHubError, "Could not reach GitHub"):
            await gh.get_repo_details("pallets", "flask")

    async def test_timeout_becomes_github_error(self):
        self.github.route("/repos/pallets/flask", httpx2.ReadTimeout("timed out"))
        with self.assertRaisesRegex(gh.GitHubError, "did not respond in time"):
            await gh.get_repo_details("pallets", "flask")


class SearchTests(GitHubTestCase):
    async def test_builds_query_sort_and_clamps_limit(self):
        self.github.route("/search/repositories", (200, {
            "total_count": 1234,
            "incomplete_results": False,
            "items": [repo_json("run-llama/llama_index", 40000)],
        }))
        result = await gh.search_repositories("  rag ", language="python", limit=100)
        params = self.github.requests[0].url.params
        self.assertEqual(params.get("q"), "rag language:python")
        self.assertEqual(params.get("sort"), "stars")
        self.assertEqual(params.get("order"), "desc")
        self.assertEqual(params.get("per_page"), str(gh.MAX_SEARCH_RESULTS))
        self.assertEqual(result["total_count"], 1234)
        self.assertEqual(result["returned"], 1)
        first = result["repositories"][0]
        self.assertEqual(first["full_name"], "run-llama/llama_index")
        self.assertEqual(first["stars"], 40000)
        self.assertNotIn("note", result)

    async def test_language_with_spaces_is_quoted(self):
        self.github.route("/search/repositories", (200, {"total_count": 0, "items": []}))
        await gh.search_repositories("notebooks", language="Jupyter Notebook")
        self.assertEqual(self.github.requests[0].url.params.get("q"), 'notebooks language:"Jupyter Notebook"')

    async def test_best_match_uses_githubs_relevance_order(self):
        self.github.route("/search/repositories", (200, {"total_count": 0, "items": []}))
        await gh.search_repositories("mcp server", sort="best-match")
        params = self.github.requests[0].url.params
        self.assertNotIn("sort", params)
        self.assertNotIn("order", params)

    async def test_incomplete_results_are_flagged(self):
        self.github.route("/search/repositories", (200, {"total_count": 5, "incomplete_results": True, "items": []}))
        result = await gh.search_repositories("anything")
        self.assertIn("incomplete", result["note"])

    async def test_bad_input_is_rejected_without_calling_github(self):
        with self.assertRaises(gh.GitHubError):
            await gh.search_repositories("   ")
        with self.assertRaises(gh.GitHubError):
            await gh.search_repositories("rag", sort="popularity")
        self.assertEqual(self.github.requests, [])


class RepositoryTests(GitHubTestCase):
    async def test_repo_details_fields(self):
        self.github.route("/repos/pallets/flask", (200, repo_json("pallets/flask", 69000, homepage="")))
        details = await gh.get_repo_details("pallets", "flask")
        self.assertEqual(details["full_name"], "pallets/flask")
        self.assertEqual(details["stars"], 69000)
        self.assertEqual(details["open_issues_and_prs"], 7)
        self.assertEqual(details["watchers"], 250)
        self.assertEqual(details["license"], "MIT License")
        self.assertIsNone(details["homepage"])  # empty string normalised to None
        self.assertFalse(details["is_fork"])

    async def test_repo_without_license(self):
        self.github.route("/repos/a/b", (200, repo_json("a/b", 1, license=None)))
        self.assertIsNone((await gh.get_repo_details("a", "b"))["license"])

    async def test_invalid_names_never_reach_the_network(self):
        with self.assertRaises(gh.GitHubError):
            await gh.get_repo_details("..", "..")
        self.assertEqual(self.github.requests, [])

    async def test_readme_uses_raw_media_type_and_truncates(self):
        self.github.route("/repos/pallets/flask/readme", (200, "x" * 1000))
        text = await gh.get_readme("pallets", "flask", max_chars=600)
        self.assertEqual(self.github.requests[0].headers.get("accept"), "application/vnd.github.raw+json")
        self.assertTrue(text.startswith("x" * 600))
        self.assertIn("showing 600 of 1,000 characters", text)

    async def test_short_readme_is_returned_whole(self):
        self.github.route("/repos/pallets/flask/readme", (200, "# Flask\n"))
        self.assertEqual(await gh.get_readme("pallets", "flask"), "# Flask\n")

    async def test_missing_readme(self):
        with self.assertRaisesRegex(gh.GitHubError, "has no README"):
            await gh.get_readme("pallets", "flask")

    async def test_contributors_are_mapped_and_limit_is_clamped(self):
        self.github.route("/repos/pallets/flask/contributors", (200, [
            {"login": "davidism", "contributions": 1200, "html_url": "https://github.com/davidism"},
            {"login": "mitsuhiko", "contributions": 900, "html_url": "https://github.com/mitsuhiko"},
        ]))
        result = await gh.get_contributors("pallets", "flask", limit=500)
        self.assertEqual(self.github.requests[0].url.params.get("per_page"), str(gh.MAX_CONTRIBUTORS))
        self.assertEqual(result["contributors"][0], {
            "login": "davidism", "commits": 1200, "profile": "https://github.com/davidism",
        })

    async def test_contributors_of_empty_repository(self):
        self.github.route("/repos/a/empty/contributors", (204, None))
        result = await gh.get_contributors("a", "empty")
        self.assertEqual(result["contributors"], [])
        self.assertIn("empty", result["note"])


def _weeks(count=52, start=1756598400):
    """GitHub commit_activity payload: one entry per week, oldest first."""
    return [{"week": start + i * 604800, "total": i % 4, "days": [0] * 7} for i in range(count)]


class CommitActivityTests(GitHubTestCase):
    PATH = "/repos/pallets/flask/stats/commit_activity"

    async def test_retries_while_github_computes_statistics(self):
        self.github.route(self.PATH, (202, {}), (202, {}), (200, _weeks()))
        result = await gh.get_commit_activity("pallets", "flask")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(self.github.requests), 3)

    async def test_gives_up_after_the_retry_budget(self):
        self.github.route(self.PATH, (202, {}))
        result = await gh.get_commit_activity("pallets", "flask")
        self.assertEqual(result["status"], "computing")
        self.assertEqual(len(self.github.requests), gh.STATS_RETRIES + 1)

    async def test_totals_and_recent_weeks(self):
        weeks = _weeks()
        self.github.route(self.PATH, (200, weeks))
        result = await gh.get_commit_activity("pallets", "flask", weeks=2)
        self.assertEqual(result["commits_last_52_weeks"], sum(w["total"] for w in weeks))
        self.assertEqual(len(result["recent_weeks"]), 2)
        last = weeks[-1]
        expected_date = datetime.fromtimestamp(last["week"], tz=timezone.utc).date().isoformat()
        self.assertEqual(result["recent_weeks"][-1], {"week_of": expected_date, "commits": last["total"]})

    async def test_empty_repository(self):
        self.github.route(self.PATH, (204, None))
        self.assertEqual((await gh.get_commit_activity("pallets", "flask"))["status"], "no_data")


class CompareTests(GitHubTestCase):
    async def test_sorts_by_stars_dedupes_and_reports_failures(self):
        self.github.route("/repos/pallets/flask", (200, repo_json("pallets/flask", 69000)))
        self.github.route("/repos/fastapi/fastapi", (200, repo_json("fastapi/fastapi", 90000)))
        result = await gh.compare_repos([
            "pallets/flask",
            "https://github.com/fastapi/fastapi",
            "PALLETS/FLASK",  # duplicate: names are case-insensitive
            "nobody/nothing",
        ])
        self.assertEqual([r["full_name"] for r in result["repositories"]],
                         ["fastapi/fastapi", "pallets/flask"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["repository"], "nobody/nothing")
        self.assertEqual(len(self.github.requests), 3)

    async def test_input_limits(self):
        with self.assertRaises(gh.GitHubError):
            await gh.compare_repos([])
        with self.assertRaises(gh.GitHubError):
            await gh.compare_repos([f"owner/repo{i}" for i in range(gh.MAX_COMPARE + 1)])
        with self.assertRaises(gh.GitHubError):
            await gh.compare_repos(["not-a-repository"])
        self.assertEqual(self.github.requests, [])


if __name__ == "__main__":
    unittest.main()
