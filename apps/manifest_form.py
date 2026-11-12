"""Print an HTML page whose button registers a GitHub App from a manifest.

Why: GitHub registers an App from a manifest only through a browser POST by a
signed-in organisation owner, with the manifest in a form field named
`manifest` (GitHub docs, "Registering a GitHub App from a manifest"). A link
cannot POST, so this writes a one-button page to open locally. Nothing is
sent anywhere until the owner presses the button.

    python3 apps/manifest_form.py apps/agent-app.manifest.json > "$TMPDIR/agent-app.html"
    open "$TMPDIR/agent-app.html"

The `state` value it prints on stderr comes back in the redirect's address;
compare the two before converting the code.
"""

from __future__ import annotations

import html
import json
import secrets
import sys
from pathlib import Path

ORG = "platform-factory"


def page(manifest: dict, state: str, org: str = ORG) -> str:
    action = f"https://github.com/organizations/{org}/settings/apps/new?state={state}"
    value = html.escape(json.dumps(manifest, separators=(",", ":")), quote=True)
    perms = ", ".join(f"{k}: {v}" for k, v in sorted(manifest.get("default_permissions", {}).items()))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Register {html.escape(manifest['name'])}</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; line-height: 1.5">
<h1>Register {html.escape(manifest['name'])} for {html.escape(org)}</h1>
<p>Permissions requested: {html.escape(perms)}. Webhook: inactive. Events: none.</p>
<p>State: <code>{state}</code> (it must match the <code>state</code> in the address GitHub redirects to).</p>
<form action="{html.escape(action, quote=True)}" method="post">
<input type="hidden" name="manifest" value="{value}">
<button type="submit">Create GitHub App for {html.escape(org)}</button>
</form>
</body></html>
"""


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: manifest_form.py <manifest.json>", file=sys.stderr)
        return 2
    manifest = json.loads(Path(argv[0]).read_text())
    state = secrets.token_hex(16)
    sys.stdout.write(page(manifest, state))
    print(f"state: {state}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
