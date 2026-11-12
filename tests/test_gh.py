import json
import unittest

from factory.gh import ApiError, DryRunGitHub, GitHub


class Transport:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, method, url, headers, body):
        self.requests.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.answers.pop(0)


def ok(obj, headers=None, status=200):
    return status, headers or {}, json.dumps(obj).encode()


class GitHubTest(unittest.TestCase):
    def test_paginate_follows_link_and_unwraps_list_member(self):
        t = Transport([
            ok({"total_count": 3, "workflow_runs": [{"id": 1}, {"id": 2}]},
               {"link": '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=2>; rel="last"'}),
            ok({"total_count": 3, "workflow_runs": [{"id": 3}]}),
        ])
        gh = GitHub("tok", transport=t, sleep=lambda s: None)
        self.assertEqual([r["id"] for r in gh.paginate("repos/o/r/actions/runs", event="push")], [1, 2, 3])
        self.assertIn("per_page=100", t.requests[0]["url"])
        self.assertIn("event=push", t.requests[0]["url"])
        self.assertEqual(t.requests[1]["url"], "https://api.github.com/x?page=2")
        self.assertEqual(t.requests[0]["headers"]["Authorization"], "Bearer tok")

    def test_graphql_sends_a_preview_media_type_when_asked(self):
        t = Transport([ok({"data": {"x": 1}}), ok({"data": {"x": 2}})])
        gh = GitHub("tok", transport=t, sleep=lambda s: None)
        self.assertEqual(gh.graphql("query { x }", {}), {"x": 1})
        self.assertEqual(gh.graphql("query { x }", {}, accept="application/vnd.github.merge-info-preview+json"),
                         {"x": 2})
        self.assertEqual(t.requests[0]["headers"]["Accept"], "application/vnd.github+json")
        self.assertEqual(t.requests[1]["headers"]["Accept"], "application/vnd.github.merge-info-preview+json")
        self.assertEqual(json.loads(t.requests[1]["body"]), {"query": "query { x }", "variables": {}})

    def test_retries_5xx_on_get_then_succeeds(self):
        slept = []
        t = Transport([(502, {}, b"bad gateway"), ok({"a": 1})])
        gh = GitHub("tok", transport=t, sleep=slept.append)
        self.assertEqual(gh.get("x"), {"a": 1})
        self.assertEqual(len(slept), 1)

    def test_post_is_not_retried_on_5xx(self):
        t = Transport([(502, {}, b"bad gateway"), ok({"a": 1})])
        gh = GitHub("tok", transport=t, sleep=lambda s: None)
        with self.assertRaises(ApiError) as cm:
            gh.post("repos/o/r/issues", {"title": "t"})
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(len(t.requests), 1)

    def test_rate_limit_is_retried_with_retry_after(self):
        slept = []
        t = Transport([(403, {"retry-after": "7"}, b'{"message":"secondary rate limit"}'), ok({"a": 1})])
        gh = GitHub("tok", transport=t, sleep=slept.append)
        self.assertEqual(gh.post("x", {}), {"a": 1})
        self.assertEqual(slept, [7.0])

    def test_405_raises_api_error_with_status_and_message(self):
        t = Transport([(405, {}, b'{"message":"Pull Request is not mergeable"}')])
        gh = GitHub("tok", transport=t, sleep=lambda s: None)
        with self.assertRaises(ApiError) as cm:
            gh.put("repos/o/r/pulls/1/merge", {"merge_method": "squash"})
        self.assertEqual((cm.exception.status, cm.exception.message), (405, "Pull Request is not mergeable"))
        self.assertEqual(json.loads(t.requests[0]["body"]), {"merge_method": "squash"})

    def test_graphql_errors_raise(self):
        t = Transport([ok({"data": None, "errors": [{"message": "nope"}]})])
        with self.assertRaises(ApiError):
            GitHub("tok", transport=t).graphql("query { viewer { login } }", {})

    def test_get_raw_follows_redirect_without_authorization(self):
        t = Transport([(302, {"location": "https://storage.example/zip"}, b""), (200, {}, b"PK\x03\x04")])
        gh = GitHub("tok", transport=t)
        self.assertEqual(gh.get_raw("repos/o/r/actions/artifacts/1/zip"), b"PK\x03\x04")
        self.assertNotIn("Authorization", t.requests[1]["headers"])

    def test_dry_run_sends_reads_and_records_writes(self):
        t = Transport([ok({"a": 1})])
        dry = DryRunGitHub(GitHub("tok", transport=t))
        self.assertEqual(dry.get("x"), {"a": 1})
        dry.put("repos/o/r/rulesets/1", {"b": 2})
        self.assertEqual(len(t.requests), 1)
        self.assertEqual(dry.writes, [{"method": "PUT", "path": "repos/o/r/rulesets/1", "body": {"b": 2}}])


if __name__ == "__main__":
    unittest.main()
