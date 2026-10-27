import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import migrate_gei_extras as m


def field(name="Status", kind="SINGLE_SELECT", id_="F", db=1):
    return {"id": id_, "databaseId": db, "name": name, "dataType": kind,
            "isIssueField": False,
            "options": [{"id": "todo", "name": "Todo", "color": "GRAY",
                         "description": None},
                        {"id": "done", "name": "Done", "color": "GREEN",
                         "description": "Finished"}]}


def view(name="Board", layout="BOARD_LAYOUT", filter_="repo:old/repo is:open"):
    ref = lambda name, kind: {"name": name, "dataType": kind}
    return {
        "id": "V", "number": 1, "name": name, "layout": layout, "filter": filter_,
        "fields": {"nodes": [ref("Title", "TITLE"), ref("Status", "SINGLE_SELECT")]},
        "groupByFields": {"nodes": []},
        "verticalGroupByFields": {"nodes": [ref("Status", "SINGLE_SELECT")]},
        "sortByFields": {"nodes": [{"direction": "DESC",
                                    "field": ref("Title", "TITLE")}]},
    }


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.issue = {
            "id": 20, "node_id": "I_dest", "number": 1, "title": "One",
            "html_url": "https://github.com/new/repo/issues/1",
        }
        self.parent = None
        self.children = []
        self.login = "dest_emu"

    def request(self, endpoint, method="GET", payload=None, paginate=False,
                missing_ok=False, version=m.REST_VERSION):
        self.calls.append((endpoint, method, payload))
        if endpoint.endswith("/parent"):
            return self.parent
        if endpoint.endswith("/pulls/1"):
            return {"node_id": "PR_dest"}
        if method == "GET":
            return deepcopy(self.issue)
        return {}

    def rest_list(self, endpoint):
        return deepcopy(self.children)

    def graphql(self, query, **variables):
        if "viewer" in query:
            return {"viewer": {"login": self.login}}
        return {"organization": {"id": "org"}}


def project_source(**extra):
    return dict({
        "number": 1, "title": "Board", "id": "src", "fields": [field()],
        "views": [view()], "items": [], "shortDescription": None, "readme": None,
        "repositories": {"nodes": [{"nameWithOwner": "old/repo"}]},
        "teams": {"nodes": []}, "workflows": [],
        "closed": False, "template": False, "public": False,
    }, **extra)


