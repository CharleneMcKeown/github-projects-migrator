#!/usr/bin/env python3
"""Restore GitHub Projects and sub-issue links after a github.com GEI migration."""

import argparse
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


FIELD_FRAGMENT = """
__typename
... on ProjectV2Field { id name dataType isIssueField }
... on ProjectV2SingleSelectField {
  id name dataType isIssueField options { id name }
}
... on ProjectV2MultiSelectField {
  id name dataType isIssueField multiSelectOptions { id name }
}
... on ProjectV2IterationField {
  id name dataType
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
WRITABLE = {"TEXT", "NUMBER", "DATE", "SINGLE_SELECT", "MULTI_SELECT", "ITERATION"}
MANUAL_CHECKS = [
    "Recreate auto-add workflows; keep them disabled until restoration is verified.",
    "Review every copied workflow and its enabled state before re-enabling it.",
    "Review view filters, workflow filters, insights, and text references for old "
    "org/repo names; native copying does not constitute reference rewriting.",
    "Recreate collaborators, permissions, team links, and repository links.",
    "Projects are deliberately private. Review visibility and closed/template "
    "settings, description, and README before changing them.",
    "Check iteration schedules, including completed iterations and breaks. "
    "Unmatched assigned iterations block restoration rather than being guessed.",
    "Check draft archive state, draft assignees, and mixed draft/issue item order. "
    "Draft content and field values are handled by native copy, not rewritten.",
    "Check source items hidden by permissions. Null/redacted project content "
    "blocks export; the API cannot prove access to every organisation resource.",
    "Check issue-backed custom fields, issue types, assignees, labels, milestones, "
    "and dependency links separately; this tool does not migrate those values.",
    "Projects are copied live from the source. Keep source projects and issue "
    "hierarchies frozen from export until verification.",
]


class API:
    def __init__(self, delay=1.0):
        self.delay = delay

    def request(self, endpoint, method="GET", payload=None, paginate=False,
                missing_ok=False):
        command = [
            "gh", "api", "--hostname", "github.com", "--method", method,
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28", endpoint,
        ]
        if paginate:
            command += ["--paginate", "--slurp"]
        if payload is not None:
            command += ["--input", "-"]
        time.sleep(self.delay)
        result = subprocess.run(
            command, input=json.dumps(payload) if payload is not None else None,
            text=True, capture_output=True, check=False,
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
            raise MigrationError(json.dumps(result["errors"]))
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


def project(api, org, number):
    result = api.graphql(
        """query($org:String!,$number:Int!) {
          organization(login:$org) {
            id projectV2(number:$number) {
              id number title url public closed shortDescription readme
            }
          }
        }""", org=org, number=number,
    )["organization"]
    require(result is not None and result["projectV2"] is not None,
            f"Project not found or inaccessible: {org}/{number}")
    return result["projectV2"]


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
                  ... on DraftIssue { title body }
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
              nodes { name layout filter }
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
    manifest = {
        "version": 1, "source_org": args.source_org, "target_org": args.target_org,
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
    return {"manifest_sha256": digest, "projects": {}}


def mutation(api, name, input_type, result, value):
    return api.graphql(
        f"mutation($input:{input_type}!) {{ {name}(input:$input) {{ {result} }} }}",
        input=value,
    )[name]


def comparable(value):
    """Ignore iterations moving from current to completed as time passes."""
    if isinstance(value, list):
        return [comparable(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: comparable(item) for key, item in value.items()}
    config = result.get("configuration")
    if isinstance(config, dict) and "completedIterations" in config:
        config["iterations"] = sorted(
            config.pop("iterations") + config.pop("completedIterations"),
            key=lambda item: (item["startDate"], item["id"]),
        )
    return result


def copy_projects(api, manifest, state, args):
    preflight(api, manifest)
    # Native copy reads the source live. Compare before any copy mutations.
    for source in manifest["projects"]:
        live = snapshot(api, manifest["source_org"], source["number"])
        require(comparable(live) == comparable(source),
                f"Project {source['number']} changed after export; re-export to a new "
                "manifest/state before copying.")
    owner = api.graphql(
        "query($org:String!) { organization(login:$org) { id } }",
        org=manifest["target_org"],
    )["organization"]
    require(owner is not None, "Target organisation is inaccessible.")
    for source in manifest["projects"]:
        key = str(source["number"])
        entry = state["projects"].get(key)
        if entry and entry.get("copy_pending"):
            raise MigrationError(
                f"Copy outcome for project {key} is uncertain. Locate the target "
                f"project and use adopt --source-project {key} --target-project NUMBER "
                "--execute. Do not create another copy blindly."
            )
        if entry is None:
            if not args.execute:
                print(f"DRY RUN: native-copy project {key}: {source['title']}")
                continue
            state["projects"][key] = {"copy_pending": True}
            write_json(args.state, state)
            copied = mutation(
                api, "copyProjectV2", "CopyProjectV2Input",
                "projectV2 { id number url }",
                {"projectId": source["id"], "ownerId": owner["id"],
                 "title": source["title"], "includeDraftIssues": True},
            )["projectV2"]
            state["projects"][key] = copied
            write_json(args.state, state)
            entry = copied
        if args.execute:
            mutation(
                api, "updateProjectV2", "UpdateProjectV2Input", "projectV2 { id }",
                {"projectId": entry["id"], "public": False},
            )
            require(not project(api, manifest["target_org"], entry["number"])["public"],
                    "Could not verify private destination project.")
        print(f"Project {key}: {entry['url']}")


def field_key(field):
    return field["name"], field["dataType"]


def index_fields(values):
    result = {}
    for value in values:
        key = field_key(value)
        require(key not in result, f"Ambiguous duplicate field: {key}")
        result[key] = value
    return result


def iterations(field):
    config = field["configuration"]
    return config["iterations"] + config["completedIterations"]


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
    identity = lambda item: (item["title"], item["startDate"], item["duration"])
    target = unique_match(
        [item for item in iterations(target_field) if identity(item) == identity(old)],
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


def project_plan(api, manifest, state, resolved):
    plans = []
    for source in manifest["projects"]:
        entry = state["projects"].get(str(source["number"]))
        require(entry is not None and not entry.get("copy_pending"),
                f"Project {source['number']} is not copied; run copy/adopt first.")
        target = project(api, manifest["target_org"], entry["number"])
        require(target["id"] == entry["id"], "Target project identity changed.")
        require(not target["public"],
                "Destination project is public. Run copy --execute to enforce "
                "private visibility before restoration.")
        source_fields = index_fields(source["fields"])
        target_fields = index_fields(fields(api, target["id"]))
        require(source_fields.keys() == target_fields.keys(),
                f"Project {source['number']}: source/target field names or types differ.")
        same_view = lambda v: dict(v, filter=v.get("filter") or "")
        require([same_view(v) for v in views(api, target["id"])] ==
                [same_view(v) for v in source["views"]],
                f"Project {source['number']}: view names/layouts/filters differ. "
                "Run restoration before manually editing view filters.")
        existing = {}
        for item in items(api, target["id"]):
            content = item["content"]
            require(content is not None, "Target has inaccessible project content.")
            if content["__typename"] == "DraftIssue":
                continue
            key = ref(content["repository"]["nameWithOwner"], content["number"])
            require(key not in existing, f"Duplicate target project item: {key}")
            existing[key] = item
        wanted = set()
        planned = []
        for item in source["items"]:
            if "reference" not in item:
                continue
            dest = resolved[item["reference"]]
            key = ref(dest["repo"], dest["number"])
            wanted.add(key)
            planned.append({
                "source": item, "destination": dest, "existing": existing.get(key),
                "values": desired_values(item, target_fields),
            })
        require(set(existing) <= wanted,
                f"Project {source['number']} has unexpected target issue/PR items; "
                "pause auto-add workflows and review the project.")
        plans.append({"target": target, "items": planned})
    return plans


def restore(api, manifest, state, args):
    resolved = preflight(api, manifest)
    plans = project_plan(api, manifest, state, resolved)
    if not args.execute:
        print(f"DRY RUN: restore {sum(len(p['items']) for p in plans)} issue/PR "
              f"project items and {sum(len(h['children']) for h in manifest['hierarchies'])} "
              "sub-issue links. All preflight mappings passed; no remote writes.")
        return
    require(args.workflows_paused,
            "Pause destination project workflows first, then pass --workflows-paused. "
            "Copied workflows can change issue state while values are restored.")
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
        target = plan["target"]
        for item in plan["items"]:
            if item["existing"]:
                item_id = item["existing"]["id"]
                if item["existing"]["isArchived"]:
                    mutation(
                        api, "unarchiveProjectV2Item", "UnarchiveProjectV2ItemInput",
                        "item { id }", {"projectId": target["id"], "itemId": item_id},
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
            require(actual is not None,
                    f"Project item missing: {item['source']['reference']}")
            require(actual["isArchived"] == item["source"]["isArchived"],
                    f"Archive mismatch: {item['source']['reference']}")
            actual_values = {field_key(v["field"]): v
                             for v in actual["values"] if "field" in v}
            for field, expected in item["values"]:
                raw = actual_values.get(field_key(field))
                observed = mapped_value(raw, field, field) if raw else None
                require(observed == expected,
                        f"Field mismatch: {item['source']['reference']} / {field['name']}")
            count += 1
    write_json(args.report, {
        "automated_checks": "passed",
        "project_issue_pr_items_verified": count,
        "sub_issue_links_verified": sum(len(h["children"]) for h in manifest["hierarchies"]),
        "destination_projects": state["projects"],
        "manual_review_required": manifest["manual_checks"],
        "fields_not_restored_by_this_tool": [
            {"source_project": source["number"], "name": field["name"],
             "data_type": field["dataType"],
             "issue_backed": bool(field.get("isIssueField"))}
            for source in manifest["projects"] for field in source["fields"]
            if field["dataType"] not in WRITABLE or field.get("isIssueField")
        ],
        "not_verified": [
            "Full view configuration, sorting/grouping details and insights",
            "Workflow definitions and enabled states",
            "Draft content/values/assignees/archive/order fidelity",
            "Unassigned iteration schedules and breaks",
            "Issue-backed fields and other GEI-managed issue metadata",
        ],
    })
    print(f"Automated restoration checks passed. Manual review remains: {args.report}")


def adopt(api, manifest, state, args):
    require(any(p["number"] == args.source_project for p in manifest["projects"]),
            "Source project is not in the manifest.")
    target = project(api, manifest["target_org"], args.target_project)
    key = str(args.source_project)
    old = state["projects"].get(key)
    require(not old or old.get("copy_pending") or old.get("id") == target["id"],
            "State already points to a different target project.")
    require(all(p.get("id") != target["id"] for k, p in state["projects"].items()
                if k != key), "Target project already maps to another source.")
    if args.execute:
        state["projects"][key] = {k: target[k] for k in ("id", "number", "url")}
        write_json(args.state, state)
    print(f"{'Adopted' if args.execute else 'DRY RUN: adopt'} {target['url']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between API calls (default: 1).")
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="Read-only source snapshot; no destination writes.")
    exp.add_argument("--source-org", required=True)
    exp.add_argument("--target-org", required=True)
    exp.add_argument("--repo-map", required=True)
    exp.add_argument("--project", action="append", type=int, default=[],
                     help="Source project number; repeat for multiple projects.")
    exp.add_argument("--manifest", required=True)
    for command in ("copy", "restore", "verify", "adopt"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--manifest", required=True)
        cmd.add_argument("--state", required=True)
        cmd.add_argument("--report", default="migration-report.json")
        if command != "verify":
            cmd.add_argument("--execute", action="store_true",
                             help="Allow destination writes; otherwise read-only dry run.")
        if command == "restore":
            cmd.add_argument("--workflows-paused", action="store_true")
        if command == "adopt":
            cmd.add_argument("--source-project", type=int, required=True)
            cmd.add_argument("--target-project", type=int, required=True)
    args = parser.parse_args()
    require(args.delay >= 0, "--delay must not be negative.")
    api = API(args.delay)
    if args.command == "export":
        export(api, args)
        return
    manifest = read_json(args.manifest)
    require(manifest["version"] == 1, "Unsupported manifest version.")
    state = load_state(args, manifest)
    {"copy": copy_projects, "restore": restore,
     "verify": verify, "adopt": adopt}[args.command](api, manifest, state, args)


if __name__ == "__main__":
    try:
        main()
    except (MigrationError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
