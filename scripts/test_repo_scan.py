#!/usr/bin/env python3
"""Tests for repo-scan.py (stdlib unittest, no pytest).

Pure functions (bin-packing, manifest diff, glob/elision, blob hashing) are
unit-tested directly with plain data. Git-touching paths are integration-tested
against throwaway repos built in tmpdirs. Deterministic: no wall-clock, no
randomness; output is sorted.

Run: python3 -m unittest discover -s grill-repo/scripts -p 'test_*.py'
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "repo-scan.py")

_spec = importlib.util.spec_from_file_location("repo_scan", SCRIPT)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

# Isolated git environment: ignore the developer's global/system git config so
# tests are hermetic (no inherited excludesfile, identity, or default branch).
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
}


def git(root, *args, **kw):
    return subprocess.run(["git", *args], cwd=root, env=GIT_ENV,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", check=kw.get("check", True))


def write(root, rel, data, binary=False):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(rel) else None
    with open(path, "wb" if binary else "w") as f:
        f.write(data)
    return path


# ---------------------------------------------------------------------------
# Pure functions (no git)
# ---------------------------------------------------------------------------
class TestPackBins(unittest.TestCase):
    def _files(self, *pairs):
        return [{"path": p, "size": s} for p, s in pairs]

    def test_empty_set_yields_no_bins(self):
        self.assertEqual(rs.pack_bins([], 100, 5), [])

    def test_single_file_larger_than_ceiling_gets_own_bin(self):
        bins = rs.pack_bins(self._files(("big.py", 500)), 100, 5)
        self.assertEqual(bins, [["big.py"]])

    def test_oversized_file_flushes_current_bin(self):
        bins = rs.pack_bins(
            self._files(("a.py", 10), ("b_big.py", 999), ("c.py", 10)), 100, 5)
        self.assertEqual(bins, [["a.py"], ["b_big.py"], ["c.py"]])

    def test_byte_ceiling_splits_bins(self):
        bins = rs.pack_bins(
            self._files(("a.py", 60), ("b.py", 60), ("c.py", 60)), 100, 99)
        self.assertEqual(bins, [["a.py"], ["b.py"], ["c.py"]])

    def test_file_count_ceiling_splits_bins(self):
        files = self._files(*[("f%02d.py" % i, 1) for i in range(5)])
        bins = rs.pack_bins(files, 10000, 2)
        self.assertEqual(bins, [["f00.py", "f01.py"], ["f02.py", "f03.py"], ["f04.py"]])

    def test_directory_files_kept_contiguous_and_input_order_irrelevant(self):
        # Shuffled input -> sorted -> same-dir files land together, deterministically.
        files = self._files(("b/1.py", 10), ("a/2.py", 10), ("a/1.py", 10))
        bins = rs.pack_bins(files, 10000, 2)
        self.assertEqual(bins, [["a/1.py", "a/2.py"], ["b/1.py"]])


class TestManifestDiff(unittest.TestCase):
    def test_all_four_categories(self):
        manifest = {"keep.py": "h1", "mod.py": "h2", "gone.py": "h3"}
        current = {"keep.py": "h1", "mod.py": "CHANGED", "new.py": "h4"}
        d = rs.diff_manifest(manifest, current)
        self.assertEqual(d["unchanged"], ["keep.py"])
        self.assertEqual(d["changed"], ["mod.py"])
        self.assertEqual(d["new"], ["new.py"])
        self.assertEqual(d["deleted"], ["gone.py"])

    def test_empty_inputs(self):
        self.assertEqual(rs.diff_manifest({}, {}),
                         {"changed": [], "new": [], "deleted": [], "unchanged": []})

    def test_results_are_sorted(self):
        current = {"z.py": "h", "a.py": "h", "m.py": "h"}
        self.assertEqual(rs.diff_manifest({}, current)["new"], ["a.py", "m.py", "z.py"])


class TestManifestParsing(unittest.TestCase):
    def test_plain_path_hash_mapping(self):
        self.assertEqual(rs.manifest_to_hashes({"a.py": "h1"}), {"a.py": "h1"})

    def test_files_array_object(self):
        data = {"files": [{"path": "a.py", "hash": "h1", "size": 3}]}
        self.assertEqual(rs.manifest_to_hashes(data), {"a.py": "h1"})

    def test_bare_list(self):
        data = [{"path": "a.py", "hash": "h1"}, {"path": "b.py", "hash": "h2"}]
        self.assertEqual(rs.manifest_to_hashes(data), {"a.py": "h1", "b.py": "h2"})


class TestGlobAndElision(unittest.TestCase):
    def test_double_star_matches_nested_and_top_level(self):
        self.assertTrue(rs.glob_match("**/dist/**", "pkg/a/dist/bundle.js"))
        self.assertTrue(rs.glob_match("**/dist/**", "dist/bundle.js"))

    def test_double_star_non_match(self):
        self.assertFalse(rs.glob_match("**/dist/**", "src/main.py"))

    def test_single_star_does_not_cross_slash(self):
        self.assertTrue(rs.glob_match("*.py", "main.py"))
        self.assertFalse(rs.glob_match("*.py", "a/main.py"))

    def test_skip_glob_elides(self):
        self.assertTrue(rs.is_elided("a/dist/x.js", {}, ["**/dist/**"]))
        self.assertFalse(rs.is_elided("a/src/x.js", {}, ["**/dist/**"]))

    def test_depth_cap_elides_deeper_files(self):
        depths = {"vendor": 2}
        self.assertFalse(rs.is_elided("vendor/a/b.py", depths, []))   # depth 2 kept
        self.assertTrue(rs.is_elided("vendor/a/b/c.py", depths, []))  # depth 3 elided
        self.assertFalse(rs.is_elided("other/a/b/c.py", depths, []))  # outside cap


class TestBlobHashAndStubNames(unittest.TestCase):
    def test_blob_hash_matches_git_known_values(self):
        self.assertEqual(rs.git_blob_hash_bytes(b""),
                         "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")
        self.assertEqual(rs.git_blob_hash_bytes(b"hello\n"),
                         "ce013625030ba8dba906f756967f9e9ca394464a")

    def test_stub_name_classification(self):
        self.assertTrue(rs.is_stub_name("assets/logo.png"))
        self.assertTrue(rs.is_stub_name("a/logo.SVG"))          # case-insensitive ext
        self.assertTrue(rs.is_stub_name("package-lock.json"))   # lockfile
        self.assertTrue(rs.is_stub_name("sub/yarn.lock"))
        self.assertFalse(rs.is_stub_name("src/main.py"))


# ---------------------------------------------------------------------------
# Git-touching integration tests
# ---------------------------------------------------------------------------
class GitRepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="grill-repo-test-")
        self.addCleanup(self._cleanup)
        git(self.tmp, "init", "-q")

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def records(self, **kw):
        recs, subs = rs.build_records(
            self.tmp, kw.get("submodule", "blackbox"),
            kw.get("depths", {}), kw.get("skips", []))
        return recs, subs

    def by_path(self, recs):
        return {r["path"]: r for r in recs}


class TestEnumeration(GitRepoTestCase):
    def test_tracked_plus_untracked_minus_ignored(self):
        write(self.tmp, "tracked.py", "x = 1\n")
        write(self.tmp, ".gitignore", "ignored.txt\nbuild/\n")
        git(self.tmp, "add", "tracked.py", ".gitignore")
        git(self.tmp, "commit", "-qm", "init")
        write(self.tmp, "untracked.py", "y = 2\n")     # new, non-ignored
        write(self.tmp, "ignored.txt", "secret\n")     # ignored -> excluded
        write(self.tmp, "build/out.js", "compiled\n")  # ignored dir -> excluded
        recs, _ = self.records()
        paths = sorted(r["path"] for r in recs)
        self.assertEqual(paths, [".gitignore", "tracked.py", "untracked.py"])

    def test_empty_scan_set_is_valid(self):
        write(self.tmp, ".gitignore", "*\n")  # ignore everything incl. itself
        recs, subs = self.records()
        self.assertEqual(recs, [])
        self.assertEqual(subs, [])


class TestHashAndClassify(GitRepoTestCase):
    def setUp(self):
        super().setUp()
        write(self.tmp, "src/main.py", "def f():\n    return 1\n")  # source
        write(self.tmp, "blob.dat", "\x00\x01\x02NUL", binary=False)  # written below
        write(self.tmp, "blob.dat", "", binary=False)
        with open(os.path.join(self.tmp, "blob.dat"), "wb") as f:
            f.write(b"\x00\x01\x02tracked-binary\x00")  # NUL -> git says binary
        write(self.tmp, "logo.png", "<svg>not really binary</svg>\n")  # binary by ext
        os.symlink("src/main.py", os.path.join(self.tmp, "link.py"))   # symlink
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        # Untracked binary (classified via null-byte sniff, not git numstat).
        with open(os.path.join(self.tmp, "fresh.dat"), "wb") as f:
            f.write(b"new\x00binary")

    def test_kinds(self):
        recs = self.by_path(self.records()[0])
        self.assertEqual(recs["src/main.py"]["kind"], "source")
        self.assertEqual(recs["blob.dat"]["kind"], "binary")     # git numstat
        self.assertEqual(recs["logo.png"]["kind"], "binary")     # stub extension
        self.assertEqual(recs["link.py"]["kind"], "symlink")     # symlink
        self.assertEqual(recs["fresh.dat"]["kind"], "binary")    # null-byte sniff

    def test_source_and_binary_hashes_equal_git_hash_object(self):
        recs = self.by_path(self.records()[0])
        for path in ("src/main.py", "blob.dat", "logo.png"):
            want = git(self.tmp, "hash-object", path).stdout.strip()
            self.assertEqual(recs[path]["hash"], want, path)

    def test_symlink_hash_equals_stored_blob(self):
        # git hash-object follows the link; the *stored* symlink blob is the
        # target-string blob, which is what we compute.
        recs = self.by_path(self.records()[0])
        want = git(self.tmp, "rev-parse", ":link.py").stdout.strip()
        self.assertEqual(recs["link.py"]["hash"], want)
        self.assertEqual(recs["link.py"]["target"], "src/main.py")  # readlink surfaced


class TestRepoInfo(GitRepoTestCase):
    def test_clean_then_dirty(self):
        write(self.tmp, "a.py", "x = 1\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        info = rs.repo_info(self.tmp)
        self.assertFalse(info["dirty"])
        self.assertEqual(info["commit"], git(self.tmp, "rev-parse", "HEAD").stdout.strip())
        self.assertTrue(info["branch"])
        write(self.tmp, "a.py", "x = 2\n")  # modify -> dirty
        self.assertTrue(rs.repo_info(self.tmp)["dirty"])


class TestScope(GitRepoTestCase):
    def test_depth_and_skip_mark_elided_and_exclude_from_bins(self):
        write(self.tmp, "keep.py", "x = 1\n")
        write(self.tmp, "vendor/a.py", "x = 1\n")        # depth 1 -> kept
        write(self.tmp, "vendor/deep/b.py", "x = 1\n")   # depth 2 -> elided (cap 1)
        write(self.tmp, "gen/out.py", "x = 1\n")         # skip-glob -> elided
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        recs = self.by_path(self.records(depths={"vendor": 1}, skips=["**/gen/**"])[0])
        self.assertFalse(recs["keep.py"]["elided"])
        self.assertFalse(recs["vendor/a.py"]["elided"])
        self.assertTrue(recs["vendor/deep/b.py"]["elided"])
        self.assertTrue(recs["gen/out.py"]["elided"])


# ---------------------------------------------------------------------------
# Submodule handling (simulated via .gitmodules + cacheinfo gitlink, offline)
# ---------------------------------------------------------------------------
class TestSubmodules(GitRepoTestCase):
    def setUp(self):
        super().setUp()
        # Build a nested repo that will act as the submodule's working tree.
        self.sub = os.path.join(self.tmp, "sub")
        os.makedirs(self.sub)
        git(self.sub, "init", "-q")
        write(self.sub, "mod.py", "def g():\n    return 2\n")
        write(self.sub, "data.bin", "")
        with open(os.path.join(self.sub, "data.bin"), "wb") as f:
            f.write(b"\x00\x01")
        git(self.sub, "add", "-A")
        git(self.sub, "commit", "-qm", "sub init")
        self.sub_sha = git(self.sub, "rev-parse", "HEAD").stdout.strip()
        # Register it in the parent as a real gitlink + .gitmodules entry.
        write(self.tmp, ".gitmodules",
              '[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n')
        write(self.tmp, "top.py", "x = 1\n")
        git(self.tmp, "add", ".gitmodules", "top.py")
        git(self.tmp, "update-index", "--add", "--cacheinfo",
            "160000,%s,sub" % self.sub_sha)
        git(self.tmp, "commit", "-qm", "add submodule")

    def test_blackbox_one_entry_no_internals(self):
        recs, subs = self.records(submodule="blackbox")
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["name"], "sub")
        self.assertEqual(subs[0]["url"], "./sub")
        self.assertEqual(subs[0]["commit"], self.sub_sha)
        self.assertFalse(subs[0]["walked"])
        paths = [r["path"] for r in recs]
        self.assertIn("top.py", paths)
        self.assertNotIn("sub", paths)                 # gitlink excluded
        self.assertFalse(any(p.startswith("sub/") for p in paths))  # no internals

    def test_walk_enumerates_internals(self):
        recs, subs = self.records(submodule="walk")
        self.assertTrue(subs[0]["walked"])
        kinds = self.by_path(recs)
        self.assertEqual(kinds["sub/mod.py"]["kind"], "source")
        self.assertEqual(kinds["sub/data.bin"]["kind"], "binary")
        # Hash computed off the submodule working tree, matches git there.
        want = git(self.sub, "hash-object", "mod.py").stdout.strip()
        self.assertEqual(kinds["sub/mod.py"]["hash"], want)


# ---------------------------------------------------------------------------
# CLI end-to-end (plan + staleness as subprocess)
# ---------------------------------------------------------------------------
class TestPlanCLI(GitRepoTestCase):
    def run_script(self, *args, expect=0):
        p = subprocess.run([sys.executable, SCRIPT, *args], env=GIT_ENV,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           encoding="utf-8")
        self.assertEqual(p.returncode, expect, p.stderr)
        return p

    def test_plan_emits_expected_json(self):
        write(self.tmp, "a/one.py", "x = 1\n")
        write(self.tmp, "a/two.py", "y = 2\n")
        write(self.tmp, "logo.png", "img\n")
        os.symlink("a/one.py", os.path.join(self.tmp, "link.py"))
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        out = json.loads(self.run_script("plan", "--root", self.tmp).stdout)
        self.assertEqual(sorted(r["path"] for r in out["files"]),
                         ["a/one.py", "a/two.py", "link.py", "logo.png"])
        self.assertEqual(out["counts"]["source"], 2)
        self.assertEqual(out["counts"]["binary"], 1)
        self.assertEqual(out["counts"]["symlink"], 1)
        # Two small source files fit one bin.
        self.assertEqual(out["bins"], [["a/one.py", "a/two.py"]])
        self.assertEqual(out["counts"]["bins"], 1)
        stub_paths = sorted(s["path"] for s in out["stubs"])
        self.assertEqual(stub_paths, ["link.py", "logo.png"])
        self.assertFalse(out["repo"]["dirty"])

    def test_symlink_stub_carries_target(self):
        write(self.tmp, "real.py", "x = 1\n")
        write(self.tmp, "logo.png", "img\n")          # binary stub: no target
        os.symlink("real.py", os.path.join(self.tmp, "link.py"))  # symlink stub
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        out = json.loads(self.run_script("plan", "--root", self.tmp).stdout)
        stubs = {s["path"]: s for s in out["stubs"]}
        self.assertEqual(stubs["link.py"]["target"], "real.py")  # readlink value emitted
        self.assertNotIn("target", stubs["logo.png"])            # binaries carry none

    def test_bin_files_ceiling_via_cli(self):
        for i in range(3):
            write(self.tmp, "f%d.py" % i, "x = 1\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        out = json.loads(self.run_script("plan", "--root", self.tmp,
                                         "--bin-files", "2").stdout)
        self.assertEqual(out["bins"], [["f0.py", "f1.py"], ["f2.py"]])

    def test_not_a_git_repo_exits_nonzero(self):
        plain = tempfile.mkdtemp(prefix="not-git-")
        self.addCleanup(lambda: __import__("shutil").rmtree(plain, ignore_errors=True))
        p = self.run_script("plan", "--root", plain, expect=2)
        self.assertIn("not a git repository", p.stderr)


class TestStalenessCLI(GitRepoTestCase):
    def _manifest_file(self, mapping):
        # Written OUTSIDE the scanned repo so it does not pollute the scan set
        # (an in-tree manifest would correctly count as an untracked file).
        fd, path = tempfile.mkstemp(prefix="manifest-", suffix=".json")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with os.fdopen(fd, "w") as f:
            json.dump(mapping, f)
        return path

    def run_script(self, *args):
        p = subprocess.run([sys.executable, SCRIPT, "staleness", *args], env=GIT_ENV,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_diff_changed_new_deleted_unchanged(self):
        write(self.tmp, "keep.py", "x = 1\n")
        write(self.tmp, "mod.py", "y = 1\n")
        write(self.tmp, "gone.py", "z = 1\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        recs = self.by_path(self.records()[0])
        manifest = {p: recs[p]["hash"] for p in ("keep.py", "mod.py", "gone.py")}
        mf = self._manifest_file(manifest)
        # Mutate the tree: change mod, delete gone, add fresh.
        write(self.tmp, "mod.py", "y = 999\n")
        os.remove(os.path.join(self.tmp, "gone.py"))
        write(self.tmp, "new.py", "w = 1\n")
        out = self.run_script("--root", self.tmp, "--manifest", mf)
        self.assertEqual(out["changed"], ["mod.py"])
        self.assertEqual(out["deleted"], ["gone.py"])
        self.assertEqual(out["new"], ["new.py"])
        self.assertEqual(out["unchanged"], ["keep.py"])
        self.assertFalse(out["cheap_fresh"])  # tree is dirty

    def test_cheap_fresh_true_when_clean_complete_and_commit_matches(self):
        write(self.tmp, "a.py", "x = 1\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        head = git(self.tmp, "rev-parse", "HEAD").stdout.strip()
        recs = self.by_path(self.records()[0])
        mf = self._manifest_file({"a.py": recs["a.py"]["hash"]})
        out = self.run_script("--root", self.tmp, "--manifest", mf,
                              "--recorded-commit", head,
                              "--recorded-dirty", "false", "--complete", "true")
        self.assertTrue(out["cheap_fresh"])
        self.assertEqual(out["unchanged"], ["a.py"])

    def test_cheap_fresh_false_when_incomplete(self):
        write(self.tmp, "a.py", "x = 1\n")
        git(self.tmp, "add", "-A")
        git(self.tmp, "commit", "-qm", "init")
        head = git(self.tmp, "rev-parse", "HEAD").stdout.strip()
        recs = self.by_path(self.records()[0])
        mf = self._manifest_file({"a.py": recs["a.py"]["hash"]})
        out = self.run_script("--root", self.tmp, "--manifest", mf,
                              "--recorded-commit", head,
                              "--recorded-dirty", "false", "--complete", "false")
        self.assertFalse(out["cheap_fresh"])


if __name__ == "__main__":
    unittest.main()
