# The two GitHub Apps

M4 has two non-human principals, each an organisation-owned GitHub App
registered from a manifest in this directory (ADR-0024 §7). If GitHub sent
you here after you pressed "Create GitHub App", the `code` in your browser's
address bar is what step 3 below converts. Do it within the hour.

## Why two Apps, and why these permissions

- **`platform-factory-agent`** (`agent-app.manifest.json`) is the agent's
  identity on `svc-hello`: it opens pull requests, commits through GraphQL
  `createCommitOnBranch`, and asks the merge API to merge. It holds
  `contents: write`, `pull_requests: write` and `metadata: read`, and nothing
  else. Without `workflows` it cannot push a change under
  `.github/workflows/`; without `checks` or `statuses` it cannot write a check
  run or a commit status, so it cannot forge `class-scope` or `go-test`;
  without `administration` it cannot touch a ruleset; without `issues` it
  holds nothing it does not need. It can still label pull requests, review
  them and merge them, which is why the rulesets' team entries, the label
  rule (labels count only from the listed human's own events) and
  `class-scope` hold the boundary, not the App's permissions alone.
- **`platform-factory-ratchet`** (`ratchet-app.manifest.json`) is the
  demotion App: it raises `svc-hello`'s rung entry to one approval on an
  escape and labels open agent pull requests `needs-human`. It holds
  `administration: write` (no narrower permission edits a repository
  ruleset, and `GITHUB_TOKEN` has no `administration` key) and
  `pull_requests: write` (labels). `administration` also lets it lower an
  entry, edit `bypass_actors` or delete either ruleset (the rung ruleset or
  the wall ruleset, two since probe P1); "only raises" and "never the wall"
  are held by `factory/ratchet.py`, not by GitHub, and its key sits in the `ratchet`
  environment that only the detector and the merge step's `pre-merge` job use.

GitHub grants `metadata: read` to every App; the ratchet manifest does not
list it because ADR-0024 §7 does not, and the result is the same.

Neither manifest has a webhook (`hook_attributes.active: false`; the URL is a
required placeholder that is never called) or subscribes to any event.
Neither App is installed on this repository.

**An open question these manifests do not settle.** The detector and the merge
step read `svc-hello`'s workflow runs and jobs, check runs, file contents and
compares with the ratchet App's token, and the merge step reads check runs
with the agent App's. GitHub's permission data files those reads under
`actions: read`, `checks: read` and `contents: read`. GitHub's docs say Apps
have "implicit permissions to read public resources when acting on behalf of
a user" (choosing-permissions-for-a-github-app.md, read 2026-11-11), which
covers user access tokens, not the installation tokens used here; whether an
installation token without those permissions can read them on a public
repository is not documented. Probe P3 tries each read with each App's token
on the throwaway repository. If any is refused, the fix is a read-only
permission added to the manifest (the ratchet App: `actions: read`,
`checks: read`, `contents: read`; the agent App: `actions: read`,
`checks: read`), recorded as a dated correction before the class file's
first commit, with C-21's attempt list noting it. The manifests here are
exactly ADR-0024 §7 until then.

**What P3 answered (2026-11-15).** With the agent App's installation token
on `m4-probe`, each of these reads returned 200: workflow runs, check runs
on a commit, file contents, a compare, a pull request's reviews, its issue
events, the rulesets list and the artifacts list. So the agent App's
manifest stays as it is. The ratchet App holds no `contents` permission and
was not installed on `m4-probe`, so its reads of file contents
are still untried. A dry run with its token cannot be made: its key is kept
nowhere but the `ratchet` environment, and `detector.yml` has no dry-run
input. The first detector run after human step 8, which the delegate reads
before anything else (`docs/RUNBOOK.md`, step 8), makes two of them: the
branch read and `go.mod`'s contents. The detector's read of a commit and of
the class's files at it is made only for an escape (b), so it is first made
at injection (b). The detector makes no compare.

## Registering one (the owner, in the browser)

Human step 4 of `docs/RUNBOOK.md`. The owner account must be signed in to
github.com in the browser. Do the agent App first, then the ratchet App.

**0. The environments** (once, before either App; the delegate may run this).
Each environment holds one App's key and lets only workflows from `main` use
it, so a branch pushed by anyone with write cannot read the key:

```sh
R=platform-factory/factory-controls
for e in agent-app ratchet agent-model; do
  gh api -X PUT "repos/$R/environments/$e" --input - <<<'{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
  gh api -X POST "repos/$R/environments/$e/deployment-branch-policies" -f name=main -f type=branch
done
gh api "repos/$R/environments" --jq '.environments[] | {name, deployment_branch_policy}'
```

**1. The form.** From this repository's root:

```sh
python3 apps/manifest_form.py apps/agent-app.manifest.json > "$TMPDIR/agent-app.html"
open "$TMPDIR/agent-app.html"
```

The script prints `state: <hex>` on the terminal. The page shows the
permissions requested; press **Create GitHub App for platform-factory**.

**2. On GitHub.** GitHub shows the App's name (keep it as it is: the class
file's `agent.app` and every bot login depend on the slug) and asks you to
confirm. Press **Create GitHub App**. GitHub redirects to this README with
`?code=...&state=...` in the address bar. Check that `state` matches the
terminal's, then copy the `code`.

**3. Convert the code and store the key, without displaying it.** The
conversion returns the App's id and its private key (and a client secret and
webhook secret, which nothing uses and which are not kept). One pipeline
stores the key as an environment secret and the id as an environment
variable, and writes only the public fields to the evidence file:

