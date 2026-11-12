#!/usr/bin/env python3
"""grill-repo helper: deterministic git plumbing for the repo-map skill.

Stdlib only. Enumerates files, fingerprints them with git's blob hash,
classifies them (source / binary / symlink), bin-packs source files, and
diffs the tree against a saved manifest for staleness. Emits JSON to stdout.

It NEVER reads file contents for meaning (no constants / functions / classes
parsing -- that is the LLM scanning subagents' job) and it writes no map files.
See SKILL.md for the authoritative contract.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

# Binary-by-extension stubs + common lockfiles (no LLM read). From the
# behaviour doc's Constants block; the contract is authoritative on overlap.
STUB_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip",
             ".tar", ".gz", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mp3",
             ".bin"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
             "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum",
             "Pipfile.lock"}
NULL_SNIFF_BYTES = 8192
HASH_CHUNK = 65536
EMPTY_TREE_FALLBACK = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


# ---------------------------------------------------------------------------
# Pure helpers (no git, no filesystem) -- unit-tested directly.
# ---------------------------------------------------------------------------
def git_blob_hash_bytes(data):
    """git's blob object id for the given bytes: sha1("blob <len>\\0" + data)."""
    h = hashlib.sha1()
    h.update(b"blob " + str(len(data)).encode() + b"\x00")
    h.update(data)
    return h.hexdigest()


def is_stub_name(path):
    """True if the basename's extension or whole name marks it a binary stub."""
    base = os.path.basename(path)
    return os.path.splitext(base)[1].lower() in STUB_EXTS or base in LOCKFILES


_GLOB_CACHE = {}


def _glob_to_regex(pattern):
    """Translate a gitignore-style glob to an anchored regex.

    ``**/`` matches zero or more leading path segments, ``**`` matches across
    ``/``, ``*`` matches within a segment, ``?`` matches one non-slash char.
    """
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "^" + "".join(out) + "$"


def glob_match(pattern, path):
    rx = _GLOB_CACHE.get(pattern)
    if rx is None:
        rx = re.compile(_glob_to_regex(pattern))
        _GLOB_CACHE[pattern] = rx
    return rx.match(path) is not None


def is_elided(path, depths, skips):
    """A path is elided if it matches a skip-glob or sits below a depth cap.

    ``depths`` maps DIR -> N: a file under DIR is elided when its path,
    relative to DIR, has more than N segments.
    """
    for pattern in skips:
        if glob_match(pattern, path):
            return True
    for directory, limit in depths.items():
        prefix = directory + "/"
        if path.startswith(prefix):
            if len(path[len(prefix):].split("/")) > limit:
                return True
    return False


def pack_bins(files, bin_bytes, bin_files):
    """Greedy bin-pack source files. Pure over a list of {'path','size'} dicts.

    Each bin holds <= ``bin_bytes`` total and <= ``bin_files`` files; a single
    file larger than ``bin_bytes`` gets its own bin. Files are processed in
    sorted path order so a directory's files stay contiguous and output is
    deterministic. Returns a list of bins, each a list of paths.
    """
    ordered = sorted(files, key=lambda f: f["path"])
    bins, current, current_bytes = [], [], 0
    for f in ordered:
        size, path = f["size"], f["path"]
        if size > bin_bytes:
            if current:
                bins.append(current)
                current, current_bytes = [], 0
            bins.append([path])
            continue
        if current and (current_bytes + size > bin_bytes
                        or len(current) >= bin_files):
            bins.append(current)
            current, current_bytes = [], 0
        current.append(path)
        current_bytes += size
    if current:
        bins.append(current)
    return bins


def manifest_to_hashes(data):
    """Normalise a saved manifest to {path: hash}.

    Accepts a plain ``{path: hash}`` mapping, a ``{"files": [...]}`` object
    (as emitted by ``plan``), or a bare ``[{"path","hash"}, ...]`` list.
    """
    if isinstance(data, dict) and isinstance(data.get("files"), list):
        return {e["path"]: e["hash"] for e in data["files"]}
    if isinstance(data, list):
        return {e["path"]: e["hash"] for e in data}
    if isinstance(data, dict):
        return dict(data)
    raise ValueError("unrecognised manifest format")


def diff_manifest(manifest, current):
    """Diff two {path: hash} maps. Returns sorted changed/new/deleted/unchanged."""
    changed, new, unchanged = [], [], []
    for path, h in current.items():
        if path not in manifest:
            new.append(path)
        elif manifest[path] != h:
            changed.append(path)
        else:
            unchanged.append(path)
    deleted = [p for p in manifest if p not in current]
    return {"changed": sorted(changed), "new": sorted(new),
            "deleted": sorted(deleted), "unchanged": sorted(unchanged)}


# ---------------------------------------------------------------------------
# Git + filesystem helpers.
# ---------------------------------------------------------------------------
class GitError(Exception):
    pass


