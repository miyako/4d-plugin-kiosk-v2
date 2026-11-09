import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / ".github/scripts"))
import release
import source_version as source


@contextlib.contextmanager
def cwd(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def git(path, *args):
    location = ["--git-dir", str(path)] if path.name.endswith(".git") else ["-C", str(path)]
    return subprocess.run(
        ["git", *location, *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


def copy_source(destination):
    for path in source.SOURCE_PATHS:
        (destination / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, destination / path)


def entry(tag="v9.8.7", draft=False):
    return {"id": 42, "tag_name": tag, "draft": draft, "prerelease": False}


class SourceIntegrationTests(unittest.TestCase):
    def test_coordinated_metadata_only_all_modes_live_and_stable_fixtures(self):
        with tempfile.TemporaryDirectory(prefix=".lifecycle-source-", dir=ROOT) as temp:
            root = Path(temp)
            copy_source(root)
            live = source.read(root)
            for current in (live, "3.2.1"):
                parts = list(map(int, current.split(".")))
                for mode, expected in (
                    ("patch", [parts[0], parts[1], parts[2] + 1]),
                    ("minor", [parts[0], parts[1] + 1, 0]),
                    ("major", [parts[0] + 1, 0, 0]),
                ):
                    with self.subTest(current=current, mode=mode):
                        copy_source(root)
                        for path in source.SOURCE_PATHS:
                            target = root / path
                            target.write_bytes(target.read_bytes().replace(live.encode(), current.encode()))
                        self.assertEqual(source.read(root), current)
                        before = {path: (root / path).read_bytes() for path in source.SOURCE_PATHS}
                        excluded = root / "prebuilt/Contents/Info.plist"
                        excluded.parent.mkdir(parents=True, exist_ok=True)
                        excluded.write_bytes(b"prebuilt bundle is not source metadata")
                        new = ".".join(map(str, expected))
                        self.assertEqual(source.bump(mode, root), new)
                        self.assertEqual(source.read(root), new)
                        for path, data in before.items():
                            self.assertEqual((root / path).read_bytes(),
                                             data.replace(current.encode(), new.encode()))
                        self.assertEqual(excluded.read_bytes(), b"prebuilt bundle is not source metadata")


class AvailabilityTests(unittest.TestCase):
    def test_page_two_draft_published_and_successful_full_absence(self):
        for draft in (True, False):
            with self.subTest(draft=draft), patch.object(release, "remote_ref", return_value=None), \
                    patch.object(release, "api", side_effect=[
                        [entry()] * 100, [entry("v1.2.3", draft)]
                    ]) as api:
                with self.assertRaisesRegex(ValueError, "draft or published"):
                    release.available("owner/repo", "v1.2.3")
                self.assertEqual(api.call_args_list[1].args[0],
                                 "repos/owner/repo/releases?per_page=100&page=2")
        with patch.object(release, "remote_ref", return_value=None), \
                patch.object(release, "api", side_effect=[[entry()] * 100, [entry()] * 100, []]) as api:
            release.available("owner/repo", "v1.2.3")
            self.assertEqual(api.call_count, 3)
            self.assertTrue(api.call_args.args[0].endswith("page=3"))

    def test_errors_and_malformed_every_page_fail_closed(self):
        errors = [
            *(subprocess.CalledProcessError(1, ["gh", "api", str(status)])
              for status in (401, 403, 404, 500)),
            OSError("network"), json.JSONDecodeError("bad", "?", 0),
        ]
        bad_pages = [{}, None, "[]", [None], [{}], [dict(entry(), id=True)],
                     [dict(entry(), id=0)], [dict(entry(), tag_name=None)],
                     [dict(entry(), tag_name="")], [dict(entry(), draft="false")],
                     [dict(entry(), prerelease=None)], [entry()] * 101]
        for page in (1, 2, 3):
            for response in errors + bad_pages:
                with self.subTest(page=page, response=response), \
                        patch.object(release, "remote_ref", return_value=None), \
                        patch.object(release, "api", side_effect=(
                            [[entry()] * 100] * (page - 1) + [response])) as api:
                    with self.assertRaises((ValueError, OSError, subprocess.CalledProcessError)):
                        release.available("owner/repo", "v1.2.3")
                    self.assertEqual(api.call_count, page)

    def test_remote_errors_and_malformed_fail_closed(self):
        with patch.object(release, "remote_ref", return_value="a" * 40), \
                patch.object(release, "api") as api:
            with self.assertRaises(ValueError):
                release.available("o/r", "v1.2.3")
            api.assert_not_called()
        for output in ("invalid\trefs/tags/v1.2.3", "a" * 40,
                       "a" * 40 + "\trefs/tags/wrong",
                       ("a" * 40 + "\trefs/tags/v1.2.3\n") * 2):
            with self.subTest(output=output), patch.object(release, "run", return_value=output):
                with self.assertRaises(ValueError):
                    release.remote_ref("refs/tags/v1.2.3")
        with patch.object(release, "run", side_effect=OSError("network")):
            with self.assertRaises(OSError):
                release.available("o/r", "v1.2.3")

    def test_transport_explicit_methods_original_payload_and_no_retry(self):
        with patch.object(release, "run", return_value="[]") as run:
            release.api("repos/o/r/releases?per_page=100&page=2")
            self.assertEqual(run.call_args.args, ("gh", "api", "--method", "GET",
                             "repos/o/r/releases?per_page=100&page=2"))
        with patch.object(release, "run", return_value='{"id": 7}') as run:
            payload = {"tag_name": "v1.2.3", "draft": False}
            release.api("repos/o/r/releases", payload)
            self.assertIn("POST", run.call_args.args)
            self.assertEqual(json.loads(run.call_args.kwargs["input"]), payload)
        for error in (subprocess.CalledProcessError(1, "gh"), OSError("network")):
            with patch.object(release, "run", side_effect=error) as run:
                with self.assertRaises(type(error)):
                    release.api("repos/o/r/releases")
                self.assertEqual(run.call_count, 1)
        with patch.object(release, "run", return_value='{"id": 8}') as run:
            asset = Path("kiosk-1.2.3.zip")
            endpoint = "https://uploads.github.com/repos/o/r/releases/123/assets"
            release.api(endpoint, asset=asset)
            self.assertEqual(run.call_args.args, (
                "gh", "api", "--method", "POST", endpoint,
                "--header", "Content-Type: application/octet-stream", "--input", str(asset)))
        with patch.object(release, "run", return_value="not json"):
            with self.assertRaises(json.JSONDecodeError):
                release.api("repos/o/r/releases")


class PublishValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".lifecycle-responses-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.sha = "a" * 40
        self.paths = [Path(f"kiosk-1.2.3.{ext}") for ext in ("zip", "dmg")]
        for path in self.paths:
            (root / path).write_bytes(b"fixture asset")
        self.enter = cwd(root)
        self.enter.__enter__()
        self.addCleanup(self.enter.__exit__, None, None, None)

    def validate(self, stage, response):
        ref = {"ref": "refs/tags/v1.2.3", "object": {"sha": self.sha}}
        created = {"id": 123, "tag_name": "v1.2.3", "draft": False, "prerelease": False}
        upload = {"id": 456, "name": self.paths[0].name, "size": 13, "state": "uploaded"}
        responses = [ref, created, upload]
        responses[stage] = response
        with patch.object(release, "context", return_value=("owner/repo", "1.2.3", "v1.2.3")), \
                patch.object(release, "available"), \
                patch.object(release, "remote_ref", return_value=self.sha), \
                patch.object(release, "api", side_effect=responses) as api:
            with self.assertRaises(ValueError):
                release.publish(self.sha, self.paths)
            self.assertEqual(api.call_count, stage + 1)

    def test_every_tag_create_response_field_fail_closed(self):
        for response in (None, [], {}, {"ref": "refs/tags/wrong"},
                         {"ref": "refs/tags/v1.2.3", "object": None},
                         {"ref": "refs/tags/v1.2.3", "object": {"sha": "b" * 40}}):
            with self.subTest(response=response):
                self.validate(0, response)

    def test_every_release_create_response_field_fail_closed(self):
        valid = {"id": 123, "tag_name": "v1.2.3", "draft": False, "prerelease": False}
        for response in (
            None, [], {}, *(dict(valid, id=value) for value in (None, True, 0, -1, "123")),
            dict(valid, tag_name="wrong"), dict(valid, draft=True), dict(valid, draft=0),
            dict(valid, prerelease=True), dict(valid, prerelease=0),
        ):
            with self.subTest(response=response):
                self.validate(1, response)

    def test_every_upload_response_field_fail_closed(self):
        valid = {"id": 456, "name": self.paths[0].name, "size": 13, "state": "uploaded"}
        for response in (
            None, [], {}, *(dict(valid, id=value) for value in (None, True, 0, -1, "456")),
            dict(valid, name="wrong.zip"), dict(valid, size=0),
            dict(valid, size="13"), dict(valid, state="new"),
        ):
            with self.subTest(response=response):
                self.validate(2, response)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".lifecycle-git-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.local = self.root / "local"
        self.peer = self.root / "peer"
        git(self.root, "init", "--bare", str(self.remote))
        git(self.root, "init", "-b", "selected", str(self.local))
        for key, value in (("user.name", "Lifecycle Test"), ("user.email", "test@example.invalid"),
                           ("push.followTags", "true")):
            git(self.local, "config", key, value)
        git(self.local, "commit", "--allow-empty", "-m", "ancestor")
        self.ancestor = git(self.local, "rev-parse", "HEAD")
        copy_source(self.local)
        shutil.copytree(ROOT / ".github/scripts", self.local / ".github/scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        git(self.local, "add", ".")
        git(self.local, "commit", "-m", "fixture")
        git(self.local, "remote", "add", "origin", str(self.remote))
        git(self.local, "push", "origin", "HEAD:refs/heads/selected")
        self.sha = git(self.local, "rev-parse", "HEAD")
        self.version = source.read(self.local)
        self.tag = f"v{self.version}"
        self.published = None
        self.uploaded = []
        git(self.root, "clone", "--branch", "selected", str(self.remote), str(self.peer))
        git(self.peer, "config", "user.name", "Peer")
        git(self.peer, "config", "user.email", "peer@example.invalid")
        self.enter = cwd(self.local)
        self.enter.__enter__()
        self.addCleanup(self.enter.__exit__, None, None, None)

    def assets(self):
        paths = [Path(f"kiosk-{self.version}.{ext}") for ext in ("zip", "dmg")]
        for asset in paths:
            asset.write_bytes(b"fixture asset")
        return paths

    def advance_peer(self):
        (self.peer / "peer.txt").write_text("concurrent branch update")
        git(self.peer, "add", ".")
        git(self.peer, "commit", "-m", "peer")
        git(self.peer, "push", "origin", "HEAD:refs/heads/selected")

    def test_bump_only_explicit_branch_source_no_follow_tags(self):
        git(self.local, "tag", "-a", "unrelated-annotated", "-m", "must not follow")
        git(self.remote, "update-ref", "refs/heads/other", self.sha)
        release.bump_branch(self.sha, "selected", "patch")
        new = git(self.local, "rev-parse", "HEAD")
        self.assertNotEqual(new, self.sha)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/selected"), new)
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/other"), self.sha)
        self.assertEqual(git(self.remote, "tag", "--list"), "")
        self.assertEqual(git(self.local, "rev-list", "--parents", "-n", "1", new),
                         f"{new} {self.sha}")
        self.assertEqual(git(self.local, "status", "--porcelain"), "")
        self.assertEqual(
            git(self.local, "log", "-1", "--format=%(trailers:only,unfold)"),
            "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>")
        changed = set(git(self.local, "diff-tree", "--no-commit-id", "--name-only",
                          "-r", "HEAD").splitlines())
        self.assertTrue(changed)
        self.assertTrue(changed.issubset(set(map(str, source.SOURCE_PATHS))))
        self.assertNotEqual(source.read(self.local), self.version)

    def test_cli_bytecode_keeps_clean_checkout(self):
        env = os.environ.copy()
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        subprocess.run(
            [sys.executable, ".github/scripts/release.py", "bump", "--sha", self.sha,
             "--branch", "selected", "--mode", "patch"],
            env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(git(self.local, "status", "--porcelain"), "")
        self.assertEqual(git(self.remote, "tag", "--list"), "")

    def test_bump_postpush_exact_remote_verification_fail_closed(self):
        real_remote_ref = release.remote_ref
        reads = 0

        def wrong_verification(ref):
            nonlocal reads
            result = real_remote_ref(ref)
            reads += 1
            return self.sha if reads == 3 else result

        with patch.object(release, "remote_ref", side_effect=wrong_verification):
            with self.assertRaisesRegex(ValueError, "Pushed branch failed exact-SHA"):
                release.bump_branch(self.sha, "selected", "patch")
        self.assertEqual(reads, 3)
        self.assertEqual(real_remote_ref("refs/heads/selected"), git(self.local, "rev-parse", "HEAD"))

    def test_preparation_races(self):
        self.advance_peer()
        with self.assertRaisesRegex(ValueError, "moved"):
            release.bump_branch(self.sha, "selected", "patch")
        self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.sha)

    def test_race_during_preparation(self):
        real_bump = release.bump

        def concurrent_bump(mode):
            result = real_bump(mode)
            self.advance_peer()
            return result

        with patch.object(release, "bump", side_effect=concurrent_bump):
            with self.assertRaisesRegex(ValueError, "moved during"):
                release.bump_branch(self.sha, "selected", "minor")
        self.assertEqual(release.remote_ref("refs/heads/selected"),
                         git(self.peer, "rev-parse", "HEAD"))

    def advertised_race(self, kind):
        real_run = release.run
        expected = None
        pushes = 0

        def concurrent_push(*args, **kwargs):
            nonlocal expected, pushes
            # Mutate only after the final ls-remote, before receive-pack advertisement.
            if "push" in args:
                pushes += 1
                if kind == "rewind":
                    expected = self.ancestor
                    git(self.remote, "update-ref", "refs/heads/selected", expected)
                elif kind == "delete":
                    git(self.remote, "update-ref", "-d", "refs/heads/selected")
                elif kind == "advance":
                    self.advance_peer()
                    expected = git(self.peer, "rev-parse", "HEAD")
                elif kind == "same-result":
                    expected = git(self.local, "rev-parse", "HEAD")
                    git(self.remote, "fetch", "--no-tags", str(self.local), "HEAD")
                    git(self.remote, "update-ref", "refs/heads/selected", expected)
                else:
                    self.fail(kind)
            return real_run(*args, **kwargs)

        with patch.object(release, "run", side_effect=concurrent_push):
            with self.assertRaises(subprocess.CalledProcessError):
                release.bump_branch(self.sha, "selected", "patch")
        self.assertEqual(pushes, 1)
        self.assertEqual(release.remote_ref("refs/heads/selected"), expected)
        self.assertEqual(git(self.remote, "tag", "--list"), "")

    def test_advertised_rewind(self):
        self.advertised_race("rewind")

    def test_advertised_deletion(self):
        self.advertised_race("delete")

    def test_advertised_advance(self):
        self.advertised_race("advance")

    def test_advertised_same_result(self):
        self.advertised_race("same-result")

    def test_missing_nonexecutable_guard_initially_and_dirty_checkout(self):
        guard = self.local / ".github/scripts/bump-hooks/pre-push"
        guard.chmod(0o644)
        for absent in (False, True):
            if absent:
                guard.unlink()
            with self.subTest(absent=absent), \
                    patch.object(release, "__file__", str(self.local / ".github/scripts/release.py")):
                with self.assertRaisesRegex(ValueError, "guard is missing or not executable"):
                    release.bump_branch(self.sha, "selected", "patch")
            self.assertEqual(git(self.local, "rev-parse", "HEAD"), self.sha)
        with self.assertRaisesRegex(ValueError, "clean checkout"):
            release.bump_branch(self.sha, "selected", "patch")

    def guard_loss_after_final_read(self, missing):
        runtime = self.root / "runtime"
        shutil.copytree(self.local / ".github/scripts", runtime)
        guard = runtime / "bump-hooks/pre-push"
        real_remote_ref = release.remote_ref
        reads = 0

        def lose_guard(ref):
            nonlocal reads
            result = real_remote_ref(ref)
            reads += 1
            if reads == 2:
                if missing:
                    guard.unlink()
                else:
                    guard.chmod(0o644)
            return result

        with patch.object(release, "__file__", str(runtime / "release.py")), \
                patch.object(release, "remote_ref", side_effect=lose_guard), \
                patch.object(release.os, "access", wraps=os.access) as access:
            with self.assertRaisesRegex(ValueError, "guard is missing or not executable"):
                release.bump_branch(self.sha, "selected", "patch")
            self.assertEqual(access.call_count, 1 if missing else 2)
        self.assertEqual(reads, 2)
        self.assertEqual(real_remote_ref("refs/heads/selected"), self.sha)
        self.assertEqual(git(self.remote, "tag", "--list"), "")

    def test_nonexecutable_guard_rechecked_after_final_remote_read(self):
        self.guard_loss_after_final_read(False)

    def test_missing_guard_rechecked_after_final_remote_read(self):
        self.guard_loss_after_final_read(True)

    def test_immutable_invalid_sha_and_branch(self):
        for sha in ("selected", "a" * 40, "a" * 39, "a" * 41):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                release.selected_sha(sha)
        release.selected_sha(self.sha)
        with self.assertRaises(subprocess.CalledProcessError):
            release.bump_branch(self.sha, "invalid..branch", "patch")

    def test_preflight_clean_and_final_source_drift(self):
        path = self.local / source.SOURCE_PATHS[0]
        path.write_bytes(path.read_bytes() + b"\n")
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api") as api:
            with self.assertRaisesRegex(ValueError, "clean source"):
                release.preflight(self.sha)
            api.assert_not_called()
        copy_source(self.local)
        paths = self.assets()
        source.bump("patch")
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api") as api:
            with self.assertRaisesRegex(ValueError, "differs from the selected commit"):
                release.publish(self.sha, paths)
            api.assert_not_called()
        self.assertEqual(git(self.remote, "tag", "--list"), "")

    def test_preflight_immutable_clean_source_and_remote_absence(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", return_value=[]) as api:
            self.assertEqual(release.preflight(self.sha), (self.version, self.tag))
            api.assert_called_once_with("repos/owner/repo/releases?per_page=100&page=1")
        git(self.local, "commit", "--allow-empty", "-m", "different selected checkout")
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api") as api:
            with self.assertRaisesRegex(ValueError, "Checkout does not match"):
                release.preflight(self.sha)
            api.assert_not_called()

    def merge_commit(self):
        tree = git(self.local, "rev-parse", "HEAD^{tree}")
        new = subprocess.run(
            ["git", "commit-tree", tree, "-p", self.sha, "-p", self.ancestor],
            input="merge fixture\n", text=True, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.strip()
        git(self.local, "update-ref", "HEAD", new)
        return new

    def test_bump_rejects_merge_commit_before_push(self):
        real_run = release.run

        def merge_after_commit(*args, **kwargs):
            result = real_run(*args, **kwargs)
            if args[:2] == ("git", "commit"):
                self.merge_commit()
            return result

        with patch.object(release, "run", side_effect=merge_after_commit) as run:
            with self.assertRaisesRegex(ValueError, "single-parent direct child"):
                release.bump_branch(self.sha, "selected", "patch")
            self.assertFalse(any("push" in call.args for call in run.call_args_list))
        self.assertEqual(release.remote_ref("refs/heads/selected"), self.sha)

    def test_bump_rejects_unrelated_committed_changes(self):
        real_run = release.run

        def unrelated_change(*args, **kwargs):
            if args[:2] == ("git", "commit"):
                (self.local / "unrelated.txt").write_text("unexpected commit hook change")
                git(self.local, "add", "unrelated.txt")
            return real_run(*args, **kwargs)

        with patch.object(release, "run", side_effect=unrelated_change) as run:
            with self.assertRaisesRegex(ValueError, "only source version metadata"):
                release.bump_branch(self.sha, "selected", "patch")
            self.assertFalse(any("push" in call.args for call in run.call_args_list))
        self.assertEqual(release.remote_ref("refs/heads/selected"), self.sha)

    def guard(self, new, rows, env=None):
        return subprocess.run(
            [str(ROOT / ".github/scripts/bump-hooks/pre-push")], input=rows,
            env=env if env is not None else {
                **os.environ, "BUMP_EXPECTED_SHA": self.sha,
                "BUMP_EXPECTED_REF": "refs/heads/selected", "BUMP_NEW_SHA": new},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_guard_local_sha_target_parent_and_missing_env(self):
        git(self.local, "commit", "--allow-empty", "-m", "direct child")
        new = git(self.local, "rev-parse", "HEAD")
        valid = f"{new} {new} refs/heads/selected {self.sha}\n"
        self.assertEqual(self.guard(new, valid).returncode, 0)
        for row in (
            f"{new} {new} refs/heads/wrong {self.sha}\n",
            f"HEAD {new} refs/heads/selected {self.sha}\n",
            f"{new} {self.ancestor} refs/heads/selected {self.sha}\n",
            f"{new} {new} refs/heads/selected {self.ancestor}\n",
            valid * 2, "",
        ):
            with self.subTest(row=row):
                self.assertNotEqual(self.guard(new, row).returncode, 0)
        self.assertNotEqual(self.guard(new, valid, env={}).returncode, 0)
        self.assertNotEqual(self.guard(self.ancestor, valid).returncode, 0)

    def test_guard_rejects_multiple_parents_even_with_selected_first_parent(self):
        new = self.merge_commit()
        result = self.guard(new, f"{new} {new} refs/heads/selected {self.sha}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("advertised branch state differs", result.stderr)
        self.assertEqual(release.remote_ref("refs/heads/selected"), self.sha)

    def publication_api(self, calls, fail=None, race=None):
        def api(endpoint, payload=None, asset=None):
            calls.append((endpoint, payload, asset))
            if "/releases?" in endpoint:
                return []
            if endpoint.endswith("/git/refs"):
                if race:
                    git(self.remote, "update-ref", f"refs/tags/{self.tag}", race)
                # Atomic create-only bare Git models POST refs, even same-SHA collisions.
                git(self.remote, "update-ref", payload["ref"], payload["sha"], "0" * 40)
                return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
            if endpoint.endswith("/releases"):
                if fail == "release":
                    raise subprocess.CalledProcessError(1, ["gh", "api"])
                self.published = {"id": 123, "tag_name": self.tag,
                                  "draft": False, "prerelease": False}
                return self.published
            if asset is not None:
                if fail == asset.suffix:
                    raise subprocess.CalledProcessError(1, ["gh", "api"])
                self.uploaded.append(asset.name)
                return {"id": 456, "name": asset.name,
                        "size": asset.stat().st_size, "state": "uploaded"}
            raise AssertionError(endpoint)
        return api

    def test_create_only_sha_new_release_id_original_notes(self):
        calls = []
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", side_effect=self.publication_api(calls)):
            release.publish(self.sha, self.assets())
        self.assertEqual(git(self.remote, "rev-parse", f"refs/tags/{self.tag}"), self.sha)
        self.assertIn("/releases?", calls[0][0])
        self.assertTrue(calls[1][0].endswith("/git/refs"))
        self.assertEqual(calls[1][1], {"ref": f"refs/tags/{self.tag}", "sha": self.sha})
        self.assertEqual(calls[2][1], {
            "tag_name": self.tag, "target_commitish": self.sha, "name": self.tag,
            "draft": False, "prerelease": False, "body": ""})
        self.assertEqual(len(calls), 5)
        for endpoint, payload, asset in calls[3:]:
            self.assertIn("/releases/123/assets?name=", endpoint)
            self.assertIsNone(payload)
            self.assertIsNotNone(asset)

    def tag_race(self, sha):
        calls = []
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", side_effect=self.publication_api(calls, race=sha)):
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish(self.sha, self.assets())
        self.assertEqual(len(calls), 2)
        self.assertEqual(git(self.remote, "rev-parse", f"refs/tags/{self.tag}"), sha)
        self.assertIsNone(self.published)

    def test_same_sha_tag_race(self):
        self.tag_race(self.sha)

    def test_different_sha_tag_race(self):
        self.advance_peer()
        self.tag_race(git(self.peer, "rev-parse", "HEAD"))

    def publication_failure(self, failure, count, uploaded):
        calls = []
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", side_effect=self.publication_api(calls, fail=failure)):
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish(self.sha, self.assets())
        self.assertEqual(len(calls), count)
        self.assertEqual(git(self.remote, "rev-parse", f"refs/tags/{self.tag}"), self.sha)
        if failure == "release":
            self.assertIsNone(self.published)
        else:
            self.assertEqual(self.published["id"], 123)
        self.assertEqual(self.uploaded, uploaded)

    def test_release_post_failure_retains_tag(self):
        self.publication_failure("release", 3, [])

    def test_first_upload_failure_retains_tag_and_release(self):
        self.publication_failure(".zip", 4, [])

    def test_second_upload_failure_retains_tag_release_earlier_asset(self):
        self.publication_failure(".dmg", 5, [f"kiosk-{self.version}.zip"])

    def test_tag_post_failure_does_not_create_release_or_upload(self):
        calls = []

        def failed_tag(endpoint, payload=None, asset=None):
            calls.append((endpoint, payload, asset))
            if "/releases?" in endpoint:
                return []
            raise subprocess.CalledProcessError(1, ["gh", "api", "git/refs"])

        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", side_effect=failed_tag):
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish(self.sha, self.assets())
        self.assertEqual(len(calls), 2)
        self.assertEqual(git(self.remote, "tag", "--list"), "")
        self.assertIsNone(self.published)
        self.assertEqual(self.uploaded, [])

    def test_final_recheck_new_draft(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", return_value=[]):
            release.preflight(self.sha)
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", return_value=[entry(self.tag, True)]) as api:
            with self.assertRaisesRegex(ValueError, "draft or published"):
                release.publish(self.sha, self.assets())
            self.assertEqual(api.call_count, 1)
        self.assertEqual(git(self.remote, "tag", "--list"), "")

    def test_final_recheck_remote_tag_collision(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", return_value=[]):
            release.preflight(self.sha)
        git(self.remote, "update-ref", f"refs/tags/{self.tag}", self.sha)
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api") as api:
            with self.assertRaisesRegex(ValueError, "already exists remotely"):
                release.publish(self.sha, self.assets())
            api.assert_not_called()

    def test_final_later_page_failure_stops_before_creation(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", return_value=[]):
            release.preflight(self.sha)
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", side_effect=[
                    [entry()] * 100, subprocess.CalledProcessError(1, ["gh", "api", "404"])
                ]) as api:
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish(self.sha, self.assets())
            self.assertEqual(api.call_count, 2)
        self.assertEqual(git(self.remote, "tag", "--list"), "")
        self.assertIsNone(self.published)

    def test_remote_verification_blocks_release(self):
        calls = []
        real_remote_ref = release.remote_ref

        def incorrect_verification(ref):
            return "a" * 40 if real_remote_ref(ref) else None

        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api", side_effect=self.publication_api(calls)), \
                patch.object(release, "remote_ref", side_effect=incorrect_verification):
            with self.assertRaisesRegex(ValueError, "exact-SHA verification"):
                release.publish(self.sha, self.assets())
        self.assertEqual(len(calls), 2)
        self.assertIsNone(self.published)
        self.assertEqual(git(self.remote, "rev-parse", f"refs/tags/{self.tag}"), self.sha)

    def test_malformed_post_and_upload_responses_retain_state(self):
        for kind in ("release", "upload", "tag"):
            with self.subTest(kind=kind):
                git(self.remote, "update-ref", "-d", f"refs/tags/{self.tag}")
                calls = []
                real_api = self.publication_api(calls)

                def malformed(endpoint, payload=None, asset=None):
                    result = real_api(endpoint, payload, asset)
                    if (kind == "release" and endpoint.endswith("/releases")) or (
                            kind == "upload" and asset is not None) or (
                            kind == "tag" and endpoint.endswith("/git/refs")):
                        return {"id": None}
                    return result

                with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                        patch.object(release, "api", side_effect=malformed):
                    with self.assertRaises(ValueError):
                        release.publish(self.sha, self.assets())
                self.assertEqual(len(calls), {"tag": 2, "release": 3, "upload": 4}[kind])
                self.assertEqual(git(self.remote, "rev-parse", f"refs/tags/{self.tag}"), self.sha)

    def test_missing_empty_wrong_assets_before_mutation(self):
        for paths in (
            [Path(f"kiosk-{self.version}.{ext}") for ext in ("zip", "dmg")],
            [Path("wrong.zip"), Path("wrong.dmg")],
        ):
            with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                    patch.object(release, "api") as api:
                with self.assertRaises(ValueError):
                    release.publish(self.sha, paths)
                api.assert_not_called()
        paths = self.assets()
        paths[0].write_bytes(b"")
        with patch.dict(os.environ, GITHUB_REPOSITORY="owner/repo"), \
                patch.object(release, "api") as api:
            with self.assertRaises(ValueError):
                release.publish(self.sha, paths)
            api.assert_not_called()


class WiringTests(unittest.TestCase):
    def test_manual_zero_inputs_immutable_native_gate_wiring(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertRegex(workflow, r"(?m)^name: Build$")
        self.assertEqual(workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0].strip(),
                         "workflow_dispatch:")
        self.assertEqual(workflow.count("uses: actions/checkout@v4"), 3)
        self.assertEqual(workflow.count("ref: ${{ github.sha }}"), 3)
        self.assertIn("group: release-publication\n  cancel-in-progress: false", workflow)
        self.assertIn("needs: [version, build-windows]", workflow)
        self.assertIn("needs: version", workflow)
        self.assertEqual(workflow.count("GH_TOKEN: ${{ github.token }}"), 2)
        gates = [
            "release.py preflight", "build x64", "upload windows binaries",
            "download windows binaries", "import Developer ID certificate",
            "store notarytool credentials", "build macOS plugin (universal)",
            "verify universal binary", "merge Windows binaries into the bundle",
            "- name: codesign", "package, notarize & staple", "release.py publish",
        ]
        self.assertEqual([workflow.index(gate) for gate in gates],
                         sorted(workflow.index(gate) for gate in gates))
        for required in ("PRODUCT_NAME: kiosk", "BASE_DIR: test",
                         "MACOS_DEPLOYMENT_TARGET: '11.0'",
                         "PlatformToolset=v143", "WindowsTargetPlatformVersion=10.0",
                         'ARCHS="arm64 x86_64"', "CODE_SIGNING_ALLOWED=NO",
                         'CONFIGURATION_BUILD_DIR="$PWD/$PLUGINS_DIR"',
                         "codesign --verify --deep --strict", 'stapler staple "$DMG_PATH"',
                         'notarytool submit "$1"', 'notarize "$ZIP_PATH"', 'notarize "$DMG_PATH"',
                         "--wait --output-format json", '"$STATUS" != "Accepted"',
                         "APPLE_DEVELOPER_ID_CERTIFICATE", "APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD",
                         "KEYCHAIN_PASSWORD", "NOTARYTOOL_APPLE_ID", "NOTARYTOOL_TEAM_ID",
                         "NOTARYTOOL_PASSWORD", "if-no-files-found: error",
                         "runs-on: windows-latest", "runs-on: macos-latest"):
            self.assertIn(required, workflow)
        for prohibited in ("continue-on-error", "always()", "${{ inputs.", "softprops/",
                           "github.ref_name", "branches:", "REF_TYPE", "refs/heads/master"):
            self.assertNotIn(prohibited, workflow)
        final = workflow.split("release.py publish", 1)[1]
        self.assertIn('--sha "$SELECTED_SHA" "$ZIP_PATH" "$DMG_PATH"', final)
        self.assertNotIn("python3 ", final)

    def test_bump_guarded_source_only_and_no_destructive_publication(self):
        workflow = (ROOT / ".github/workflows/bump-version.yml").read_text()
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("SELECTED_BRANCH: ${{ github.ref_name }}", workflow)
        self.assertIn('test "$REF_TYPE" = branch', workflow)
        for mode in ("patch", "minor", "major"):
            self.assertIn(f"- {mode}", workflow)
        self.assertNotIn("release.py publish", workflow)
        self.assertNotIn("git tag", workflow)
        self.assertNotIn("gh workflow", workflow)
        helper = (ROOT / ".github/scripts/release.py").read_text()
        for prohibited in ('"DELETE"', '"PATCH"', "--clobber", "--force",
                           '"gh", "release", "create"', '"git", "tag"'):
            self.assertNotIn(prohibited, helper)
        self.assertIn('"push.followTags=false"', helper)
        self.assertIn('"--no-follow-tags"', helper)
        self.assertNotIn("workflow_dispatch", helper)
        self.assertEqual(helper.count('not os.access(guard, os.X_OK)'), 2)
        self.assertTrue(os.access(ROOT / ".github/scripts/bump-hooks/pre-push", os.X_OK))

    def test_cli_asset_options_and_preflight_outputs(self):
        assets = [Path(f"kiosk-1.2.3.{ext}") for ext in ("zip", "dmg")]
        with patch.object(sys, "argv", ["release.py", "publish", "--sha", "a" * 40,
                                       *map(str, assets)]), \
                patch.object(release, "publish") as publish:
            release.main()
        publish.assert_called_once_with("a" * 40, assets)
        with tempfile.TemporaryDirectory(prefix=".lifecycle-cli-", dir=ROOT) as temp:
            output = Path(temp) / "output"
            with patch.dict(os.environ, GITHUB_OUTPUT=str(output)), \
                    patch.object(sys, "argv", ["release.py", "preflight", "--sha", "a" * 40]), \
                    patch.object(release, "preflight", return_value=("1.2.3", "v1.2.3")):
                release.main()
            self.assertEqual(output.read_text(), "version=1.2.3\ntag=v1.2.3\n")
            result = subprocess.run(
                [sys.executable, str(ROOT / ".github/scripts/release.py"),
                 "bump", "--sha", "invalid", "--branch", "selected", "--mode", "patch"],
                cwd=temp, text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            for required in ("without rollback/retry", "retained", "README recovery guidance"):
                self.assertIn(required, result.stderr)

    def test_context_rejects_invalid_helper_version_and_repository(self):
        with patch.object(release, "selected_sha"), patch.object(release, "run", return_value=""):
            for version in ("1.2", "v1.2.3", "01.2.3", "1.2.3-beta", None):
                with self.subTest(version=version), patch.object(release, "read", return_value=version):
                    with self.assertRaisesRegex(ValueError, "strict X.Y.Z"):
                        release.context("a" * 40)
            with patch.object(release, "read", return_value="1.2.3"), \
                    patch.dict(os.environ, GITHUB_REPOSITORY="../invalid/repo"):
                with self.assertRaisesRegex(ValueError, "Invalid repository"):
                    release.context("a" * 40)


if __name__ == "__main__":
    unittest.main()