```sh
R=platform-factory/factory-controls
code='<paste the code>'
resp=$(curl -fsS -X POST -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/app-manifests/$code/conversions") \
  || resp=$(gh api -X POST "app-manifests/$code/conversions")
printf '%s' "$resp" | jq -r .pem | gh secret set AGENT_APP_PRIVATE_KEY --env agent-app --repo "$R"
printf '%s' "$resp" | jq -r .client_id | gh variable set AGENT_APP_CLIENT_ID --env agent-app --repo "$R"
printf '%s' "$resp" | jq '{id, slug, client_id, owner: .owner.login, permissions, events, html_url}' \
  > "$EVIDENCE/07-apps-agent.json"
unset resp code
```

`$EVIDENCE` is the local evidence folder the build plan names, outside any
repository.

The docs show the conversion as an unauthenticated `POST`; whether it needs a
token was not tried, so the `gh api` fallback (the owner's credential) is
there. The `code` works once and expires after an hour.

For the ratchet App, repeat steps 1 to 3 with `apps/ratchet-app.manifest.json`,
the environment `ratchet`, the secret `RATCHET_APP_PRIVATE_KEY`, the variable
`RATCHET_APP_CLIENT_ID` and the evidence file `07-apps-ratchet.json`.

The workflows pass the App's Client ID, not its numeric id, to
`actions/create-github-app-token`: at the pinned v3.3.0 the `app-id` input
carries the deprecation message "Use 'client-id' instead." (its action.yml),
and `client-id` is the input it names. The Client ID is not a secret; it is
also in the evidence file.

**4. The read that proves it.** Each App's permissions, events and webhook
as GitHub has them, and the stored names (never values):

```sh
gh api apps/platform-factory-agent --jq '{slug, owner: .owner.login, permissions, events}'
# expect permissions {"contents":"write","metadata":"read","pull_requests":"write"} and events []
gh api apps/platform-factory-ratchet --jq '{slug, owner: .owner.login, permissions, events}'
# expect permissions {"administration":"write","metadata":"read","pull_requests":"write"} and events []
gh secret list --env agent-app --repo platform-factory/factory-controls
gh variable list --env agent-app --repo platform-factory/factory-controls
gh secret list --env ratchet --repo platform-factory/factory-controls
```

The webhook's state shows in the App's settings (organisation **Settings** →
**Developer settings** → **GitHub Apps** → the App → **General** →
**Webhook**: "Active" unticked).

**5. Install** (only where and when `docs/RUNBOOK.md` says: the agent App on
`m4-probe` at step 4; both Apps on `svc-hello` at step 8, after the L1
switch-on is recorded). Open `https://github.com/apps/platform-factory-agent/installations/new`
(or `.../platform-factory-ratchet/...`), choose **platform-factory**, choose
**Only select repositories**, pick the one repository, and press **Install**.
Never choose "All repositories". The read:

```sh
gh api orgs/platform-factory/installations \
  --jq '.installations[] | select(.app_slug | startswith("platform-factory-")) | {id, app_slug, repository_selection, permissions}'
gh api "user/installations/<installation id>/repositories" --jq '.repositories[].full_name'
```

## At the close

The owner suspends or uninstalls both Apps (the App's settings →
**Advanced**, or the installation's **Configure** page), deletes each private
key (the App's settings → **General** → **Private keys** → **Delete**), and
deletes the stored secrets:

```sh
for s in AGENT_APP_PRIVATE_KEY SEED_KEY; do gh secret delete "$s" --env agent-app --repo platform-factory/factory-controls; done
gh secret delete RATCHET_APP_PRIVATE_KEY --env ratchet --repo platform-factory/factory-controls
gh secret delete ANTHROPIC_API_KEY --env agent-model --repo platform-factory/factory-controls
```