def run_git(args, root, check=True, stdin=None):
    p = subprocess.run(
        ["git", *args], cwd=root, input=stdin,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="surrogateescape")
    if check and p.returncode != 0:
        raise GitError("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout


def _git_or_none(args, root):
    p = subprocess.run(
        ["git", *args], cwd=root, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, encoding="utf-8", errors="surrogateescape")
    return p.stdout.strip() if p.returncode == 0 else None


def is_git_repo(root):
    if not os.path.isdir(root):
        return False
    return _git_or_none(["rev-parse", "--is-inside-work-tree"], root) == "true"


def repo_info(root):
    """Current {commit, branch, dirty}. dirty counts tracked changes AND
    untracked (non-ignored) files, matching the 'clean tree' freshness rule."""
    porcelain = run_git(
        ["status", "--porcelain", "--untracked-files=normal"], root)
    return {"commit": _git_or_none(["rev-parse", "HEAD"], root),
            "branch": _git_or_none(["branch", "--show-current"], root) or "",
            "dirty": bool(porcelain.strip())}


def _ls(args, root):
    return [p for p in run_git(args, root).split("\x00") if p]


def _empty_tree(root):
    # Computed, not written (no -w): git recognises the empty tree intrinsically.
    return (run_git(["hash-object", "-t", "tree", "--stdin"], root,
                    stdin="", check=False).strip() or EMPTY_TREE_FALLBACK)


def git_binary_set(root):
    """Paths git reports binary, via ``git diff --numstat`` vs the empty tree
    (binary files show '-\\t-\\t<path>'). Covers all present tracked files."""
    out = run_git(["-c", "core.quotepath=false", "diff", "--numstat",
                   _empty_tree(root)], root, check=False)
    binary = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0] == "-" and parts[1] == "-":
            binary.add("\t".join(parts[2:]))
    return binary


def git_submodules(root):
    """Submodules from .gitmodules (name, path, url) + pinned gitlink commit."""
    gitmodules = os.path.join(root, ".gitmodules")
    if not os.path.isfile(gitmodules):
        return []
    info = {}
    for line in run_git(["config", "-f", gitmodules, "--list"], root,
                        check=False).splitlines():
        key, _, value = line.partition("=")
        m = re.match(r"submodule\.(.+)\.(path|url)$", key)
        if m:
            info.setdefault(m.group(1), {})[m.group(2)] = value
    links = {}
    for line in run_git(["ls-files", "-s"], root, check=False).splitlines():
        meta, _, path = line.partition("\t")
        cols = meta.split()
        if len(cols) >= 2 and cols[0] == "160000":
            links[path] = cols[1]
    subs = []
    for name in sorted(info):
        path = info[name].get("path", name)
        subs.append({"name": name, "path": path,
                     "url": info[name].get("url", ""),
                     "commit": links.get(path, "")})
    return subs


def _has_null_byte(abspath):
    try:
        with open(abspath, "rb") as f:
            return b"\x00" in f.read(NULL_SNIFF_BYTES)
    except OSError:
        return False


def _kind_for(abspath, rel_in_repo, full_path, binary_set, untracked):
    if os.path.islink(abspath):
        return "symlink"
    if is_stub_name(full_path):
        return "binary"
    if rel_in_repo in binary_set:
        return "binary"
    if untracked and _has_null_byte(abspath):
        return "binary"
    return "source"


