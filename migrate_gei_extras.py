#!/usr/bin/env python3
"""Rebuild GitHub Projects and sub-issue links after a GEI migration from a
github.com organisation to a github.com Enterprise Managed Users (EMU)
organisation, using separate source and destination tokens."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


class MigrationError(RuntimeError):
    pass


class GraphQLError(MigrationError):
    """GitHub answered with GraphQL errors, so the outcome is known."""


SOURCE_TOKEN_ENV = "SOURCE_GH_TOKEN"
TARGET_TOKEN_ENV = "TARGET_GH_TOKEN"
# Never let an ambient credential override the explicitly selected token.
AMBIENT_TOKEN_ENVS = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                      "GITHUB_ENTERPRISE_TOKEN"}
REST_VERSION = "2022-11-28"
VIEWS_REST_VERSION = "2026-03-10"
MANIFEST_VERSION = 2
PENDING = "pending"

FIELD_FRAGMENT = """
__typename
... on ProjectV2Field { id databaseId name dataType isIssueField }
... on ProjectV2SingleSelectField {
  id databaseId name dataType isIssueField
  options { id name color description }
}
... on ProjectV2MultiSelectField {
  id databaseId name dataType isIssueField
  multiSelectOptions { id name color description }
}
... on ProjectV2IterationField {
  id databaseId name dataType isIssueField
  configuration {
    duration startDay
    iterations { id title startDate duration }
    completedIterations { id title startDate duration }
  }
}
"""
VALUE_FRAGMENT = """
__typename
... on ProjectV2ItemFieldTextValue { text field { ...Field } }
... on ProjectV2ItemFieldNumberValue { number field { ...Field } }
... on ProjectV2ItemFieldDateValue { date field { ...Field } }
... on ProjectV2ItemFieldSingleSelectValue { name field { ...Field } }
... on ProjectV2ItemFieldMultiSelectValue {
  options { name } field { ...Field }
}
... on ProjectV2ItemFieldIterationValue {
  iterationId field { ...Field }
}
"""
FRAGMENT = "fragment Field on ProjectV2FieldConfiguration {" + FIELD_FRAGMENT + "}"
FIELD_REF = "nodes { ... on ProjectV2FieldCommon { name dataType } }"
WRITABLE = {"TEXT", "NUMBER", "DATE", "SINGLE_SELECT", "MULTI_SELECT", "ITERATION"}
LAYOUTS = {"TABLE_LAYOUT": "table", "BOARD_LAYOUT": "board",
           "ROADMAP_LAYOUT": "roadmap"}
MANUAL_CHECKS = [
    "Recreate project workflows (built-in and auto-add) in the destination. "
    "There is no API to create or enable workflows; compare the report's "
    "source_workflows list with the destination and keep them disabled until "
    "restoration is verified.",
    "Recreate collaborators and team links with EMU managed users/teams. Source "
    "accounts and teams do not exist in the EMU enterprise.",
    "Recreate draft issue assignees with EMU managed users (not restored).",
    "Review view filters and draft/README text for references the tool did not "
    "rewrite (only exact mapped owner/repo names in view filters are rewritten), "
    "including @user mentions of non-EMU accounts.",
    "Review roadmap view date fields, zoom level, markers, column widths, field "
    "sums, slice-by, and insights charts; these are not exposed by the API.",
    "Projects are created private. Review visibility, closed and template "
    "settings before changing them.",
    "Check issue-backed custom fields, issue types, assignees, labels, milestones, "
    "and dependency links separately; this tool does not migrate those values.",
    "Check source items hidden by permissions. Null/redacted project content "
    "blocks export; the API cannot prove access to every organisation resource.",
    "Keep source projects and issue hierarchies frozen from export until "
    "verification; later source changes are not migrated.",
]


class API:
    def __init__(self, token, delay=1.0):
        require(token, "A GitHub token is required.")
        self.token = token
        self.delay = delay

    def request(self, endpoint, method="GET", payload=None, paginate=False,
                missing_ok=False, version=REST_VERSION):
        command = [
            "gh", "api", "--hostname", "github.com", "--method", method,
            "-H", "Accept: application/vnd.github+json",
            "-H", f"X-GitHub-Api-Version: {version}", endpoint,
        ]
        if paginate:
            command += ["--paginate", "--slurp"]
        if payload is not None:
            command += ["--input", "-"]
        env = {k: v for k, v in os.environ.items() if k not in AMBIENT_TOKEN_ENVS}
        env["GH_TOKEN"] = self.token
        time.sleep(self.delay)
        result = subprocess.run(
            command, input=json.dumps(payload) if payload is not None else None,
            text=True, capture_output=True, check=False, env=env,
        )
        if result.returncode:
            if missing_ok and result.stdout.strip():
                try:
                    error = json.loads(result.stdout)
                except json.JSONDecodeError:
                    error = {}
                if str(error.get("status")) == "404":
                    return None
            raise MigrationError(
                f"{method} {endpoint} failed. No automatic write retries.\n"
                f"{result.stderr.strip()}\n{result.stdout.strip()}"
            )
        return json.loads(result.stdout) if result.stdout.strip() else None

    def graphql(self, query, **variables):
        result = self.request(
            "graphql", "POST", {"query": query, "variables": variables}
        )
        if result.get("errors"):
            raise GraphQLError(json.dumps(result["errors"]))
        return result["data"]

    def pages(self, query, key, **variables):
        nodes, cursor = [], None
        while True:
            data = self.graphql(query, cursor=cursor, **variables)
            for part in key:
                data = data[part]
                if data is None:
                    raise MigrationError(f"Missing or inaccessible GraphQL object: {key}")
            nodes.extend(data["nodes"])
            if not data["pageInfo"]["hasNextPage"]:
                return nodes
            cursor = data["pageInfo"]["endCursor"]

    def rest_list(self, endpoint):
        pages = self.request(
            endpoint + ("&" if "?" in endpoint else "?") + "per_page=100",
            paginate=True,
        )
        return [item for page in pages for item in page]


def require(condition, message):
    if not condition:
        raise MigrationError(message)


def api_from_env(variable, delay):
    token = os.environ.get(variable, "").strip()
    require(token, f"Set {variable} to the token for this step. Source and "
                   f"destination use separate tokens ({SOURCE_TOKEN_ENV} for "
                   f"export, {TARGET_TOKEN_ENV} for the EMU destination).")
    return API(token, delay)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ref(repo, number):
    return f"{repo.lower()}#{number}"


def issue_ref(issue):
    parts = issue["html_url"].split("/")
    require(len(parts) >= 7 and parts[2] == "github.com",
            f"Unexpected issue URL: {issue['html_url']}")
    return ref("/".join(parts[3:5]), issue["number"])


def validate_repo(repo):
    require(isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo),
            f"Expected owner/repository, got {repo!r}")


def viewer(api):
    return api.graphql("query { viewer { login } }")["viewer"]["login"]


def project(api, org, number):
    result = api.graphql(
        """query($org:String!,$number:Int!) {
          organization(login:$org) {
            id projectV2(number:$number) {
              id number title url public closed template shortDescription readme
              repositories(first:100) { nodes { nameWithOwner } }
              teams(first:100) { nodes { slug } }
            }
          }
        }""", org=org, number=number,
    )["organization"]
    require(result is not None and result["projectV2"] is not None,
            f"Project not found or inaccessible: {org}/{number}")
    return result["projectV2"]


def workflows(api, project_id, required=True):
    """Workflows need project admin; optional for a read-only source token."""
    try:
        return api.graphql(
            """query($id:ID!) { node(id:$id) { ... on ProjectV2 {
              workflows(first:100) { nodes { name number enabled } }
            } } }""", id=project_id,
        )["node"]["workflows"]["nodes"]
    except MigrationError as error:
        if required or "FORBIDDEN" not in str(error):
            raise
        print("WARNING: cannot read source project workflows (requires project "
              "admin); they will be listed as unknown.", file=sys.stderr)
        return None


def fields(api, project_id):
    return api.pages(
        """query($id:ID!,$cursor:String) {
          node(id:$id) { ... on ProjectV2 {
            fields(first:100,after:$cursor) {
              nodes { ...Field } pageInfo { hasNextPage endCursor }
            }
          } }
        }""" + FRAGMENT, ["node", "fields"], id=project_id,
    )


def items(api, project_id):
    result = api.pages(
        """query($id:ID!,$cursor:String) {
          node(id:$id) { ... on ProjectV2 {
            items(first:50,after:$cursor,
                  archivedStates:[ARCHIVED,NOT_ARCHIVED]) {
              nodes {
                id isArchived content {
                  __typename
                  ... on Issue { number title repository { nameWithOwner } }
                  ... on PullRequest { number title repository { nameWithOwner } }
                  ... on DraftIssue {
                    title body assignees(first:20) { nodes { login } }
                  }
                }
              } pageInfo { hasNextPage endCursor }
            }
          } }
        }""", ["node", "items"], id=project_id,
    )
    for item in result:
        item["values"] = api.pages(
            """query($id:ID!,$cursor:String) {
              node(id:$id) { ... on ProjectV2Item {
                fieldValues(first:100,after:$cursor) {
                  nodes { """ + VALUE_FRAGMENT + """ }
                  pageInfo { hasNextPage endCursor }
                }
              } }
            }""" + FRAGMENT, ["node", "fieldValues"], id=item["id"],
        )
    return result


def views(api, project_id):
    return api.pages(
        """query($id:ID!,$cursor:String) {
          node(id:$id) { ... on ProjectV2 {
            views(first:100,after:$cursor) {
              nodes {
                id number name layout filter
                fields(first:100) { """ + FIELD_REF + """ }
                groupByFields(first:10) { """ + FIELD_REF + """ }
                verticalGroupByFields(first:10) { """ + FIELD_REF + """ }
                sortByFields(first:10) {
                  nodes { direction field {
                    ... on ProjectV2FieldCommon { name dataType }
                  } }
                }
              }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }""", ["node", "views"], id=project_id,
    )


def snapshot(api, org, number):
    data = project(api, org, number)
    data["fields"] = fields(api, data["id"])
    data["items"] = items(api, data["id"])
    data["views"] = views(api, data["id"])
    data["workflows"] = workflows(api, data["id"], required=False)
    for item in data["items"]:
        content = item["content"]
        if content and content["__typename"] in {"Issue", "PullRequest"}:
            item["reference"] = ref(
                content["repository"]["nameWithOwner"], content["number"]
            )
    return data


def export(api, args):
    require(not Path(args.manifest).exists(), "Manifest exists; choose a new path.")
    require(args.source_org.lower() != args.target_org.lower(),
            "Source and destination organisations must differ.")
    mapping = read_json(args.repo_map)
    require(isinstance(mapping, dict) and mapping, "Repository map must be a nonempty object.")
    normalized = {}
    for source, target in mapping.items():
        validate_repo(source)
        validate_repo(target)
        require(source.split("/")[0].lower() == args.source_org.lower(),
                f"Source repo is outside --source-org: {source}")
        require(target.split("/")[0].lower() == args.target_org.lower(),
                f"Destination repo is outside --target-org: {target}")
        require(source.lower() != target.lower(), "Source and destination must differ.")
        require(source.lower() not in normalized, "Duplicate source repository.")
        normalized[source.lower()] = target
    require(len({r.lower() for r in normalized.values()}) == len(normalized),
            "Multiple source repositories cannot map to one destination.")
    login = viewer(api)
    print(f"Source identity ({SOURCE_TOKEN_ENV}): {login}", file=sys.stderr)
    manifest = {
        "version": MANIFEST_VERSION, "source_org": args.source_org,
        "target_org": args.target_org, "source_login": login,
        "repo_map": normalized, "references": {}, "hierarchies": [], "projects": [],
        "manual_checks": MANUAL_CHECKS,
    }

    def remember(key, title, kind):
        old = manifest["references"].get(key)
        value = {"title": title, "kind": kind}
        require(old is None or old == value,
                f"Source changed during export: {key}; freeze writes and re-export.")
        manifest["references"][key] = value

    for repo in normalized:
        print(f"Exporting issue hierarchy: {repo}", file=sys.stderr)
        for issue in api.rest_list(f"repos/{repo}/issues?state=all"):
            if "pull_request" in issue:
                continue
            # Query every real issue: do not rely on optional summary properties.
            children = api.rest_list(
                f"repos/{repo}/issues/{issue['number']}/sub_issues"
            )
            if not children:
                continue
            parent = ref(repo, issue["number"])
            remember(parent, issue["title"], "Issue")
            child_refs = []
            for child in children:
                key = issue_ref(child)
                require(key.split("#")[0] in normalized,
                        f"Sub-issue {key} is outside the explicit repository map.")
                remember(key, child["title"], "Issue")
                child_refs.append(key)
            manifest["hierarchies"].append({"parent": parent, "children": child_refs})

    for number in dict.fromkeys(args.project):
        print(f"Exporting project: {args.source_org}/{number}", file=sys.stderr)
        data = snapshot(api, args.source_org, number)
        for item in data["items"]:
            content = item["content"]
            require(content is not None, f"Project {number} has inaccessible content.")
            kind = content["__typename"]
            require(kind in {"Issue", "PullRequest", "DraftIssue"},
                    f"Project {number} has unsupported content: {kind}")
            if kind == "DraftIssue":
                continue
            repo = content["repository"]["nameWithOwner"].lower()
            require(repo in normalized,
                    f"Project {number} references unmapped repository {repo}.")
            key = ref(repo, content["number"])
            item["reference"] = key
            remember(key, content["title"], kind)
        manifest["projects"].append(data)
    write_json(args.manifest, manifest)
    print(f"Exported {len(manifest['projects'])} projects and "
          f"{sum(len(h['children']) for h in manifest['hierarchies'])} sub-issue links "
          f"to {args.manifest}.")


def destination(api, manifest, key):
    source_repo, number = key.rsplit("#", 1)
    repo = manifest["repo_map"][source_repo]
    data = api.request(f"repos/{repo}/issues/{number}")
    expected = manifest["references"][key]
    kind = "PullRequest" if "pull_request" in data else "Issue"
    require(kind == expected["kind"] and data["number"] == int(number)
            and data["title"] == expected["title"],
            f"GEI identity check failed for {repo}#{number}. Expected same issue/PR "
            "number, type and title. Review the migration; no identity guessing.")
    actual_repo = "/".join(data["html_url"].split("/")[3:5])
    require(actual_repo.lower() == repo.lower(),
            f"Destination redirected to another repository: {data['html_url']}")
    if kind == "PullRequest":
        data["node_id"] = api.request(f"repos/{repo}/pulls/{number}")["node_id"]
    return {"repo": repo, "number": int(number), "id": data["id"],
            "node_id": data["node_id"], "parent_issue_url": data.get("parent_issue_url")}


def preflight(api, manifest):
    resolved = {}
    for key in manifest["references"]:
        print(f"Checking GEI destination: {key}", file=sys.stderr)
        resolved[key] = destination(api, manifest, key)
    for hierarchy in manifest["hierarchies"]:
        parent = resolved[hierarchy["parent"]]
        expected_parent = (
            f"https://api.github.com/repos/{parent['repo']}/issues/{parent['number']}"
        ).lower()
        existing_children = api.rest_list(
            f"repos/{parent['repo']}/issues/{parent['number']}/sub_issues"
        )
        wanted = {resolved[k]["id"] for k in hierarchy["children"]}
        require(all(child["id"] in wanted for child in existing_children),
                f"Destination {hierarchy['parent']} has extra sub-issues; refusing to reorder.")
        for key in hierarchy["children"]:
            child = resolved[key]
            current = api.request(
                f"repos/{child['repo']}/issues/{child['number']}/parent",
                missing_ok=True,
            )
            if current is not None:
                require(current["url"].lower() == expected_parent,
                        f"{key} already has another destination parent. "
                        "The tool never replaces existing parents.")
    return resolved


def load_state(args, manifest):
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if Path(args.state).exists():
        state = read_json(args.state)
        require(state["manifest_sha256"] == digest,
                "State belongs to another manifest. Use the matching state file.")
        return state
    return {"manifest_sha256": digest, "target_login": None, "projects": {}}


def check_target_identity(api, manifest, state, args):
    """Destination writes must come from the EMU identity, never the source one."""
    login = viewer(api)
    print(f"Destination identity ({TARGET_TOKEN_ENV}): {login}", file=sys.stderr)
    require(login.lower() != manifest["source_login"].lower(),
            f"{TARGET_TOKEN_ENV} belongs to the source account {login}. Use a token "
            "for an EMU managed user in the destination enterprise.")
    if "_" not in login:
        print(f"WARNING: {login} does not look like an EMU managed user "
              "(login_shortcode). Confirm the destination token.", file=sys.stderr)
    known = state.get("target_login")
    require(not known or known.lower() == login.lower(),
            f"State was written by destination identity {known}; current token is "
            f"{login}. Use the same destination identity.")
    if getattr(args, "execute", False) and not known:
        state["target_login"] = login
        write_json(args.state, state)
    return login


def mutation(api, name, input_type, result, value):
    return api.graphql(
        f"mutation($input:{input_type}!) {{ {name}(input:$input) {{ {result} }} }}",
        input=value,
    )[name]


def tracked_create(args, state, bucket, key, label, candidates, create):
    """Create once. An interrupted create is recovered only by a unique match."""
    current = bucket.get(key)
    if current not in (None, PENDING):
        return current
    if current == PENDING:
        require(len(candidates) <= 1,
                f"Interrupted creation of {label} matches several destination "
                "objects. Delete the unintended duplicate in GitHub, then retry.")
        if candidates:
            bucket[key] = candidates[0]
            write_json(args.state, state)
            return candidates[0]
    bucket[key] = PENDING
    write_json(args.state, state)
    bucket[key] = create()
    write_json(args.state, state)
    return bucket[key]


def field_key(field):
    return field["name"], field["dataType"]


def index_fields(values):
    result = {}
    for value in values:
        key = field_key(value)
        require(key not in result, f"Ambiguous duplicate field: {key}")
        result[key] = value
    return result


def custom_fields(values):
    return [f for f in values
            if f["dataType"] in WRITABLE and not f.get("isIssueField")]


def iterations(field):
    config = field["configuration"]
    return config["iterations"] + config["completedIterations"]


def iteration_identity(item):
    return item["title"], item["startDate"], item["duration"]


def option_shape(option):
    return {"name": option["name"], "color": option["color"],
            "description": option.get("description") or ""}


def field_definition(field):
    """Comparable destination-independent configuration of a custom field."""
    kind = field["dataType"]
    if kind == "SINGLE_SELECT":
        return [option_shape(o) for o in field["options"]]
    if kind == "MULTI_SELECT":
        return [option_shape(o) for o in field["multiSelectOptions"]]
    if kind == "ITERATION":
        return sorted(list(iteration_identity(i)) for i in iterations(field))
    return None


def field_input(field):
    kind = field["dataType"]
    if kind == "SINGLE_SELECT":
        return {"singleSelectOptions": field_definition(field)}
    if kind == "MULTI_SELECT":
        return {"multiSelectOptions": field_definition(field)}
    if kind == "ITERATION":
        schedule = sorted(iterations(field), key=lambda i: i["startDate"])
        start = (schedule[0]["startDate"] if schedule
                 else datetime.date.today().isoformat())
        return {"iterationConfiguration": {
            "startDate": start, "duration": field["configuration"]["duration"],
            "iterations": [{"title": i["title"], "startDate": i["startDate"],
                            "duration": i["duration"]} for i in schedule],
        }}
    return {}


def sync_fields(api, source, project_id):
    target = index_fields(fields(api, project_id))
    for field in custom_fields(source["fields"]):
        existing = target.get(field_key(field))
        if existing is None:
            require(not any(k[0].lower() == field["name"].lower() for k in target),
                    f"Destination field {field['name']!r} exists with another type.")
            print(f"  Creating field {field['name']} ({field['dataType']})",
                  file=sys.stderr)
            mutation(api, "createProjectV2Field", "CreateProjectV2FieldInput",
                     "projectV2Field { __typename }",
                     dict({"projectId": project_id, "name": field["name"],
                           "dataType": field["dataType"]}, **field_input(field)))
        elif field_definition(existing) != field_definition(field):
            print(f"  Updating field {field['name']} options/iterations",
                  file=sys.stderr)
            mutation(api, "updateProjectV2Field", "UpdateProjectV2FieldInput",
                     "projectV2Field { __typename }",
                     dict({"fieldId": existing["id"]}, **field_input(field)))
    return index_fields(fields(api, project_id))


def rewrite_repos(text, repo_map):
    """Rewrite exact mapped owner/repo names; everything else is left unchanged."""
    text = text or ""
    for source in sorted(repo_map, key=len, reverse=True):
        text = re.sub(r"(?<![\w.-])" + re.escape(source) + r"(?![\w.-])",
                      lambda _match, t=repo_map[source]: t, text, flags=re.I)
    return text


def view_shape(view, repo_map=None):
    names = lambda connection: [
        [n["name"], n["dataType"]] for n in connection["nodes"] if n
    ]
    roadmap = view["layout"] == "ROADMAP_LAYOUT"
    return {
        "name": view["name"], "layout": view["layout"],
        "filter": (rewrite_repos(view.get("filter"), repo_map) if repo_map
                   else view.get("filter") or ""),
        "visible": [] if roadmap else names(view["fields"]),
        "group": names(view["groupByFields"]),
        "vertical": names(view["verticalGroupByFields"]),
        "sort": [[s["field"]["name"], s["field"]["dataType"], s["direction"]]
                 for s in view["sortByFields"]["nodes"] if s and s["field"]],
    }


def view_payload(shape, target_fields):
    by_key = {key: field["databaseId"] for key, field in target_fields.items()}

    def ids(entries):
        result = []
        for entry in entries:
            key = (entry[0], entry[1])
            require(key in by_key,
                    f"View {shape['name']!r} uses field {entry[0]!r} ({entry[1]}), "
                    "which does not exist in the destination project.")
            result.append(by_key[key])
        return result

    payload = {"name": shape["name"], "layout": LAYOUTS[shape["layout"]]}
    if shape["filter"]:
        payload["filter"] = shape["filter"]
    if shape["visible"]:
        payload["visible_fields"] = ids(shape["visible"])
    if shape["sort"]:
        payload["sort_by"] = [[i, s[2].lower()]
                              for i, s in zip(ids(shape["sort"]), shape["sort"])]
    if shape["group"]:
        payload["group_by"] = ids(shape["group"])
    if shape["vertical"]:
        payload["vertical_group_by"] = ids(shape["vertical"])
    return payload


def link_repositories(api, manifest, source, target):
    linked = {r["nameWithOwner"].lower() for r in target["repositories"]["nodes"]}
    for repo in source["repositories"]["nodes"]:
        mapped = manifest["repo_map"].get(repo["nameWithOwner"].lower())
        if not mapped or mapped.lower() in linked:
            continue
        owner, name = mapped.split("/")
        repository = api.graphql(
            "query($o:String!,$n:String!) { repository(owner:$o,name:$n) { id } }",
            o=owner, n=name,
        )["repository"]
        require(repository is not None, f"Destination repository missing: {mapped}")
        print(f"  Linking repository {mapped}", file=sys.stderr)
        mutation(api, "linkProjectV2ToRepository", "LinkProjectV2ToRepositoryInput",
                 "repository { id }",
                 {"projectId": target["id"], "repositoryId": repository["id"]})


def create_projects(api, manifest, state, args):
    check_target_identity(api, manifest, state, args)
    owner = api.graphql(
        "query($org:String!) { organization(login:$org) { id } }",
        org=manifest["target_org"],
    )["organization"]
    require(owner is not None, "Target organisation is inaccessible.")
    repo_map = manifest["repo_map"]
    for source in manifest["projects"]:
        key = str(source["number"])
        entry = state["projects"].get(key)
        if entry and entry.get("create_pending"):
            raise MigrationError(
                f"Creation outcome for project {key} is uncertain. Locate the new "
                f"destination project and use adopt --source-project {key} "
                "--target-project NUMBER --execute. Do not create another blindly."
            )
        if not args.execute:
            print(f"DRY RUN: {'continue' if entry else 'create'} project {key} "
                  f"{source['title']!r}: {len(custom_fields(source['fields']))} "
                  f"custom fields, {len(source['views'])} views, "
                  f"{len(source['repositories']['nodes'])} repository links.")
            for view in source["views"]:
                shape = view_shape(view, repo_map)
                if shape["filter"] != (view.get("filter") or ""):
                    print(f"  View {view['name']!r} filter rewritten to: "
                          f"{shape['filter']}")
            continue
        if entry is None:
            state["projects"][key] = {"create_pending": True}
            write_json(args.state, state)
            try:
                created = mutation(
                    api, "createProjectV2", "CreateProjectV2Input",
                    "projectV2 { id number url }",
                    {"ownerId": owner["id"], "title": source["title"]},
                )["projectV2"]
            except GraphQLError:
                # A GraphQL error response means nothing was created.
                del state["projects"][key]
                write_json(args.state, state)
                raise
            entry = dict(created, default_views=None, views={}, drafts={})
            state["projects"][key] = entry
            write_json(args.state, state)
        if entry.get("default_views") is None:
            entry["default_views"] = [v["id"] for v in views(api, entry["id"])]
            write_json(args.state, state)
        print(f"Building project {key} -> {entry['url']}", file=sys.stderr)
        mutation(
            api, "updateProjectV2", "UpdateProjectV2Input", "projectV2 { id }",
            {"projectId": entry["id"], "public": False,
             "shortDescription": source.get("shortDescription") or "",
             "readme": source.get("readme") or ""},
        )
        target_fields = sync_fields(api, source, entry["id"])
        link_repositories(api, manifest, source,
                          project(api, manifest["target_org"], entry["number"]))
        for view in source["views"]:
            shape = view_shape(view, repo_map)
            mapped = set(entry["views"].values())
            candidates = [v["id"] for v in views(api, entry["id"])
                          if v["name"] == shape["name"] and v["id"] not in mapped
                          and v["id"] not in entry["default_views"]]

            def create_view(shape=shape):
                print(f"  Creating view {shape['name']}", file=sys.stderr)
                return api.request(
                    f"orgs/{manifest['target_org']}/projectsV2/{entry['number']}/views",
                    "POST", view_payload(shape, target_fields),
                    version=VIEWS_REST_VERSION,
                )["node_id"]

            tracked_create(args, state, entry["views"], str(view["number"]),
                           f"view {shape['name']!r}", candidates, create_view)
        present = {v["id"] for v in views(api, entry["id"])}
        for view_id in entry["default_views"]:
            if view_id in present and view_id not in entry["views"].values():
                mutation(api, "deleteProjectV2View", "DeleteProjectV2ViewInput",
                         "projectV2View { id }", {"viewId": view_id})
        entry["default_views"] = []
        write_json(args.state, state)
        require(not project(api, manifest["target_org"], entry["number"])["public"],
                "Could not verify private destination project.")
        print(f"Project {key}: {entry['url']}")


def unique_match(values, description):
    require(len(values) == 1, f"Missing or ambiguous mapping: {description}")
    return values[0]


def mapped_value(value, source_field, target_field):
    kind = source_field["dataType"]
    if kind in {"TEXT", "NUMBER", "DATE"}:
        return {kind.lower(): value[kind.lower()]}
    if kind in {"SINGLE_SELECT", "MULTI_SELECT"}:
        option_key = "options" if kind == "SINGLE_SELECT" else "multiSelectOptions"
        names = [value["name"]] if kind == "SINGLE_SELECT" else [
            option["name"] for option in (value["options"] or [])
        ]
        ids = [
            unique_match(
                [option["id"] for option in target_field[option_key]
                 if option["name"] == name], f"{source_field['name']}: {name}",
            ) for name in names
        ]
        return ({"singleSelectOptionId": ids[0]} if kind == "SINGLE_SELECT"
                else {"multiSelectOptionIds": ids})
    require(kind == "ITERATION", f"Unsupported writable field {kind}")
    old = unique_match(
        [item for item in iterations(source_field) if item["id"] == value["iterationId"]],
        f"source iteration {value['iterationId']}",
    )
    target = unique_match(
        [item for item in iterations(target_field)
         if iteration_identity(item) == iteration_identity(old)],
        f"iteration {old['title']} / {old['startDate']} / {old['duration']}",
    )
    return {"iterationId": target["id"]}


def desired_values(source, target_fields):
    values = {field_key(v["field"]): v for v in source["values"] if "field" in v}
    result = []
    for key, target in target_fields.items():
        if target["dataType"] not in WRITABLE or target.get("isIssueField"):
            continue
        value = values.get(key)
        result.append((target, mapped_value(value, value["field"], target)
                       if value is not None else None))
    return result


def draft_identity(content):
    return content["title"], content.get("body") or ""


def project_plan(api, manifest, state, resolved):
    plans = []
    for source in manifest["projects"]:
        entry = state["projects"].get(str(source["number"]))
        require(entry is not None and not entry.get("create_pending")
                and entry.get("default_views") == [],
                f"Project {source['number']} is not fully created; run create "
                "(or adopt then create) first.")
        target = project(api, manifest["target_org"], entry["number"])
        require(target["id"] == entry["id"], "Target project identity changed.")
        require(not target["public"],
                "Destination project is public. Run create --execute to enforce "
                "private visibility before restoration.")
        target_fields = index_fields(fields(api, target["id"]))
        expected = {field_key(f): field_definition(f)
                    for f in custom_fields(source["fields"])}
        actual = {field_key(f): field_definition(f)
                  for f in custom_fields(target_fields.values())}
        require(expected == actual,
                f"Project {source['number']}: destination custom fields, options or "
                "iterations differ from the export. Rerun create or fix the project.")
        require([view_shape(v) for v in views(api, target["id"])] ==
                [view_shape(v, manifest["repo_map"]) for v in source["views"]],
                f"Project {source['number']}: view names/layouts/filters/fields/"
                "grouping/sorting differ. Do not edit views before verification.")
        existing, drafts = {}, {}
        for item in items(api, target["id"]):
            content = item["content"]
            require(content is not None, "Target has inaccessible project content.")
            if content["__typename"] == "DraftIssue":
                drafts[item["id"]] = item
                continue
            key = ref(content["repository"]["nameWithOwner"], content["number"])
            require(key not in existing, f"Duplicate target project item: {key}")
            existing[key] = item
        mapped_drafts = {v for v in entry["drafts"].values() if v != PENDING}
        for source_id, target_id in entry["drafts"].items():
            require(target_id == PENDING or target_id in drafts,
                    f"Restored draft item {target_id} is missing from the destination.")
        pending_identities = {
            draft_identity(i["content"]) for i in source["items"]
            if entry["drafts"].get(i["id"]) == PENDING
        }
        require(all(item_id in mapped_drafts or
                    draft_identity(item["content"]) in pending_identities
                    for item_id, item in drafts.items()),
                f"Project {source['number']} has unexpected draft items; review the "
                "project and remove items not created by this tool.")
        wanted = set()
        planned = []
        for item in source["items"]:
            values = desired_values(item, target_fields)
            if item["content"]["__typename"] == "DraftIssue":
                target_id = entry["drafts"].get(item["id"])
                identity = draft_identity(item["content"])
                planned.append({
                    "kind": "DraftIssue", "source": item, "label": identity[0],
                    "existing": drafts.get(target_id),
                    "candidates": [i for i, d in drafts.items()
                                   if i not in mapped_drafts
                                   and draft_identity(d["content"]) == identity],
                    "values": values,
                })
                continue
            dest = resolved[item["reference"]]
            key = ref(dest["repo"], dest["number"])
            wanted.add(key)
            planned.append({
                "kind": "Content", "source": item, "label": item["reference"],
                "destination": dest, "existing": existing.get(key), "values": values,
            })
        require(set(existing) <= wanted,
                f"Project {source['number']} has unexpected target issue/PR items; "
                "disable auto-add workflows and review the project.")
        target["workflows"] = workflows(api, target["id"])
        plans.append({"source": source, "target": target, "entry": entry,
                      "items": planned})
    return plans


def enabled_workflows(plans):
    return [f"{plan['target']['url']}: {workflow['name']}"
            for plan in plans for workflow in plan["target"]["workflows"]
            if workflow["enabled"]]


def restore(api, manifest, state, args):
    check_target_identity(api, manifest, state, args)
    resolved = preflight(api, manifest)
    plans = project_plan(api, manifest, state, resolved)
    enabled = enabled_workflows(plans)
    if not args.execute:
        print(f"DRY RUN: restore {sum(len(p['items']) for p in plans)} project items "
              f"and {sum(len(h['children']) for h in manifest['hierarchies'])} "
              "sub-issue links. All preflight mappings passed; no remote writes.")
        if enabled:
            print("Disable these destination workflows before --execute:\n  "
                  + "\n  ".join(enabled))
        return
    require(not enabled,
            "Destination project workflows are enabled and could change items "
            "while values are restored. Disable them in the project settings:\n  "
            + "\n  ".join(enabled))
    write_json(args.report, {
        "automated_checks": "not_completed",
        "manual_review_required": manifest["manual_checks"],
    })
    for hierarchy in manifest["hierarchies"]:
        parent = resolved[hierarchy["parent"]]
        endpoint = f"repos/{parent['repo']}/issues/{parent['number']}/sub_issues"
        existing = {item["id"] for item in api.rest_list(endpoint)}
        children = [resolved[key] for key in hierarchy["children"]]
        for child in children:
            if child["id"] not in existing:
                api.request(endpoint, "POST", {"sub_issue_id": child["id"],
                                               "replace_parent": False})
        for previous, child in zip(children, children[1:]):
            api.request(endpoint + "/priority", "PATCH",
                        {"sub_issue_id": child["id"], "after_id": previous["id"]})
    for plan in plans:
        target, entry = plan["target"], plan["entry"]
        # Items are added in source order, preserving relative manual ordering.
        for item in plan["items"]:
            if item["existing"]:
                item_id = item["existing"]["id"]
                if item["existing"]["isArchived"]:
                    mutation(
                        api, "unarchiveProjectV2Item", "UnarchiveProjectV2ItemInput",
                        "item { id }", {"projectId": target["id"], "itemId": item_id},
                    )
            elif item["kind"] == "DraftIssue":
                title, body = draft_identity(item["source"]["content"])
                item_id = tracked_create(
                    args, state, entry["drafts"], item["source"]["id"],
                    f"draft {title!r}", item["candidates"],
                    lambda title=title, body=body: mutation(
                        api, "addProjectV2DraftIssue", "AddProjectV2DraftIssueInput",
                        "projectItem { id }",
                        {"projectId": target["id"], "title": title, "body": body},
                    )["projectItem"]["id"],
                )
            else:
                item_id = mutation(
                    api, "addProjectV2ItemById", "AddProjectV2ItemByIdInput",
                    "item { id }", {"projectId": target["id"],
                                    "contentId": item["destination"]["node_id"]},
                )["item"]["id"]
            for field, value in item["values"]:
                payload = {"projectId": target["id"], "itemId": item_id,
                           "fieldId": field["id"]}
                if value is None:
                    mutation(
                        api, "clearProjectV2ItemFieldValue",
                        "ClearProjectV2ItemFieldValueInput", "projectV2Item { id }",
                        payload,
                    )
                else:
                    mutation(
                        api, "updateProjectV2ItemFieldValue",
                        "UpdateProjectV2ItemFieldValueInput", "projectV2Item { id }",
                        dict(payload, value=value),
                    )
            if item["source"]["isArchived"]:
                mutation(
                    api, "archiveProjectV2Item", "ArchiveProjectV2ItemInput",
                    "item { id }", {"projectId": target["id"], "itemId": item_id},
                )
    verify(api, manifest, state, args)


def verify(api, manifest, state, args):
    write_json(args.report, {
        "automated_checks": "not_completed",
        "manual_review_required": manifest["manual_checks"],
    })
    check_target_identity(api, manifest, state, args)
    resolved = preflight(api, manifest)
    plans = project_plan(api, manifest, state, resolved)
    for hierarchy in manifest["hierarchies"]:
        parent = resolved[hierarchy["parent"]]
        children = api.rest_list(
            f"repos/{parent['repo']}/issues/{parent['number']}/sub_issues"
        )
        require([item["id"] for item in children] ==
                [resolved[key]["id"] for key in hierarchy["children"]],
                f"Sub-issue membership/order mismatch: {hierarchy['parent']}")
    count = 0
    for plan in plans:
        for item in plan["items"]:
            actual = item["existing"]
            require(actual is not None, f"Project item missing: {item['label']}")
            if item["kind"] == "DraftIssue":
                require(draft_identity(actual["content"]) ==
                        draft_identity(item["source"]["content"]),
                        f"Draft title/body mismatch: {item['label']}")
            require(actual["isArchived"] == item["source"]["isArchived"],
                    f"Archive mismatch: {item['label']}")
            actual_values = {field_key(v["field"]): v
                             for v in actual["values"] if "field" in v}
            for field, expected in item["values"]:
                raw = actual_values.get(field_key(field))
                observed = mapped_value(raw, field, field) if raw else None
                require(observed == expected,
                        f"Field mismatch: {item['label']} / {field['name']}")
            count += 1
    projects = manifest["projects"]
    write_json(args.report, {
        "automated_checks": "passed",
        "project_items_verified": count,
        "sub_issue_links_verified": sum(len(h["children"]) for h in manifest["hierarchies"]),
        "destination_projects": {
            k: {"id": v["id"], "number": v["number"], "url": v["url"]}
            for k, v in state["projects"].items()
        },
        "manual_review_required": manifest["manual_checks"],
        "source_workflows": [
            {"source_project": p["number"], "name": w["name"], "enabled": w["enabled"]}
            for p in projects for w in (p["workflows"] or [])
        ],
        "source_workflows_unreadable": [
            p["number"] for p in projects if p["workflows"] is None
        ],
        "source_teams_not_linked": [
            {"source_project": p["number"], "team": t["slug"]}
            for p in projects for t in p["teams"]["nodes"]
        ],
        "source_repository_links_not_mapped": [
            {"source_project": p["number"], "repository": r["nameWithOwner"]}
            for p in projects for r in p["repositories"]["nodes"]
            if r["nameWithOwner"].lower() not in manifest["repo_map"]
        ],
        "source_project_settings_not_applied": [
            {"source_project": p["number"], "closed": p["closed"],
             "template": p["template"], "public": p["public"]}
            for p in projects if p["closed"] or p["template"] or p["public"]
        ],
        "draft_assignees_not_restored": [
            {"source_project": p["number"], "draft": i["content"]["title"],
             "assignees": [a["login"] for a in i["content"]["assignees"]["nodes"]]}
            for p in projects for i in p["items"]
            if i["content"]["__typename"] == "DraftIssue"
            and i["content"].get("assignees", {}).get("nodes")
        ],
        "fields_not_restored_by_this_tool": [
            {"source_project": source["number"], "name": field["name"],
             "data_type": field["dataType"],
             "issue_backed": bool(field.get("isIssueField"))}
            for source in projects for field in source["fields"]
            if field["dataType"] not in WRITABLE or field.get("isIssueField")
        ],
        "not_verified": [
            "Workflow definitions and enabled states",
            "Roadmap date fields/zoom, column widths, field sums, slice-by, insights",
            "Collaborators, team links and draft assignees",
            "Issue-backed fields and other GEI-managed issue metadata",
        ],
    })
    print(f"Automated restoration checks passed. Manual review remains: {args.report}")


def adopt(api, manifest, state, args):
    check_target_identity(api, manifest, state, args)
    source = next((p for p in manifest["projects"]
                   if p["number"] == args.source_project), None)
    require(source is not None, "Source project is not in the manifest.")
    target = project(api, manifest["target_org"], args.target_project)
    key = str(args.source_project)
    old = state["projects"].get(key)
    require(not old or old.get("create_pending") or old.get("id") == target["id"],
            "State already points to a different target project.")
    require(all(p.get("id") != target["id"] for k, p in state["projects"].items()
                if k != key), "Target project already maps to another source.")
    if not old or old.get("create_pending"):
        # create rewrites settings and deletes existing views of an adopted
        # project, so only accept one that looks freshly created.
        require(target["title"] == source["title"],
                f"Target title {target['title']!r} does not match source title "
                f"{source['title']!r}; refusing to adopt.")
        require(not items(api, target["id"]),
                "Target project already has items; refusing to adopt.")
        require(len(views(api, target["id"])) <= 1,
                "Target project has more than one view; refusing to adopt.")
    if args.execute:
        if not old or old.get("create_pending"):
            # Views present at adoption are the auto-created defaults to replace.
            state["projects"][key] = dict(
                {k: target[k] for k in ("id", "number", "url")},
                default_views=None, views={}, drafts={},
            )
        write_json(args.state, state)
    print(f"{'Adopted' if args.execute else 'DRY RUN: adopt'} {target['url']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between API calls (default: 1).")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser(
        "export", help=f"Read-only source snapshot using {SOURCE_TOKEN_ENV}.")
    exp.add_argument("--source-org", required=True)
    exp.add_argument("--target-org", required=True)
    exp.add_argument("--repo-map", required=True)
    exp.add_argument("--project", action="append", type=int, default=[],
                     help="Source project number; repeat for multiple projects.")
    exp.add_argument("--manifest", required=True)
    for command in ("create", "restore", "verify", "adopt"):
        cmd = sub.add_parser(
            command, help=f"Destination step using {TARGET_TOKEN_ENV}.")
        cmd.add_argument("--manifest", required=True)
        cmd.add_argument("--state", required=True)
        cmd.add_argument("--report", default="migration-report.json")
        if command != "verify":
            cmd.add_argument("--execute", action="store_true",
                             help="Allow destination writes; otherwise read-only dry run.")
        if command == "adopt":
            cmd.add_argument("--source-project", type=int, required=True)
            cmd.add_argument("--target-project", type=int, required=True)
    args = parser.parse_args()
    require(args.delay >= 0, "--delay must not be negative.")
    if args.command == "export":
        export(api_from_env(SOURCE_TOKEN_ENV, args.delay), args)
        return
    manifest = read_json(args.manifest)
    require(manifest.get("version") == MANIFEST_VERSION,
            "Unsupported manifest version; export a new manifest with this tool.")
    state = load_state(args, manifest)
    api = api_from_env(TARGET_TOKEN_ENV, args.delay)
    {"create": create_projects, "restore": restore,
     "verify": verify, "adopt": adopt}[args.command](api, manifest, state, args)


if __name__ == "__main__":
    try:
        main()
    except (MigrationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
