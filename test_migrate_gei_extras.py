import argparse
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import migrate_gei_extras as m


def field(name="Status", kind="SINGLE_SELECT", id_="F"):
    return {"id": id_, "name": name, "dataType": kind, "isIssueField": False,
            "options": [{"id": "todo", "name": "Todo"},
                        {"id": "done", "name": "Done"}]}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.issue = {
            "id": 20, "node_id": "I_dest", "number": 1, "title": "One",
            "html_url": "https://github.com/new/repo/issues/1",
        }
        self.parent = None
        self.children = []

    def request(self, endpoint, method="GET", payload=None, paginate=False,
                missing_ok=False):
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


class Tests(unittest.TestCase):
    def manifest(self):
        return {
            "version": 1, "source_org": "old", "target_org": "new",
            "repo_map": {"old/repo": "new/repo"},
            "references": {"old/repo#1": {"title": "One", "kind": "Issue"}},
            "hierarchies": [], "projects": [], "manual_checks": m.MANUAL_CHECKS,
        }

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
        api = m.API(0)
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
            api = m.API(0)
            self.assertIsNone(api.request("parent", missing_ok=True))
            with self.assertRaises(m.MigrationError):
                api.request("other")
            self.assertEqual(run.call_count, 2)
        result.returncode, result.stdout = 0, '{"errors":[{"message":"denied"}]}'
        with patch.object(m.subprocess, "run", return_value=result):
            with self.assertRaises(m.MigrationError):
                m.API(0).graphql("query")

    def test_copy_dry_run_does_not_mutate_or_persist(self):
        manifest = self.manifest()
        source = {"number": 1, "title": "Board", "id": "src"}
        manifest["projects"] = [source]
        state = {"projects": {}}
        api = FakeAPI()
        api.graphql = lambda *a, **kw: {"organization": {"id": "org"}}
        args = argparse.Namespace(execute=False, state="must-not-write.json")
        with patch.object(m, "snapshot", return_value=source), \
                patch.object(m, "mutation") as mutate, \
                patch.object(m, "write_json") as write:
            m.copy_projects(api, manifest, state, args)
            mutate.assert_not_called()
            write.assert_not_called()
            self.assertEqual(state["projects"], {})

    def test_uncertain_copy_cannot_duplicate(self):
        manifest = self.manifest()
        source = {"number": 1, "title": "Board", "id": "src"}
        manifest["projects"] = [source]
        state = {"projects": {"1": {"copy_pending": True}}}
        api = FakeAPI()
        api.graphql = lambda *a, **kw: {"organization": {"id": "org"}}
        with patch.object(m, "snapshot", return_value=source), \
                patch.object(m, "mutation") as mutate:
            with self.assertRaisesRegex(m.MigrationError, "uncertain"):
                m.copy_projects(api, manifest, state,
                                argparse.Namespace(execute=True))
            mutate.assert_not_called()

    def test_snapshot_adds_consistent_source_references(self):
        content = {"__typename": "Issue", "number": 1, "title": "One",
                   "repository": {"nameWithOwner": "OLD/Repo"}}
        with patch.object(m, "project", return_value={"id": "P"}), \
                patch.object(m, "fields", return_value=[]), \
                patch.object(m, "items", return_value=[{"content": content}]), \
                patch.object(m, "views", return_value=[]):
            self.assertEqual(m.snapshot(None, "old", 1)["items"][0]["reference"],
                             "old/repo#1")

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
        args = argparse.Namespace(execute=True, workflows_paused=True, report="unused.json")
        with patch.object(m, "preflight", return_value=resolved), \
                patch.object(m, "project_plan", return_value=[]), \
                patch.object(m, "write_json"), \
                patch.object(m, "verify") as verify:
            m.restore(api, manifest, {}, args)
            posts = [call[2] for call in api.calls if call[1] == "POST"]
            self.assertEqual(posts, [{"sub_issue_id": 3, "replace_parent": False},
                                     {"sub_issue_id": 4, "replace_parent": False}])
            patches = [call[2] for call in api.calls if call[1] == "PATCH"]
            self.assertEqual(patches, [{"sub_issue_id": 3, "after_id": 2},
                                       {"sub_issue_id": 4, "after_id": 3}])
            verify.assert_called_once()

    def test_restore_requires_workflow_acknowledgment_before_writes(self):
        with patch.object(m, "preflight", return_value={}), \
                patch.object(m, "project_plan", return_value=[]):
            api = FakeAPI()
            with self.assertRaisesRegex(m.MigrationError, "Pause destination"):
                m.restore(api, self.manifest(), {},
                          argparse.Namespace(execute=True, workflows_paused=False))
            self.assertEqual(api.calls, [])

    def test_project_values_and_archive_write_payloads(self):
        manifest, api = self.manifest(), FakeAPI()
        definition = field("Estimate", "NUMBER")
        plan = [{
            "target": {"id": "PROJECT"},
            "items": [{
                "source": {"isArchived": True},
                "destination": {"node_id": "ISSUE"},
                "existing": None,
                "values": [(definition, {"number": 0}), (field(), None)],
            }],
        }]
        def mutate(_api, name, input_type, selection, payload):
            return {"item": {"id": "ITEM"}}
        with patch.object(m, "preflight", return_value={}), \
                patch.object(m, "project_plan", return_value=plan), \
                patch.object(m, "verify"), \
                patch.object(m, "write_json"), \
                patch.object(m, "mutation", side_effect=mutate) as mutation:
            m.restore(api, manifest, {},
                      argparse.Namespace(execute=True, workflows_paused=True,
                                         report="unused.json"))
            self.assertEqual([c.args[1] for c in mutation.call_args_list], [
                "addProjectV2ItemById", "updateProjectV2ItemFieldValue",
                "clearProjectV2ItemFieldValue", "archiveProjectV2Item",
            ])
            self.assertEqual(mutation.call_args_list[1].args[-1]["value"], {"number": 0})

    def test_verify_does_not_claim_full_fidelity(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(m, "preflight", return_value={}), \
                patch.object(m, "project_plan", return_value=[]):
            args = argparse.Namespace(report=str(Path(directory) / "report.json"))
            m.verify(FakeAPI(), self.manifest(), {"projects": {}}, args)
            report = json.loads(Path(args.report).read_text())
            self.assertEqual(report["automated_checks"], "passed")
            self.assertTrue(report["not_verified"])
            self.assertTrue(report["manual_review_required"])

    def test_failed_verification_does_not_leave_stale_passed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(report=str(Path(directory) / "report.json"))
            m.write_json(args.report, {"automated_checks": "passed"})
            with patch.object(m, "preflight", side_effect=m.MigrationError("failed")):
                with self.assertRaises(m.MigrationError):
                    m.verify(FakeAPI(), self.manifest(), {"projects": {}}, args)
            self.assertEqual(m.read_json(args.report)["automated_checks"], "not_completed")

    def test_export_closed_cross_repo_nested_hierarchy_and_project(self):
        class ExportAPI:
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
            with patch.object(m, "snapshot", return_value=source):
                m.export(ExportAPI(), args)
            result = m.read_json(args.manifest)
            self.assertEqual(len(result["hierarchies"]), 2)
            self.assertEqual(result["hierarchies"][0]["children"], ["old/b#2"])
            self.assertEqual(result["hierarchies"][1]["children"], ["old/b#3"])
            self.assertEqual(result["projects"][0]["items"][0]["reference"], "old/a#1")
            self.assertEqual(len(result["references"]), 3)


if __name__ == "__main__":
    unittest.main()