class Tests(unittest.TestCase):
    def manifest(self):
        return {
            "version": m.MANIFEST_VERSION, "source_org": "old", "target_org": "new",
            "source_login": "source-user",
            "repo_map": {"old/repo": "new/repo"},
            "references": {"old/repo#1": {"title": "One", "kind": "Issue"}},
            "hierarchies": [], "projects": [], "manual_checks": m.MANUAL_CHECKS,
        }

    # --- credentials -----------------------------------------------------

    def test_selected_token_overrides_ambient_credentials(self):
        result = argparse.Namespace(returncode=0, stdout="{}", stderr="")
        ambient = {"GH_TOKEN": "ambient", "GITHUB_TOKEN": "ambient2"}
        with patch.dict(os.environ, ambient), \
                patch.object(m.subprocess, "run", return_value=result) as run:
            m.API("target-secret", 0).request("user")
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["GH_TOKEN"], "target-secret")
            self.assertNotIn("GITHUB_TOKEN", env)
            self.assertNotIn("target-secret", " ".join(run.call_args.args[0]))

    def test_missing_token_env_is_reported(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(m.MigrationError, m.TARGET_TOKEN_ENV):
                m.api_from_env(m.TARGET_TOKEN_ENV, 0)

    def test_target_identity_must_differ_from_source_and_stay_stable(self):
        api, manifest = FakeAPI(), self.manifest()
        args = argparse.Namespace(execute=False)
        api.login = "Source-User"
        with self.assertRaisesRegex(m.MigrationError, "source account"):
            m.check_target_identity(api, manifest, {}, args)
        api.login = "other_emu"
        with self.assertRaisesRegex(m.MigrationError, "destination identity"):
            m.check_target_identity(api, manifest, {"target_login": "dest_emu"}, args)
        with tempfile.TemporaryDirectory() as directory:
            state = {"target_login": None}
            args = argparse.Namespace(execute=True,
                                      state=str(Path(directory) / "s.json"))
            m.check_target_identity(api, manifest, state, args)
            self.assertEqual(m.read_json(args.state)["target_login"], "other_emu")

    # --- field and view reconstruction ------------------------------------

    def test_field_input_preserves_options_and_iteration_breaks(self):
        self.assertEqual(m.field_input(field())["singleSelectOptions"], [
            {"name": "Todo", "color": "GRAY", "description": ""},
            {"name": "Done", "color": "GREEN", "description": "Finished"},
        ])
        sprint = field("Sprint", "ITERATION")
        sprint["configuration"] = {"duration": 14, "startDay": 1, "iterations": [
            {"id": "b", "title": "S2", "startDate": "2026-02-01", "duration": 14}],
            "completedIterations": [
            {"id": "a", "title": "S1", "startDate": "2026-01-01", "duration": 7}]}
        config = m.field_input(sprint)["iterationConfiguration"]
        self.assertEqual(config["startDate"], "2026-01-01")
        self.assertEqual([i["title"] for i in config["iterations"]], ["S1", "S2"])
        self.assertNotIn("id", config["iterations"][0])

    def test_sync_fields_creates_missing_and_updates_default_status(self):
        source = {"fields": [field(), field("Estimate", "NUMBER"),
                             dict(field("Shared", "TEXT"), isIssueField=True)]}
        default_status = field()
        default_status["options"] = [{"id": "x", "name": "Todo", "color": "GRAY",
                                      "description": ""}]
        with patch.object(m, "fields", return_value=[default_status]), \
                patch.object(m, "mutation") as mutation:
            m.sync_fields(None, source, "P")
        names = [c.args[1] for c in mutation.call_args_list]
        self.assertEqual(names, ["updateProjectV2Field", "createProjectV2Field"])
        self.assertEqual(mutation.call_args_list[1].args[-1]["name"], "Estimate")

    def test_sync_fields_refuses_type_conflict(self):
        with patch.object(m, "fields", return_value=[field("Estimate", "TEXT")]):
            with self.assertRaisesRegex(m.MigrationError, "another type"):
                m.sync_fields(None, {"fields": [field("Estimate", "NUMBER")]}, "P")

    def test_filter_rewrites_only_exact_mapped_repositories(self):
        repo_map = {"old/repo": "new/repo"}
        self.assertEqual(
            m.rewrite_repos("repo:OLD/repo repo:old/repo-two old/repo2", repo_map),
            "repo:new/repo repo:old/repo-two old/repo2")
        self.assertEqual(m.rewrite_repos(None, repo_map), "")

    def test_view_payload_maps_field_database_ids(self):
        targets = m.index_fields([
            {"name": "Title", "dataType": "TITLE", "databaseId": 10},
            field(db=20),
        ])
        shape = m.view_shape(view(), {"old/repo": "new/repo"})
        self.assertEqual(m.view_payload(shape, targets), {
            "name": "Board", "layout": "board", "filter": "repo:new/repo is:open",
            "visible_fields": [10, 20], "sort_by": [[10, "desc"]],
            "vertical_group_by": [20],
        })
        roadmap = m.view_shape(view(layout="ROADMAP_LAYOUT"))
        self.assertNotIn("visible_fields", m.view_payload(roadmap, targets))
        with self.assertRaisesRegex(m.MigrationError, "does not exist"):
            m.view_payload(shape, m.index_fields([field(db=20)]))

    # --- value mapping (unchanged behaviour) -------------------------------

    def test_single_select_maps_names_not_source_ids(self):
        old, target = field(), field(id_="target")
        target["options"][1]["id"] = "new_done"
        self.assertEqual(
            m.mapped_value({"name": "Done"}, old, target),
            {"singleSelectOptionId": "new_done"},
        )

    def test_multi_select(self):
        old = field("Team", "MULTI_SELECT")
        new = dict(old, multiSelectOptions=[{"id": "b", "name": "Blue"}])
        self.assertEqual(
            m.mapped_value({"options": [{"name": "Blue"}]}, old, new),
            {"multiSelectOptionIds": ["b"]},
        )

    def test_zero_and_empty_text_not_dropped(self):
        for kind, value in [("NUMBER", 0), ("TEXT", ""), ("DATE", "2026-10-23")]:
            definition = field("Value", kind)
            data = {kind.lower(): value}
            self.assertEqual(m.mapped_value(data, definition, definition), data)

    def test_iteration_maps_completed_schedule(self):
        old = field("Sprint", "ITERATION")
        iteration = {"id": "old", "title": "Sprint 1",
                     "startDate": "2026-01-01", "duration": 14}
        old["configuration"] = {"iterations": [], "completedIterations": [iteration]}
        new = deepcopy(old)
        new["configuration"]["completedIterations"][0]["id"] = "new"
        self.assertEqual(
            m.mapped_value({"iterationId": "old"}, old, new), {"iterationId": "new"}
        )
        new["configuration"]["completedIterations"][0]["startDate"] = "2027-01-01"
        with self.assertRaises(m.MigrationError):
            m.mapped_value({"iterationId": "old"}, old, new)

    def test_ambiguous_option_fails(self):
        old = field()
        old["options"].append({"id": "another", "name": "Done"})
        with self.assertRaises(m.MigrationError):
            m.mapped_value({"name": "Done"}, old, old)

    def test_unset_fields_cleared_and_shared_issue_fields_not_mutated(self):
        shared = dict(field("Shared", "TEXT"), isIssueField=True)
        definitions = m.index_fields([field(), shared])
        self.assertEqual(m.desired_values({"values": []}, definitions),
                         [(field(), None)])

    # --- destination identity and hierarchy --------------------------------

    def test_destination_identity_checks_and_redirect(self):
        api, manifest = FakeAPI(), self.manifest()
        self.assertEqual(m.destination(api, manifest, "old/repo#1")["id"], 20)
        api.issue["title"] = "Wrong"
        with self.assertRaises(m.MigrationError):
            m.destination(api, manifest, "old/repo#1")
        api.issue["title"] = "One"
        api.issue["html_url"] = "https://github.com/other/repo/issues/1"
        with self.assertRaises(m.MigrationError):
            m.destination(api, manifest, "old/repo#1")

    def test_pr_gets_pull_request_node_id(self):
        api, manifest = FakeAPI(), self.manifest()
        api.issue["pull_request"] = {}
        manifest["references"]["old/repo#1"]["kind"] = "PullRequest"
        self.assertEqual(m.destination(api, manifest, "old/repo#1")["node_id"],
                         "PR_dest")

    def test_parent_conflict_and_extra_children_block(self):
        api, manifest = FakeAPI(), self.manifest()
        manifest["hierarchies"] = [{"parent": "old/repo#1", "children": ["old/repo#1"]}]
        api.parent = {"url": "https://api.github.com/repos/new/repo/issues/999"}
        with self.assertRaisesRegex(m.MigrationError, "another destination parent"):
            m.preflight(api, manifest)
        api.parent = None
        api.children = [{"id": 999}]
        with self.assertRaisesRegex(m.MigrationError, "extra sub-issues"):
            m.preflight(api, manifest)

    def test_parent_checked_even_without_summary_property(self):
        api, manifest = FakeAPI(), self.manifest()
        manifest["hierarchies"] = [{"parent": "old/repo#1", "children": ["old/repo#1"]}]
        m.preflight(api, manifest)
        self.assertTrue(any(call[0].endswith("/parent") for call in api.calls))

    # --- state and API plumbing ---------------------------------------------

    def test_manifest_bound_state(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(state=str(Path(directory) / "state.json"))
            state = m.load_state(args, self.manifest())
            m.write_json(args.state, state)
            manifest = self.manifest()
            manifest["target_org"] = "elsewhere"
            with self.assertRaisesRegex(m.MigrationError, "another manifest"):
                m.load_state(args, manifest)
            self.assertEqual(Path(args.state).stat().st_mode & 0o777, 0o600)

    def test_rest_and_graphql_pagination(self):
        api = m.API("t", 0)
        with patch.object(api, "request", return_value=[[1, 2], [3]]) as request:
            self.assertEqual(api.rest_list("repos/a/b/issues?state=all"), [1, 2, 3])
            self.assertTrue(request.call_args.kwargs["paginate"])
        pages = [
            {"node": {"items": {"nodes": [1], "pageInfo": {
                "hasNextPage": True, "endCursor": "next"}}}},
            {"node": {"items": {"nodes": [2], "pageInfo": {
                "hasNextPage": False, "endCursor": None}}}},
        ]
        with patch.object(api, "graphql", side_effect=pages) as graphql:
            self.assertEqual(api.pages("query", ["node", "items"]), [1, 2])
            self.assertEqual(graphql.call_args.kwargs["cursor"], "next")

    def test_api_errors_and_no_blind_retries(self):
        result = argparse.Namespace(returncode=1, stdout='{"status":"404"}',
                                    stderr="not found")
        with patch.object(m.subprocess, "run", return_value=result) as run:
            api = m.API("t", 0)
            self.assertIsNone(api.request("parent", missing_ok=True))
            with self.assertRaises(m.MigrationError):
                api.request("other")
            self.assertEqual(run.call_count, 2)
        result.returncode, result.stdout = 0, '{"errors":[{"message":"denied"}]}'
        with patch.object(m.subprocess, "run", return_value=result):
            with self.assertRaises(m.MigrationError):
                m.API("t", 0).graphql("query")

    def test_tracked_create_recovers_unique_pending_and_blocks_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(state=str(Path(directory) / "s.json"))
            state = {"bucket": {"a": m.PENDING}}
            create = unittest.mock.Mock(return_value="NEW")
            self.assertEqual(m.tracked_create(args, state, state["bucket"], "a",
                                              "x", ["FOUND"], create), "FOUND")
            create.assert_not_called()
            state["bucket"]["b"] = m.PENDING
            with self.assertRaisesRegex(m.MigrationError, "several"):
                m.tracked_create(args, state, state["bucket"], "b", "x",
                                 ["1", "2"], create)
            self.assertEqual(m.tracked_create(args, state, state["bucket"], "c",
                                              "x", [], create), "NEW")
            self.assertEqual(m.read_json(args.state)["bucket"]["c"], "NEW")

    # --- create ------------------------------------------------------------------

    def test_create_dry_run_does_not_mutate_or_persist(self):
        manifest = self.manifest()
        manifest["projects"] = [project_source()]
        state = {"projects": {}}
        args = argparse.Namespace(execute=False, state="must-not-write.json")
        with patch.object(m, "mutation") as mutate, \
                patch.object(m, "write_json") as write:
            m.create_projects(FakeAPI(), manifest, state, args)
            mutate.assert_not_called()
            write.assert_not_called()
            self.assertEqual(state["projects"], {})

    def test_uncertain_create_cannot_duplicate(self):
        manifest = self.manifest()
        manifest["projects"] = [project_source()]
        state = {"target_login": "dest_emu",
                 "projects": {"1": {"create_pending": True}}}
        with patch.object(m, "mutation") as mutate:
            with self.assertRaisesRegex(m.MigrationError, "uncertain"):
                m.create_projects(FakeAPI(), manifest, state,
                                  argparse.Namespace(execute=True))
            mutate.assert_not_called()

    def test_rejected_create_clears_pending_marker(self):
        manifest = self.manifest()
        manifest["projects"] = [project_source()]
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(execute=True,
                                      state=str(Path(directory) / "s.json"))
            state = {"target_login": "dest_emu", "projects": {}}
            with patch.object(m, "mutation",
                              side_effect=m.GraphQLError("FORBIDDEN")):
                with self.assertRaises(m.GraphQLError):
                    m.create_projects(FakeAPI(), manifest, state, args)
            self.assertEqual(m.read_json(args.state)["projects"], {})
            with patch.object(m, "mutation",
                              side_effect=m.MigrationError("transport")):
                with self.assertRaises(m.MigrationError):
                    m.create_projects(FakeAPI(), manifest, state, args)
            self.assertEqual(m.read_json(args.state)["projects"],
                             {"1": {"create_pending": True}})

    def test_adopt_refuses_project_that_is_not_fresh(self):
        manifest = self.manifest()
        manifest["projects"] = [project_source()]
        args = argparse.Namespace(execute=False, source_project=1,
                                  target_project=9)
        target = {"id": "P", "number": 9, "url": "u", "title": "Board"}
        state = {"target_login": "dest_emu", "projects": {}}
        cases = [(dict(target, title="Other"), [], ["V"], "title"),
                 (target, [{"id": "i"}], ["V"], "items"),
                 (target, [], ["V", "W"], "more than one view")]
        for project, item_list, view_list, message in cases:
            with patch.object(m, "project", return_value=project), \
                    patch.object(m, "items", return_value=item_list), \
                    patch.object(m, "views", return_value=view_list):
                with self.assertRaisesRegex(m.MigrationError, message):
                    m.adopt(FakeAPI(), manifest, state, args)
        with patch.object(m, "project", return_value=target), \
                patch.object(m, "items", return_value=[]), \
                patch.object(m, "views", return_value=["V"]):
            m.adopt(FakeAPI(), manifest, state, args)

    def test_create_builds_project_views_and_removes_default_view(self):
        manifest = self.manifest()
        manifest["projects"] = [project_source()]
        target_fields = [{"id": "T", "databaseId": 10, "name": "Title",
                          "dataType": "TITLE"}, field(db=20)]
        default = dict(view("View 1", "TABLE_LAYOUT", ""), id="DEFAULT")
        created_view = dict(view(), id="NEWVIEW")
        view_lists = [[default], [default], [default, created_view]]
        api = FakeAPI()
        api.request = lambda *a, **kw: (api.calls.append((a, kw))
                                        or {"node_id": "NEWVIEW"})
        target = {"public": False, "repositories": {"nodes": []}, "id": "P"}
        def mutate(_api, name, *_):
            return {"projectV2": {"id": "P", "number": 7, "url": "u"}}
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(execute=True,
                                      state=str(Path(directory) / "s.json"))
            state = {"projects": {}}
            with patch.object(m, "mutation", side_effect=mutate) as mutation, \
                    patch.object(m, "views", side_effect=view_lists), \
                    patch.object(m, "fields", return_value=target_fields), \
                    patch.object(m, "project", return_value=target), \
                    patch.object(m, "link_repositories"):
                m.create_projects(api, manifest, state, args)
            names = [c.args[1] for c in mutation.call_args_list]
            self.assertEqual(names, ["createProjectV2", "updateProjectV2",
                                     "deleteProjectV2View"])
            self.assertIs(mutation.call_args_list[1].args[-1]["public"], False)
            (endpoint, _, payload), kwargs = api.calls[-1]
            self.assertEqual(endpoint, "orgs/new/projectsV2/7/views")
            self.assertEqual(kwargs["version"], m.VIEWS_REST_VERSION)
            self.assertEqual(payload["filter"], "repo:new/repo is:open")
            entry = m.read_json(args.state)["projects"]["1"]
            self.assertEqual(entry["views"], {"1": "NEWVIEW"})
            self.assertEqual(entry["default_views"], [])

    # --- restore and verify --------------------------------------------------

    def test_snapshot_adds_consistent_source_references(self):
        content = {"__typename": "Issue", "number": 1, "title": "One",
                   "repository": {"nameWithOwner": "OLD/Repo"}}
        with patch.object(m, "project", return_value={"id": "P"}), \
                patch.object(m, "fields", return_value=[]), \
                patch.object(m, "items", return_value=[{"content": content}]), \
                patch.object(m, "workflows", return_value=None), \
                patch.object(m, "views", return_value=[]):
            self.assertEqual(m.snapshot(None, "old", 1)["items"][0]["reference"],
                             "old/repo#1")

    def restore_args(self):
        return argparse.Namespace(execute=True, report="unused.json",
                                  state="unused.json")

    def test_restore_sibling_order_and_safe_link(self):
        manifest = self.manifest()
        manifest["hierarchies"] = [{
            "parent": "parent", "children": ["first", "second", "third"],
        }]
        resolved = {
            key: {"repo": "new/repo", "number": index, "id": index}
            for index, key in enumerate(["parent", "first", "second", "third"], 1)
        }
        api = FakeAPI()
        api.children = [{"id": 2}]
        with patch.object(m, "preflight", return_value=resolved), \
                patch.object(m, "project_plan", return_value=[]), \
                patch.object(m, "write_json"), \
                patch.object(m, "verify") as verify:
            m.restore(api, manifest, {}, self.restore_args())
            posts = [call[2] for call in api.calls if call[1] == "POST"]
            self.assertEqual(posts, [{"sub_issue_id": 3, "replace_parent": False},
                                     {"sub_issue_id": 4, "replace_parent": False}])
            patches = [call[2] for call in api.calls if call[1] == "PATCH"]
            self.assertEqual(patches, [{"sub_issue_id": 3, "after_id": 2},
                                       {"sub_issue_id": 4, "after_id": 3}])
            verify.assert_called_once()

    def test_restore_refuses_enabled_workflows_before_writes(self):
        plan = [{"target": {"url": "u", "workflows": [
            {"name": "Item closed", "enabled": True}]}, "items": []}]
        api = FakeAPI()
        with patch.object(m, "preflight", return_value={}), \
                patch.object(m, "project_plan", return_value=plan), \
                patch.object(m, "write_json") as write:
            with self.assertRaisesRegex(m.MigrationError, "Item closed"):
                m.restore(api, self.manifest(), {"target_login": "dest_emu"},
                          self.restore_args())
            self.assertEqual(api.calls, [])
            write.assert_not_called()

    def test_project_values_drafts_and_archive_write_payloads(self):
        manifest, api = self.manifest(), FakeAPI()
        definition = field("Estimate", "NUMBER")
        draft = {"id": "SRC_DRAFT", "isArchived": False,
                 "content": {"__typename": "DraftIssue", "title": "Idea",
                             "body": "Text"}}
        entry = {"drafts": {}}
        plan = [{
            "target": {"id": "PROJECT", "workflows": []},
            "entry": entry,
            "items": [{
                "kind": "DraftIssue", "source": draft, "existing": None,
                "candidates": [], "values": [(definition, {"number": 1})],
            }, {
                "kind": "Content", "source": {"isArchived": True},
                "destination": {"node_id": "ISSUE"}, "existing": None,
                "key": "new/repo#1",
                "values": [(definition, {"number": 0}), (field(), None)],
            }],
        }]
        def mutate(_api, name, input_type, selection, payload):
            return {"item": {"id": "ITEM"}, "projectItem": {"id": "DRAFT"}}
        with patch.object(m, "preflight", return_value={}), \
                patch.object(m, "project_plan", return_value=plan), \
                patch.object(m, "remove_auto_added"), \
                patch.object(m, "verify"), \
                patch.object(m, "write_json"), \
                patch.object(m, "mutation", side_effect=mutate) as mutation:
            m.restore(api, manifest, {}, self.restore_args())
            self.assertEqual([c.args[1] for c in mutation.call_args_list], [
                "addProjectV2DraftIssue", "updateProjectV2ItemFieldValue",
                "addProjectV2ItemById", "updateProjectV2ItemFieldValue",
                "clearProjectV2ItemFieldValue", "archiveProjectV2Item",
            ])
            self.assertEqual(mutation.call_args_list[0].args[-1]["body"], "Text")
            self.assertEqual(mutation.call_args_list[3].args[-1]["value"], {"number": 0})
            self.assertEqual(entry["drafts"], {"SRC_DRAFT": "DRAFT"})

    def test_descendants_follow_restored_hierarchy(self):
        manifest = {"hierarchies": [
            {"parent": "old/a#1", "children": ["old/a#2", "old/b#1"]},
            {"parent": "old/a#2", "children": ["old/a#3"]},
            {"parent": "old/a#9", "children": ["old/a#8"]},
        ]}
        resolved = {k: {"repo": k.split("#")[0].replace("old/", "new/"),
                        "number": int(k.split("#")[1])}
                    for k in ["old/a#1", "old/a#2", "old/a#3", "old/b#1",
                              "old/a#8", "old/a#9"]}
        self.assertEqual(m.descendant_refs(manifest, resolved, {"new/a#1"}),
                         {"new/a#2", "new/a#3", "new/b#1"})

    def test_add_item_reuses_auto_added_item(self):
        api = FakeAPI()
        api.graphql = lambda q, **v: {"node": {"projectItems": {"nodes": [
            {"id": "OTHER", "project": {"id": "ELSEWHERE"}},
            {"id": "EXISTING", "project": {"id": "P"}}]}}}
        with patch.object(m, "mutation", side_effect=m.GraphQLError(
                "Content already exists in this project")):
            self.assertEqual(m.add_item(api, "P", "ISSUE"), "EXISTING")
        with patch.object(m, "mutation", side_effect=m.GraphQLError("FORBIDDEN")):
            with self.assertRaises(m.GraphQLError):
                m.add_item(api, "P", "ISSUE")

    def test_restore_links_sub_issues_after_items_then_sweeps(self):
        manifest, api = self.manifest(), FakeAPI()
        manifest["hierarchies"] = [{"parent": "old/repo#1", "children": []}]
        order = []
        api.rest_list = lambda endpoint: order.append("hierarchy") or []
        plan = [{"target": {"id": "P", "workflows": []}, "entry": {"drafts": {}},
                 "items": [{"kind": "Content", "source": {"isArchived": False},
                            "destination": {"node_id": "I"}, "existing": None,
                            "key": "new/repo#1", "values": []}]}]
        with patch.object(m, "preflight",
                          return_value={"old/repo#1": {"repo": "new/repo",
                                                       "number": 1}}), \
                patch.object(m, "project_plan", return_value=plan), \
                patch.object(m, "add_item",
                             side_effect=lambda *a: order.append("item") or "X"), \
                patch.object(m, "remove_auto_added",
                             side_effect=lambda *a: order.append("sweep")), \
                patch.object(m, "verify"), patch.object(m, "write_json"):
            m.restore(api, manifest, {}, self.restore_args())
        self.assertEqual(order, ["item", "hierarchy", "sweep"])

    def test_remove_auto_added_only_deletes_unwanted_descendants(self):
        manifest = {"hierarchies": [
            {"parent": "old/repo#1", "children": ["old/repo#2"]}]}
        resolved = {"old/repo#1": {"repo": "new/repo", "number": 1},
                    "old/repo#2": {"repo": "new/repo", "number": 2}}
        plan = [{"target": {"id": "P"},
                 "items": [{"kind": "Content", "key": "new/repo#1"}]}]
        present = {"new/repo#1": {"id": "KEEP"}, "new/repo#2": {"id": "AUTO"},
                   "new/repo#7": {"id": "OTHER"}}
        with patch.object(m, "content_items", return_value=present), \
                patch.object(m, "mutation") as mutation:
            m.remove_auto_added(FakeAPI(), manifest, resolved, plan)
        self.assertEqual([c.args[1] for c in mutation.call_args_list],
                         ["deleteProjectV2Item"])
        self.assertEqual(mutation.call_args.args[-1]["itemId"], "AUTO")

    def test_verify_does_not_claim_full_fidelity(self):
        manifest = self.manifest()
        manifest["projects"] = [project_source(workflows=[
            {"name": "Auto-add", "enabled": True}])]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "preflight", return_value={}), \
                patch.object(m, "project_plan", return_value=[]):
            args = argparse.Namespace(report=str(Path(directory) / "report.json"))
            m.verify(FakeAPI(), manifest, {"projects": {}}, args)
            report = json.loads(Path(args.report).read_text())
            self.assertEqual(report["automated_checks"], "passed")
            self.assertTrue(report["not_verified"])
            self.assertTrue(report["manual_review_required"])
            self.assertEqual(report["source_workflows"][0]["name"], "Auto-add")

    def test_source_workflows_optional_but_destination_required(self):
        api = FakeAPI()
        api.graphql = unittest.mock.Mock(side_effect=m.MigrationError('"FORBIDDEN"'))
        self.assertIsNone(m.workflows(api, "P", required=False))
        with self.assertRaises(m.MigrationError):
            m.workflows(api, "P")

    def test_failed_verification_does_not_leave_stale_passed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(report=str(Path(directory) / "report.json"))
            m.write_json(args.report, {"automated_checks": "passed"})
            with patch.object(m, "preflight", side_effect=m.MigrationError("failed")):
                with self.assertRaises(m.MigrationError):
                    m.verify(FakeAPI(), self.manifest(), {"projects": {}}, args)
            self.assertEqual(m.read_json(args.report)["automated_checks"], "not_completed")

    def test_export_closed_cross_repo_nested_hierarchy_and_project(self):
        class ExportAPI(FakeAPI):
            def rest_list(self, endpoint):
                data = {
                    "repos/old/a/issues?state=all": [
                        {"number": 1, "title": "Parent", "state": "closed"},
                        {"number": 99, "pull_request": {}},
                    ],
                    "repos/old/a/issues/1/sub_issues": [
                        {"number": 2, "title": "Child",
                         "html_url": "https://github.com/old/b/issues/2"},
                    ],
                    "repos/old/b/issues?state=all": [
                        {"number": 2, "title": "Child"},
                        {"number": 3, "title": "Grandchild"},
                    ],
                    "repos/old/b/issues/2/sub_issues": [
                        {"number": 3, "title": "Grandchild",
                         "html_url": "https://github.com/old/b/issues/3"},
                    ],
                    "repos/old/b/issues/3/sub_issues": [],
                }
                return data[endpoint]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            m.write_json(root / "map.json", {"old/a": "new/a", "old/b": "new/b"})
            args = argparse.Namespace(
                manifest=str(root / "manifest.json"), repo_map=str(root / "map.json"),
                source_org="old", target_org="new", project=[1],
            )
            source = {"number": 1, "fields": [], "items": [{
                "content": {"__typename": "Issue", "number": 1, "title": "Parent",
                            "repository": {"nameWithOwner": "old/a"}},
            }]}
            api = ExportAPI()
            api.login = "source-user"
            with patch.object(m, "snapshot", return_value=source):
                m.export(api, args)
            result = m.read_json(args.manifest)
            self.assertEqual(result["version"], m.MANIFEST_VERSION)
            self.assertEqual(result["source_login"], "source-user")
            self.assertEqual(len(result["hierarchies"]), 2)
            self.assertEqual(result["hierarchies"][0]["children"], ["old/b#2"])
            self.assertEqual(result["hierarchies"][1]["children"], ["old/b#3"])
            self.assertEqual(result["projects"][0]["items"][0]["reference"], "old/a#1")
            self.assertEqual(len(result["references"]), 3)


if __name__ == "__main__":
    unittest.main()
