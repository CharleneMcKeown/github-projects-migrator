# GEI Projects and sub-issue migration to EMU

This is a companion script for **GitHub.com to GitHub.com Enterprise Managed Users
(EMU)** migrations. It snapshots GitHub Projects (Projects v2) and issue
sub-issue hierarchies in a source github.com organisation. After GitHub
Enterprise Importer (GEI) has migrated the repositories, it **rebuilds** them in
the destination EMU organisation.

GEI migrates repositories, issues, and pull requests. This script does not
migrate repository data, issue/PR metadata, Actions secrets/variables, runners,
or organisation configuration.

## Why projects are rebuilt rather than copied

GitHub's native project copy (`copyProjectV2`) needs **one identity that can
access both projects**. In an EMU migration that identity does not exist:
managed users cannot access organisations outside their enterprise, and
personal accounts cannot join an EMU enterprise. This tool therefore uses
**two separate tokens**:

| Variable | Identity | Used by | Access |
|---|---|---|---|
| `SOURCE_GH_TOKEN` | Personal github.com account that is a member of the source org | `export` | Read-only |
| `TARGET_GH_TOKEN` | EMU managed user (`login_shortcode`) in the destination org | `create`, `adopt`, `restore`, `verify` | Destination writes |

The source token is never used after export. The export data is the only
link between the two sides. Destination commands refuse to run if
`TARGET_GH_TOKEN` resolves to the account that ran the export. They also
refuse to run if it differs from the identity recorded in the state file. The
script warns if the destination login does not look like a managed user. Each
`gh` call receives only the selected token, so an ambient `GH_TOKEN`,
`GITHUB_TOKEN`, or stored `gh` login cannot be used by mistake.

## What it does

- **Export** (source token): saves the following to a local JSON manifest:
  - Selected project settings, custom fields (options, colours, descriptions,
    and iteration schedules), and views (layout, filter, visible fields,
    grouping, vertical grouping, and sorting).
  - Linked repositories, teams, and workflows (when readable).
  - Issue, PR, and draft items with their field values and archived state.
  - Sub-issue hierarchies for every mapped repository.