def _hash_and_size(abspath):
    if os.path.islink(abspath):
        link = os.readlink(abspath)
        target = os.fsencode(link)
        return git_blob_hash_bytes(target), len(target), link
    size = os.path.getsize(abspath)
    h = hashlib.sha1()
    h.update(b"blob " + str(size).encode() + b"\x00")
    with open(abspath, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest(), size, None


def _record(repo_abs, rel_in_repo, full_path, binary_set, untracked,
            depths, skips):
    abspath = os.path.join(repo_abs, rel_in_repo)
    kind = _kind_for(abspath, rel_in_repo, full_path, binary_set, untracked)
    h, size, target = _hash_and_size(abspath)
    record = {"path": full_path, "size": size, "hash": h, "kind": kind,
              "elided": is_elided(full_path, depths, skips)}
    if target is not None:  # symlinks: surface the readlink target (never followed)
        record["target"] = target
    return record


def _under_any(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def build_records(root, submodule_mode, depths, skips):
    """Enumerate + classify + fingerprint the scan set for ``root``.

    Returns ``(records, submodules)`` where records is a path-sorted list of
    {path, size, hash, kind, elided} and submodules is the list of submodule
    entries (each carrying ``walked``). Shared by ``plan`` and ``staleness``
    so the compared set always matches.
    """
    subs = git_submodules(root)
    sub_paths = [s["path"] for s in subs]
    tracked = set(_ls(["ls-files", "-z"], root))
    others = set(_ls(["ls-files", "--others", "--exclude-standard", "-z"], root))
    binary_set = git_binary_set(root)

    records = []
    for rel in tracked | others:
        if _under_any(rel, sub_paths):
            continue
        abspath = os.path.join(root, rel)
        if not os.path.lexists(abspath):  # tracked but deleted from worktree
            continue
        untracked = rel in others and rel not in tracked
        records.append(_record(root, rel, rel, binary_set, untracked,
                               depths, skips))

    out_subs = []
    for s in subs:
        walked = submodule_mode == "walk"
        out_subs.append({"name": s["name"], "url": s["url"],
                         "commit": s["commit"], "walked": walked})
        if not walked:
            continue
        sub_abs = os.path.join(root, s["path"])
        if not is_git_repo(sub_abs):
            continue
        sub_binary = git_binary_set(sub_abs)
        for srel in _ls(["ls-files", "-z"], sub_abs):
            sub_file_abs = os.path.join(sub_abs, srel)
            if not os.path.lexists(sub_file_abs):
                continue
            records.append(_record(sub_abs, srel, s["path"] + "/" + srel,
                                   sub_binary, False, depths, skips))

    records.sort(key=lambda r: r["path"])
    return records, out_subs


# ---------------------------------------------------------------------------
# Subcommands.
# ---------------------------------------------------------------------------
def cmd_plan(args):
    root = os.path.abspath(args.root)
    _require_repo(root)
    records, subs = build_records(root, args.submodule, args.depths, args.skips)
    source = [r for r in records if r["kind"] == "source" and not r["elided"]]
    bins = pack_bins(source, args.bin_bytes, args.bin_files)
    stubs = []
    for r in records:
        if r["kind"] not in ("binary", "symlink"):
            continue
        stub = {"path": r["path"], "kind": r["kind"], "size": r["size"],
                "hash": r["hash"]}
        if r["kind"] == "symlink":
            stub["target"] = r["target"]
        stubs.append(stub)
    counts = {
        "source": sum(r["kind"] == "source" for r in records),
        "binary": sum(r["kind"] == "binary" for r in records),
        "symlink": sum(r["kind"] == "symlink" for r in records),
        "elided": sum(r["elided"] for r in records),
        "bins": len(bins),
    }
    return {"repo": repo_info(root), "files": records, "bins": bins,
            "stubs": stubs, "submodules": subs, "counts": counts}


def cmd_staleness(args):
    root = os.path.abspath(args.root)
    _require_repo(root)
    with open(args.manifest, encoding="utf-8") as f:
        manifest = manifest_to_hashes(json.load(f))
    records, _ = build_records(root, args.submodule, args.depths, args.skips)
    current = {r["path"]: r["hash"] for r in records}
    info = repo_info(root)
    cheap_fresh = (args.complete and not args.recorded_dirty
                   and info["commit"] == args.recorded_commit
                   and not info["dirty"])
    result = {"cheap_fresh": cheap_fresh, "repo": info}
    result.update(diff_manifest(manifest, current))
    return result


def _require_repo(root):
    if not is_git_repo(root):
        sys.stderr.write(
            "error: not a git repository (or no working tree): %s\n" % root)
        sys.exit(2)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def _parse_depths(values):
    depths = {}
    for value in values or []:
        directory, sep, n = value.rpartition("=")
        if not sep or not n.lstrip("-").isdigit():
            raise SystemExit("invalid --depth (want DIR=N): %s" % value)
        depths[directory] = int(n)
    return depths


def _parse_bool(value):
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off", ""):
        return False
    raise SystemExit("invalid boolean (want true/false): %s" % value)


def _add_scope_flags(parser):
    parser.add_argument("--root", default=".")
    parser.add_argument("--submodule", choices=["blackbox", "walk"],
                        default="blackbox")
    parser.add_argument("--depth", action="append", dest="depth_raw",
                        metavar="DIR=N", help="cap detail depth under DIR")
    parser.add_argument("--skip", action="append", dest="skips",
                        metavar="GLOB", help="skip-glob for vendored trees")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="repo-scan.py")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    plan = subparsers.add_parser("plan", help="enumerate + classify + bin-pack")
    _add_scope_flags(plan)
    plan.add_argument("--bin-bytes", type=int, default=65536)
    plan.add_argument("--bin-files", type=int, default=15)

    stale = subparsers.add_parser("staleness", help="diff tree vs a manifest")
    _add_scope_flags(stale)
    stale.add_argument("--manifest", required=True)
    stale.add_argument("--recorded-commit", default=None)
    stale.add_argument("--recorded-dirty", default="false")
    stale.add_argument("--complete", default="false")

    args = parser.parse_args(argv)
    args.depths = _parse_depths(getattr(args, "depth_raw", None))
    args.skips = args.skips or []

    if args.cmd == "plan":
        result = cmd_plan(args)
    else:
        args.recorded_dirty = _parse_bool(args.recorded_dirty)
        args.complete = _parse_bool(args.complete)
        result = cmd_staleness(args)

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
