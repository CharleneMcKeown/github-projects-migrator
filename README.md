# GEI Projects and sub-issue restoration

Companion script for **GitHub.com-to-GitHub.com** migrations. It snapshots and
restores GitHub Projects (Projects v2) configuration, supported project field
values, issue/PR membership, and issue sub-issue hierarchies around a GitHub
Enterprise Importer (GEI) repository and issue migration.

GEI is responsible for migrating repositories, issues, and pull requests. This
script does not migrate repository data, issue/PR metadata, GitHub Actions
secrets or variables, runners, or organization configuration.

## What it does

- Exports selected project configurations, issue/PR membership, custom values,
  archived state, and sub-issue hierarchies to a local JSON manifest.
- Uses GitHub's native project-copy operation, with draft issues included, rather
  than rebuilding views or iterations through incomplete configuration APIs.
- Resolves destination issues/PRs by explicitly mapped repository and preserved
  number, checking type and exact title before making changes.
- Restores sub-issue relationships, including cross-repository links within the
  map, nested hierarchies, and sibling ordering. Existing different parents or
  extra destination sub-issues cause a failure, never a forced replacement.
- Restores issue/PR membership and supported project-specific text, number,
  date, single-select, multi-select, and iteration values. Unset source fields
  are cleared to avoid retaining destination defaults.
- Verifies restored membership, values, archive state, and sub-issue order.
  Emits a report that explicitly separates automated checks from manual review.

**This is not a promise of full-fidelity automatic migration.** GitHub's native
project-copy operation copies views, custom fields, draft issues and their
values, configured workflows except auto-add workflows, and insights. It does
not copy the original issue/PR items, collaborators, or team/repository links.
This script subsequently restores supported issue/PR items and values, but does
not recreate auto-add workflows, collaborators, or team/repository links. It
does not rewrite old-organization/repository references in filters, workflow
rules, insights, markdown, or draft bodies. Inspect those manually.

Mixed draft/issue item ordering, draft archive/assignee fidelity, iteration
breaks/unassigned schedules, issue-backed fields, and workflow definitions and
enabled states need manual review. It does not migrate issue types, issue
dependencies, or other GEI-managed issue metadata. The manifest includes source
view filters for reference; the report does not claim those filters point at the
new organization.

## Prerequisites and safety

- GitHub.com source and destination organizations. GitHub Enterprise Server
  and other GitHub hosts are not supported; API requests explicitly target
  `github.com`.
- Python 3.9+ (standard library only) and the GitHub CLI (`gh`).
- An account authorized to read the source organization, source projects, and
  mapped source repositories, and create/manage projects in the destination
  organization and read/write issues in mapped destination repositories.
  Native project copying requires the **same `gh` identity/token to access both
  projects**. Your effective access must also satisfy both organizations'
  policies, including SAML SSO authorization, OAuth-app restrictions, and
  personal access token approval where applicable.
- For the GitHub CLI's OAuth credential, the usual classic OAuth scopes for
  this script are `repo`, `read:org`, and `project`. `repo` covers access to
  private mapped repositories and their issues; `read:org` allows organization
  reads; `project` is needed for Projects access. Scopes do not grant
  organization/repository roles the account itself lacks. Add scopes to the
  existing `gh` login with:

  ```sh
  gh auth login --hostname github.com
  gh auth refresh --hostname github.com --scopes repo,read:org,project
  gh auth status --hostname github.com
  ```

- For a classic personal access token used through `GH_TOKEN`, grant the
  equivalent access needed for the operations above; the `gh auth refresh`
  command only refreshes a `gh`-managed OAuth credential. For fine-grained
  tokens or GitHub Apps, select the current Projects and repository issue
  permissions required by the APIs used here, install/authorize them for both
  organizations and all mapped repositories, and test with a pilot. GitHub
  permission requirements can vary by token type and organization policy; API
  errors identify missing permissions.
- Authenticate without putting credentials in source files or command history.
  The script uses the active `gh` credential and does not accept a token
  argument. For a CI/automation environment, use the platform's secret store to
  provide `GH_TOKEN`; do not print it or use `gh auth status --show-token`.
- GEI must complete successfully before project copying or restoration. This
  tool assumes GEI preserved issue/PR numbers and exact titles and did not
  change issue-vs-PR type; it intentionally stops on mismatches. It is not a
  general-purpose issue identity matcher.
- Export before source access is removed. Keep source projects accessible and
  unchanged until project copying completes: the copy step reads the live
  source project and compares it with the export. Freeze source projects and
  issue hierarchies through verification. There is no cross-organization
  transaction or automatic rollback.
- Include **every repository referenced by the selected projects and exported
  sub-issue trees** in the map. Unmapped references block export. Hierarchies
  are scanned across every mapped repository, not just project members.
  Inbound parents outside the map are not discovered; include those parent
  repositories explicitly when relevant. Source and target organizations must
  differ, and each repository can map to only one destination.
- Export queries every real issue's children in every mapped repository, plus
  each selected project's items and field values. Large repositories can take
  significant time and API quota. Pagination is implemented for collection
  reads; calls are paced at one second by default. There are no blind automatic
  write retries. On rate-limit failures, wait for the reset and resume using
  the same manifest and state.