- **Create** (destination token): for each project, it creates a new
  **private** project in the EMU organisation and sets its description and
  README. It then:
  - Recreates custom fields and replaces the default Status options with the
    source options.
  - Links mapped repositories.
  - Recreates views in source order using the
    [REST Project views API](https://docs.github.com/en/rest/projects/views),
    including sort and group settings. Exact mapped `old-org/repo` names in
    view filters are rewritten to their destination names.
  - Removes the auto-created default view.
- **Restore** (destination token):
  - Resolves each migrated issue or PR by mapped repository and preserved
    number, checking type and exact title.
  - Restores sub-issue links and sibling order.
  - Adds issue/PR items and recreates draft issues (title and body) in source
    order.
  - Restores text, number, date, single-select, multi-select, and iteration
    values, plus archived state. Unset source values are cleared.
- **Verify** (destination token): read-only checks of fields, options,
  iterations, view configuration, item membership, draft content, values,
  archived state, and sub-issue order. It writes a report that separates
  automated checks from items that need manual review.

**This is not a promise of full-fidelity migration.** The API cannot create or
enable project workflows. Source collaborators, teams, and draft assignees are
personal or source-enterprise identities that do not exist in EMU. Some view
details are not exposed by the API, including roadmap date fields and zoom,
column widths, field sums, slice-by, and insights charts. The report lists all
of these. The tool does not rewrite `@mentions`, URLs, or org names in draft
bodies or READMEs. It does not migrate issue types, issue dependencies,
issue-backed fields, or other GEI-managed issue metadata.

## Prerequisites and safety

- Source: a github.com organisation. Destination: a github.com EMU
  organisation. GHE.com (data residency) and GitHub Enterprise Server are not
  supported; API requests explicitly target `github.com`.
- Python 3.9+ (standard library only) and the GitHub CLI (`gh`), used only as
  the HTTP client.
- **Source token** (`SOURCE_GH_TOKEN`) needs read access to the source
  organisation, the selected projects, and every mapped repository, including
  their issues. For a classic PAT, use `repo`, `read:org`, and `read:project`.
  Authorise the token for SAML SSO if the source org enforces it. Reading
  project workflows needs project admin access. Without it, workflows are
  reported as `source_workflows_unreadable` and you must record them by hand.
- **Destination token** (`TARGET_GH_TOKEN`) must belong to an EMU managed user
  who can create projects in the destination organisation and write issues in
  every mapped destination repository. For a classic PAT, use `repo`,
  `read:org`, and `project`. For fine-grained tokens, choose the destination
  organisation as resource owner and grant organisation **Projects:
  read/write**, plus repository **Issues: read/write**, **Pull requests:
  read**, and **Metadata: read** for the mapped repositories. Organisation
  policy may require token approval.
- Supply tokens through your shell or secret store. Never put them in files in
  this repository or in command arguments. For example, run this in an
  interactive shell:

  ```sh
  read -rs SOURCE_GH_TOKEN && export SOURCE_GH_TOKEN
  read -rs TARGET_GH_TOKEN && export TARGET_GH_TOKEN
  ```

  The two tokens must belong to different accounts.
- GEI must complete successfully before `restore`. The tool assumes that GEI
  preserved issue/PR numbers and exact titles and did not change issue vs PR
  type. It stops on mismatches and never guesses identities.
- Export before source access is removed. Then freeze source projects and issue
  hierarchies until verification; later source changes are not migrated.
- Include **every repository referenced by the selected projects and exported
  sub-issue trees** in the repo map. Unmapped item references block export.
  Hierarchies are scanned across every mapped repository. Source and target
  organisations must differ, and each repository can map to only one
  destination.
- Export queries the children of every issue in every mapped repository, plus
  every item and value in each selected project. Large repositories can take
  significant time and API quota. API calls are paced at one second by default
  and writes are never retried automatically.
- Run only one invocation at a time for a manifest/state pair.
- Manifests contain private issue titles, draft bodies, and field values.
  Newly written JSON files use restrictive permissions. Keep manifests, state,
  and reports private; `.gitignore` covers the default filenames only.
- No source mutations are performed. Destination writes need `--execute`.
  `restore --execute` **checks** that every destination project workflow is
  disabled and refuses to run otherwise. New projects enable built-in workflows
  such as *Item closed* and *Pull request merged* by default, and these could
  change Status values or issue state while items are restored.
- There is no automatic rollback. A run can partially write destination data
  before an error. Review the error, then resume with the same manifest and
  state.

## Usage

Run all commands from this repository directory:

```sh
python3 --version
gh --version
cp repo-map.example.json repo-map.json
```

Edit `repo-map.json` to map each source repository to its destination in the
EMU organisation:

```json
{
  "old-org/application": "new-emu-org/application",
  "old-org/shared-library": "new-emu-org/renamed-library"
}
```

1. **Plan.** Confirm both tokens, organisation policies, and the complete repo
   map. Identify each Projects v2 board to migrate; project numbers come from
   `https://github.com/orgs/old-org/projects/NUMBER`. Agree a freeze window.

2. **Export** using the source token. Repeat `--project` for each project, or
   omit it to export sub-issue hierarchies only:

   ```sh
   python3 migrate_gei_extras.py export \
     --source-org old-org --target-org new-emu-org \
     --repo-map repo-map.json --project 12 --project 34 \
     --manifest migration.json
   ```

3. **Run GEI** for the mapped repositories using your normal procedure (GEI
   uses its own source and destination tokens). Review GEI's logs and confirm
   that issue/PR numbers and titles are preserved. This script does not invoke
   GEI. You can run steps 4 and 5 before GEI finishes, because `create` only
   needs the destination organisation and repositories.

4. **Preview project creation** using the destination token (read-only). This
   shows the fields, views, and rewritten view filters:

   ```sh
   python3 migrate_gei_extras.py create \
     --manifest migration.json --state migration-state.json
   ```

5. **Create the projects:**

   ```sh
   python3 migrate_gei_extras.py create \
     --manifest migration.json --state migration-state.json --execute
   ```

   Save the printed destination URLs. Rerunning is safe: existing fields,
   views, and links are detected and not duplicated. If the run fails while
   creating a project, see [Resuming](#resuming-and-uncertain-outcomes).

6. **Disable workflows and review the projects.** In each destination
   project, open **Workflows** and disable every workflow. Check the views,
   fields, iteration schedule, and repository links. Do not edit view names,
   layouts, filters, fields, grouping, or sorting until verification has
   passed, because restore and verify compare them against the export.

7. **Preview restoration** (read-only). This resolves every destination
   issue/PR, validates field mappings and hierarchy conflicts, and lists any
   workflows that are still enabled:

   ```sh
   python3 migrate_gei_extras.py restore \
     --manifest migration.json --state migration-state.json
   ```

8. **Restore:**

   ```sh
   python3 migrate_gei_extras.py restore \
     --manifest migration.json --state migration-state.json \
     --execute --report migration-report.json
   ```

9. **Verify and finish.** Verification runs at the end of restore. You can run
   it again at any time (read-only against GitHub; it writes the local report):

   ```sh
   python3 migrate_gei_extras.py verify \
     --manifest migration.json --state migration-state.json \
     --report migration-report.json
   ```

   Then use the report to:
   - Recreate workflows from `source_workflows` (including auto-add) and enable
     them only after checking their rules.
   - Add EMU collaborators and teams (`source_teams_not_linked`).
   - Reassign drafts (`draft_assignees_not_restored`).
   - Apply closed, template, or public settings
     (`source_project_settings_not_applied`).
   - Complete every `manual_review_required` item.

The default API pacing is one second between calls. To change it, pass
`--delay` before the subcommand, for example `python3 migrate_gei_extras.py
--delay 1.5 export ...`.

Restore reapplies the exported snapshot on each run. Do not rerun it after
users start editing the destination projects unless you intend to overwrite
their values.

## Resuming and uncertain outcomes

Keep the same manifest and state paths, and the same destination identity.
State is pinned to the manifest's hash.

- **Projects:** the state is marked `create_pending` before the create
  mutation is sent. If GitHub rejects the mutation (for example, a missing
  `project` scope), the marker is cleared automatically; fix the cause and
  rerun. If the outcome is unknown (network failure), the project may still
  have been created. Find it in the destination organisation and adopt it, then
  rerun `create --execute`:

  ```sh
  python3 migrate_gei_extras.py adopt \
    --manifest migration.json --state migration-state.json \
    --source-project 12 --target-project 56 --execute
  ```

  `adopt` only accepts a freshly created project: same title as the source,
  no items, and at most one view. Its existing view is treated as a default
  and is replaced.
- **Views and draft issues:** each is marked pending before its create call.
  On rerun, a pending entry is resolved by finding exactly one matching
  destination object (same view name, or same draft title and body). If there
  are several matches, the tool stops; delete the unintended duplicate in
  GitHub and retry.
- **Fields, repository links, issue/PR items, and sub-issues:** these are
  detected by reading the destination, so reruns do not duplicate them.

Do not delete or hand-edit state to work around an uncertain write.

## Troubleshooting

- **`Set SOURCE_GH_TOKEN` / `Set TARGET_GH_TOKEN`:** export the token for that
  step in your shell.
- **`TARGET_GH_TOKEN belongs to the source account`:** you are using the
  personal account for destination writes. Use the managed user's token.
- **Authentication or permission errors:** check token scopes, SSO
  authorisation (source), fine-grained token approval, and
  organisation/repository roles. Do not include tokens in bug reports.
- **GEI identity check failed:** stop. GEI may not have preserved the issue
  number, exact title, or issue/PR type. Resolve this manually.
- **View uses a field which does not exist in the destination project:** a
  source view references a system field that the new project lacks. Inspect the
  destination project before retrying.
- **Fields/views differ during restore or verify:** the destination project
  was edited after creation. Revert the change, or rerun `create --execute` for
  fields. Views are compared but never modified once created.
- **Unmatched iteration:** iterations are matched by title, start date, and
  duration. Fix the destination schedule and retry.
- **Workflows are enabled:** disable them in the project's Workflows settings
  and rerun.

## Sources and validation boundary

- [About migrations between GitHub products](https://docs.github.com/en/migrations/using-github-enterprise-importer/migrating-between-github-products/about-migrations-between-github-products)
- [About Enterprise Managed Users](https://docs.github.com/en/enterprise-cloud@latest/admin/managing-iam/understanding-iam-for-enterprises/about-enterprise-managed-users)
- [Projects GraphQL reference](https://docs.github.com/en/graphql/reference/projects)
- [REST API endpoints for Project views](https://docs.github.com/en/rest/projects/views)
- [Sub-issues REST API](https://docs.github.com/en/rest/issues/sub-issues)
- [Fine-grained personal access token permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)

The GraphQL documents were checked against the live github.com schema, and
the export/transform path was exercised read-only against a public project. The
unit tests use fake APIs. No destination writes have been run against a real
EMU organisation; run a small pilot before production.

```sh
python3 -m unittest -v test_migrate_gei_extras.py
```
