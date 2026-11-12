"""The App manifests hold exactly ADR-0024 §7's permissions, and the form posts them."""

import html
import json
import re
import unittest

from apps import manifest_form
from tests import fakes

APPS = fakes.ROOT / "apps"
# ADR-0024 §7, verbatim in substance: the agent App "contents write, pull_requests write,
# metadata read; no workflows, no administration, no checks, no statuses, no issues"; the
# ratchet App "administration write and pull_requests write".
ADR_0024_S7 = {
    "agent-app.manifest.json": {"contents": "write", "pull_requests": "write", "metadata": "read"},
    "ratchet-app.manifest.json": {"administration": "write", "pull_requests": "write"},
}
NEVER_AGENT = ("workflows", "administration", "checks", "statuses", "issues", "actions_variables", "secrets")


class ManifestTest(unittest.TestCase):
    def load(self, name):
        return json.loads((APPS / name).read_text())

    def test_permissions_are_exactly_adr_0024_s7(self):
        for name, perms in ADR_0024_S7.items():
            self.assertEqual(self.load(name)["default_permissions"], perms, name)
        agent = self.load("agent-app.manifest.json")["default_permissions"]
        for p in NEVER_AGENT:
            self.assertNotIn(p, agent)

    def test_no_webhook_no_events_private_and_names_fit(self):
        for name in ADR_0024_S7:
            m = self.load(name)
            self.assertFalse(m["hook_attributes"]["active"], name)
            self.assertEqual(m["default_events"], [], name)
            self.assertFalse(m["public"], name)
            self.assertLessEqual(len(m["name"]), 34, name)   # GitHub's App name limit
            self.assertTrue(m["redirect_url"].startswith("https://github.com/platform-factory/factory-controls/"))
            self.assertEqual(set(m) - {"name", "url", "description", "public", "hook_attributes", "redirect_url",
                                       "default_permissions", "default_events"}, set())

    def test_agent_slug_is_the_class_files(self):
        self.assertEqual(self.load("agent-app.manifest.json")["name"], fakes.load_cf()["agent"]["app"])

    def test_form_posts_the_manifest_to_the_org_with_a_state(self):
        m = self.load("agent-app.manifest.json")
        page = manifest_form.page(m, "abc123")
        self.assertIn('action="https://github.com/organizations/platform-factory/settings/apps/new?state=abc123"', page)
        value = re.search(r'name="manifest" value="([^"]*)"', page).group(1)
        self.assertEqual(json.loads(html.unescape(value)), m)
        self.assertIn('method="post"', page)


if __name__ == "__main__":
    unittest.main()