- Run only one invocation at a time for a manifest/state pair. There is no
  cross-process or distributed lock.
- Manifests contain private issue titles, draft bodies, and field values. Newly
  written JSON files use restrictive permissions. Keep manifests/state/reports
  in a private local directory; do not commit them or upload them publicly.
  `.gitignore` covers the default filenames only; it does not untrack files
  already committed or purge Git history.
- No source mutations or deletions are performed. Destination writes are opt-in
  with `--execute`. Copied project visibility is explicitly set to private after
  copy; native copying itself determines visibility at creation time. Review
  source visibility, destination policy, and the copied project's access before
  use. The project is created before this script makes it private.
- Use a small pilot first. Native copying can trigger project workflows, and
  this script cannot pause or inspect workflows. Inspect the copied project and
  pause destination workflows before restoration. The required
  `--workflows-paused` switch is an acknowledgment only; it is not a check.
- There is no automatic rollback. A run can partially write destination data
  before an error. Review the error and destination state before resuming.

### Credential and permission notes

The recommended interactive setup is a `gh` login using your organization's
approved authentication method. `gh auth refresh` can add scopes to that
credential but cannot override missing account roles, organization policy,
token approval, or SSO authorization.

The script calls the GitHub GraphQL API for organization Projects and the REST
API for repository issues/sub-issues. GitHub may report additional or more
specific permissions for a fine-grained token, GitHub App, or organization
policy. Do not solve an authorization error by granting broad access
automatically; ask an organization administrator to approve the narrow access
required, then retry. The GitHub CLI uses its stored credential or `GH_TOKEN`;
it does not read tokens from this repository.

## Usage

Run all commands from this repository directory. No Python packages need to be
installed. Check the CLI and authentication first:

```sh
python3 --version
gh --version
gh auth status --hostname github.com
```

Confirm the active `gh` account is the intended identity before continuing.

Copy [repo-map.example.json](./repo-map.example.json) to `repo-map.json`:

```sh
cp repo-map.example.json repo-map.json
```

Edit the local copy to map each source repository to its destination:

```json
{
  "old-org/application": "new-org/application",
  "old-org/shared-library": "new-org/renamed-library"
}
```

The included `.gitignore` excludes the local map, default `migration*.json`
outputs, logs, backups, and local environment files. If you choose other output
filenames, keep them outside the repository or add explicit ignore rules.
Replace every example owner and repository with the real source-to-destination
mapping. Do not put credentials in this file. Keep the local map private;
publish only the sanitized example. Ignore rules do not remove files already
tracked by Git or purge earlier commits.

1. **Plan the migration.** Confirm organization/repository access and policies,
   prepare the complete map, identify every Projects v2 board to copy, and
   agree on a freeze window. Project numbers come from
   `https://github.com/orgs/old-org/projects/NUMBER`. The repository map is
   scanned for hierarchies across all mapped repositories, even when no
   projects are selected.

2. **Export before GEI or before source access is withdrawn.** Export creates a
   new manifest and refuses to overwrite an existing one. Repeat `--project`
   for each selected project; omit `--project` to export sub-issue hierarchies
   only.

   ```sh
   python3 migrate_gei_extras.py export \
     --source-org old-org --target-org new-org \
     --repo-map repo-map.json --project 12 --project 34 \
     --manifest migration.json
   ```

   Store the manifest privately. It contains source data and is required for
   later steps.

3. **Run GEI** for the mapped repositories/issues/PRs using your normal GEI
   procedure. Wait for successful completion and review GEI's own migration
   results/logs. This script does not invoke GEI.

4. **Check the migrated issue/PR identities** in the destination. Confirm the
   referenced repositories exist, issue/PR numbers and titles are preserved,
   and issue-vs-PR types match. Correct GEI problems before continuing.

5. **Preview native project copies.** Keep source project access available;
   this command compares each live source project to the exported manifest.
   The preview performs API reads but no destination writes:

   ```sh
   python3 migrate_gei_extras.py copy \
     --manifest migration.json --state migration-state.json
   ```

