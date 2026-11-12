# grill-repo

[![CI](https://github.com/Guowei-Yan/grill-repo/actions/workflows/ci.yml/badge.svg)](https://github.com/Guowei-Yan/grill-repo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](#)

Incremental Git content-hash repository scanner and deterministic subagent wave bin-packer for persistent codebase maps (**zero third-party runtime dependencies**).

Re-reading an entire repository from scratch in every coding-agent session wastes context budget and latency. `grill-repo` pairs deterministic `git hash-object` fingerprinting (`scripts/repo-scan.py`) with bounded parallel summarization waves to build and incrementally refresh a navigable per-file digest plus synthesized architectural overview.

---

## How It Works

1. **Deterministic Enumeration & Fingerprinting (`scripts/repo-scan.py`)**:
   - Enumerates tracked and untracked non-ignored files, computes `git hash-object` blob OIDs (including working-tree edits), classifies symlinks (emitting link targets without dereferencing) and binary assets, and emits pure JSON without mutating the repository.
2. **O(1) Freshness & Incremental Staleness Diffing**:
   - Compares current blob OIDs against the persisted manifest to classify files into `added`, `modified`, `unchanged`, and `deleted`, re-scanning only touched files on subsequent runs.
3. **Bounded Wave Bin-Packing**:
   - Packs changed text files into balanced subagent waves bounded by byte budget and file count so each fresh-context worker produces structured per-file digests (constants, classes, functions, and one-line logic summaries) without context overflow.

---

## Quickstart

### Run `repo-scan.py` Against Any Git Repository

```bash
# Enumerate files, fingerprint via git hash-object, and pack subagent waves
python3 scripts/repo-scan.py plan --root .

# Check incremental staleness against an existing manifest
python3 scripts/repo-scan.py staleness --root . --manifest .repo-map/manifest.json
```

### Run the Test Suite (35 Tests)

```bash
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

### Install as a Local Coding-Agent Skill

```bash
./sync-grill-repo.sh
```

See [`SKILL.md`](SKILL.md) for the full manifest schema, incremental refresh protocol, and overview synthesis contract.

---

## License

[MIT](LICENSE)