6. **Create the project copies**, including draft issues. This is the first
   destination write. The state file is created/updated to record copy results:

   ```sh
   python3 migrate_gei_extras.py copy \
     --manifest migration.json --state migration-state.json --execute
   ```

   Save the returned destination URLs. If the operation errors after a copy
   might have been created, do not blindly run it again; follow
   [Resuming and uncertain copy outcomes](#resuming-and-uncertain-copy-outcomes).

7. **Inspect each copied project.** Confirm the intended destination, access
   policy, workflows, views, fields, insights, draft content, and iteration
   schedule. Recreate missing collaborators and team/repository links as
   required. Recreate auto-add workflows disabled, and review all workflow
   filters/rules for old organization/repository names. Pause destination
   workflows before restoration, especially rules that could modify newly
   added items. Do not edit view names, layouts, or filters until automated
   verification is complete; the script compares them against the export.

8. **Preview restoration.** This resolves every referenced destination issue/PR,
   validates field mappings, and checks hierarchy conflicts without writing:

   ```sh
   python3 migrate_gei_extras.py restore \
     --manifest migration.json --state migration-state.json
   ```

9. **Restore only after the dry run succeeds.** You must pause workflows
   yourself and explicitly acknowledge that before destination writes:

   ```sh
   python3 migrate_gei_extras.py restore \
     --manifest migration.json --state migration-state.json \
     --execute --workflows-paused --report migration-report.json
   ```

10. **Verify and review.** Restoration runs verification at its end. Review
    `migration-report.json`, complete each `manual_review_required` item, and
    resolve any failures before reopening the migration window. You can run
    standalone verification again (read-only against GitHub; writes the local
    report) before changing views or workflows:

    ```sh
    python3 migrate_gei_extras.py verify \
      --manifest migration.json --state migration-state.json \
      --report migration-report.json
    ```

    Finish old-name updates, recreate and test auto-add workflows, confirm
    permissions/visibility, review descriptions and README, and re-enable
    workflows only after checking their rules and effects. Then unfreeze source
    and destination changes as appropriate.

All remote writes affect destination resources. Export, copy/restore dry runs,
and verify are read-only against GitHub; verify writes its report locally. No
command mutates source resources. `copy`, `restore`, and `adopt` need
`--execute` to write to the destination. `--workflows-paused` is an explicit
acknowledgment, not a workflow-state check. Restoration reapplies the exported
snapshot on each run; do not rerun after users start editing destination fields
unless you intend to overwrite those project values.

The default API pacing is one second between calls. To change it, pass the
global `--delay` option before the subcommand, for example:

```sh
python3 migrate_gei_extras.py --delay 1.5 export \
  --source-org old-org --target-org new-org \
  --repo-map repo-map.json --manifest migration.json
```

## Resuming and uncertain copy outcomes

Keep the same manifest and state paths. State is pinned to the manifest's hash
and each copied project ID is saved immediately. Existing project items and
sub-issue links are read on resume, so retries do not intentionally duplicate
them. A failed copy may have succeeded server-side: the state is marked
`copy_pending` **before** sending the mutation, and reruns refuse to copy again.

Find the resulting destination project in GitHub, confirm it is the right copy,
and explicitly adopt it:

```sh
python3 migrate_gei_extras.py adopt \
  --manifest migration.json --state migration-state.json \
  --source-project 12 --target-project 56 --execute
```

Adoption records an existing target project; it does not populate it or change
its visibility. The next `copy --execute` run enforces private visibility.
Use adoption only for the matching native copy, not an unrelated populated
project. Do not delete or hand-edit state to work around an uncertain write.

If iteration IDs differ (normal), they are mapped by title, start date, and
duration. Missing or ambiguous matches fail before membership restoration.
Inspect/fix the destination schedule and retry rather than assigning a guessed
iteration.

## Troubleshooting

- **Authentication or permission error:** check `gh auth status
  --hostname github.com`, the active account, organization authorization/SSO,
  token scopes/permissions, and repository/project access. Do not include
  credentials in bug reports.
- **Destination identity check failed:** stop. GEI may not have preserved the
  issue number, exact title, or issue/PR type. Resolve the mapping manually;
  this script intentionally will not guess another issue.
- **Project changed after export:** freeze source changes and export a fresh
  manifest to a new path. Do not edit an existing manifest or reuse its state
  for a different snapshot.
- **Field/view mismatch or unmatched iteration:** inspect and correct the
  destination project configuration, then retry the dry run. Do not assign a
  guessed field or iteration mapping.
- **Copy outcome is uncertain / `copy_pending`:** use the GitHub UI to find and
  verify the native copy, then use the `adopt` command below. Do not delete or
  hand-edit state to force a retry.
- **Rate limit or transient API failure:** wait until the limit resets, inspect
  destination state for any partial writes, then retry with the same manifest
  and state. Writes are not automatically retried.

## Sources and validation boundary

- [Copying an existing project](https://docs.github.com/en/issues/planning-and-tracking-with-projects/creating-projects/copying-an-existing-project)
- [GitHub CLI project copy](https://cli.github.com/manual/gh_project_copy)
- [GitHub CLI authentication](https://cli.github.com/manual/gh_auth)
- [GitHub CLI scope refresh](https://cli.github.com/manual/gh_auth_refresh)
- [Authenticating to the GraphQL API](https://docs.github.com/en/graphql/guides/forming-calls-with-graphql#authenticating-with-graphql)
- [Fine-grained personal access token permissions](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
- [Projects GraphQL reference](https://docs.github.com/en/graphql/reference/projects)
- [Sub-issues REST API](https://docs.github.com/en/rest/issues/sub-issues)
- [GEI migrations between GitHub products](https://docs.github.com/en/migrations/using-github-enterprise-importer/migrating-between-github-products/about-migrations-between-github-products)

The accompanying tests use fake APIs; API shapes were checked against the live
public schema and official REST API description. No real organisation migration
has been run for you. Use a small pilot before production.

```sh
python3 -m unittest -v test_migrate_gei_extras.py
```
