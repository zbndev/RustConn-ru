#!/usr/bin/env python3
"""Prepare a local RustConnRu merge without committing or running a build."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib


FORK_ID = "io.github.zbndev.RustConnRu"
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
POT = "po/rustconn.pot"
RUSSIAN = "po/ru.po"
CHANGELOG = "CHANGELOG.md"
# The upstream blob must change by exactly one version line. The second
# template is the fork's version line, or None if the fork has no release tag.
PACKAGE_LINES = {
    f"packaging/flathub/{FORK_ID}.yml": ("        tag: v{}", None),
    f"packaging/flatpak/{FORK_ID}.yml": ("        tag: v{}", None),
    "packaging/macos/rustconn.rb": (
        '  url "https://github.com/totoshko88/RustConn/archive/refs/tags/v{}.tar.gz"', None
    ),
    "packaging/obs/AppImageBuilder.yml": ("    version: {}", "    version: {}"),
    "snap/snapcraft.yaml": ("version: '{}'", "version: '{}'"),
}


class SyncError(Exception):
    pass


def run(*args, ok=(0,), data=None):
    result = subprocess.run(
        args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "LC_ALL": "C"}, check=False,
    )
    if result.returncode not in ok:
        diagnostic = result.stderr or result.stdout
        raise SyncError(f"{' '.join(map(str, args))}: {os.fsdecode(diagnostic).strip()}")
    return result


def git(*args, ok=(0,)):
    # Respect the checkout's objects, not replacement refs or a local rerere cache.
    return run("git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", *args, ok=ok).stdout


def ref(name):
    return git("rev-parse", "--verify", f"{name}^{{commit}}").decode().strip()


def version_at(commit):
    return tomllib.loads(git("show", f"{commit}:Cargo.toml").decode())["workspace"]["package"]["version"]


def merge_base(fork, upstream):
    return git("merge-base", "--all", fork, upstream).decode().strip()


def index_entries(*args):
    entries = {}
    for record in git("ls-files", "--stage", "-z", *args).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode().split()
            entries.setdefault(os.fsdecode(path), {})[int(stage)] = (mode, oid)
    return entries


def is_content_conflict(stages):
    return set(stages) == {1, 2, 3} and all(mode == "100644" for mode, _ in stages.values())


def catalog_path(path):
    return path == POT or re.fullmatch(r"po/[a-zA-Z0-9_@-]+\.po", path) is not None


def ordinary_catalog_conflict(path, stages):
    return path != RUSSIAN and catalog_path(path) and is_content_conflict(stages)


def replace_line(data, template, old, new):
    before, after = (template.format(v).encode() + b"\n" for v in (old, new))
    lines = data.splitlines(keepends=True)
    if lines.count(before) != 1:
        return None
    lines[lines.index(before)] = after
    return b"".join(lines)


def stage_files(files):
    for path in files:
        dest = Path(path)
        if dest.is_symlink() or not dest.is_file():
            raise SyncError(f"Not a regular worktree file: {path}")
    for path, data in files.items():
        Path(path).write_bytes(data)
    if files:
        git("update-index", "--add", "--", *files)


def prepare(tag, upstream, old_version, base):
    if git("symbolic-ref", "--quiet", "HEAD", ok=(0, 1)) != b"refs/heads/main\n":
        raise SyncError("prepare must start on main")
    operations = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")
    if any(Path(os.fsdecode(git("rev-parse", "--git-path", name)).strip()).exists() for name in operations):
        raise SyncError("Finish the current Git operation first")
    if git("status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise SyncError("prepare requires a clean main, including untracked files")
    if git("ls-tree", upstream, "--", CHANGELOG).split(b" ", 1)[0] != b"100644":
        raise SyncError(f"Expected a regular {CHANGELOG} in {tag}")
    git("switch", "-c", f"update/{tag}")
    git("-c", "rerere.enabled=false", "merge", "-s", "ort", "--no-ff", "--no-commit", upstream, ok=(0, 1))
    if ref("MERGE_HEAD") != upstream:
        raise SyncError("No merge with the requested upstream commit in progress")

    unresolved = index_entries("--unmerged")
    git("restore", f"--source={upstream}", "--staged", "--worktree", "--", CHANGELOG)
    unresolved.pop(CHANGELOG, None)
    resolved = {}
    for path, (original, fork) in PACKAGE_LINES.items():
        stages = unresolved.get(path, {})
        if not is_content_conflict(stages):
            continue
        ancestor, ours, theirs = (git("cat-file", "blob", stages[i][1]) for i in (1, 2, 3))
        if replace_line(ancestor, original, old_version, tag[1:]) != theirs:
            continue
        if fork is None:
            if original.format(old_version).encode() in ours.splitlines():
                continue
            data = ours
        else:
            data = replace_line(ours, fork, old_version, tag[1:])
        if data is not None:
            resolved[path] = data
    stage_files(resolved)
    deferred = [p for p, stages in unresolved.items() if ordinary_catalog_conflict(p, stages)]
    manual = sorted(unresolved.keys() - resolved.keys() - set(deferred))
    print(f"update/{tag}: {base[:12]} -> {upstream[:12]}")
    print(f"{CHANGELOG}: exact upstream copy")
    report("Packaging resolved", sorted(resolved))
    print(f"Gettext conflicts deferred to catalogs: {len(deferred)}")
    report("Manual conflicts", manual)
    print("Review upstream changes and resolve manual conflicts before running catalogs.")


def report(label, entries):
    print(f"{label}: {len(entries)}")
    for entry in entries:
        print(f"  {entry}")


def po_entries(data):
    # GNU gettext handles quoting, continuations and line wrapping. Here each
    # normalized block is one entry; ignore extracted refs/format flags when
    # comparing translations, but keep translator comments and fuzzy state.
    normalized = run("msgcat", "--no-location", "--no-wrap", "--force-po", "-", data=data).stdout
    entries = {}
    header = b""
    obsolete = []
    for block in normalized.strip(b"\n").split(b"\n\n"):
        lines = block.split(b"\n")
        source = [line for line in lines if not line.startswith(b"#")]
        if not source:
            if any(line.startswith(b"#~") for line in lines):
                obsolete.append(block)
            continue
        end = next(i for i, line in enumerate(source) if line.startswith((b"msgid_plural ", b"msgstr")))
        key = tuple(source[:end])
        if key == (b'msgid ""',):
            header = block
            continue
        flags = [line[2:].split(b",") for line in lines if line.startswith(b"#,")]
        fuzzy = any(flag.strip() == b"fuzzy" for group in flags for flag in group)
        comments = tuple(line for line in lines if line == b"#" or line.startswith(b"# "))
        entries[key] = ((source[end:], fuzzy, comments), block)
    return header, entries, obsolete


def catalog_seed(path, stages, active):
    base, fork, upstream = (po_entries(git("cat-file", "blob", stages[i][1])) for i in (1, 2, 3))
    header, merged, obsolete = upstream
    base_entries, fork_entries = base[1], fork[1]
    conflicts = []
    for key in active:
        original = base_entries.get(key, (None,))[0]
        ours = fork_entries.get(key, (None,))[0]
        theirs = merged.get(key, (None,))[0]
        if ours == theirs:
            continue
        if theirs is None and ours is not None:
            merged[key] = fork_entries[key]
        elif ours != original:
            if theirs == original and ours is not None:
                merged[key] = fork_entries[key]
            else:
                conflicts.append(os.fsdecode(b" ".join(key)))
    if conflicts:
        report(f"Divergent translations in {path}", conflicts)
        return None
    return b"\n\n".join([header, *(block for _, block in merged.values()), *obsolete]) + b"\n"


def catalogs():
    ref("MERGE_HEAD")
    unresolved = index_entries("--unmerged")
    manual = [p for p, stages in unresolved.items() if not ordinary_catalog_conflict(p, stages)]
    if manual:
        report("Resolve before extracting the POT", manual)
        raise SyncError("Manual conflicts remain")
    paths = {p: s for p, s in index_entries("--", "po").items() if catalog_path(p)}
    locales = sorted(paths.keys() - {POT})
    linguas = Path("po/LINGUAS").read_bytes().splitlines()
    if RUSSIAN not in paths or not any(b"ru" in line.split(b"#", 1)[0].split() for line in linguas):
        raise SyncError("Preserve po/ru.po and ru in po/LINGUAS before regenerating catalogs")
    dirty = set(map(os.fsdecode, git("diff", "--name-only", "-z", "--", *paths).split(b"\0")))
    if edited := sorted(dirty & (paths.keys() - unresolved.keys())):
        report("Stage catalog edits explicitly before regenerating", edited)
        raise SyncError("Unstaged catalog edits would be staged by catalogs")
    # Never overwrite a hand-edited but unstaged resolution. ort's AUTO_MERGE
    # tree records what prepare actually put in the worktree.
    for path in unresolved:
        if Path(path).is_symlink() or Path(path).read_bytes() != git("show", f"AUTO_MERGE:{path}"):
            raise SyncError(f"Stage the manual resolution of {path} before regenerating catalogs")
    with TemporaryDirectory(prefix="rustconn-sync-") as tmp:
        template = Path(tmp, "rustconn.pot")
        # This runs a script from the merge worktree, unlike plan and prepare.
        run("bash", "po/update-pot.sh", template)
        files = {POT: template.read_bytes()}
        active = po_entries(files[POT])[1]
        seed = Path(tmp, "seed.po")
        incomplete = []
        divergent = []
        for path in locales:
            if path in unresolved:
                data = catalog_seed(path, paths[path], active)
                if data is None:
                    divergent.append(path)
                    continue
                seed.write_bytes(data)
            else:
                seed.write_bytes(git("cat-file", "blob", paths[path][0][1]))
            files[path] = run("msgmerge", "--quiet", "--previous", seed, template).stdout
            stats = run("msgfmt", "--check", "--statistics", "-o", os.devnull, "-", data=files[path]).stderr
            if b"fuzzy" in stats or b"untranslated" in stats:
                incomplete.append(f"{path}: {os.fsdecode(stats).strip()}")
        if divergent:
            raise SyncError("Merge and stage the listed catalogs manually, then rerun catalogs")
        stage_files(files)
    print(f"Regenerated POT and {len(locales)} catalogs.")
    report("Catalogs needing translation review", incomplete)
    if incomplete:
        print("Inspect each with msgattrib --only-fuzzy --no-obsolete FILE and "
              "msgattrib --untranslated --no-obsolete FILE.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "prepare"):
        commands.add_parser(name).add_argument("tag", help="upstream release tag vX.Y.Z")
    commands.add_parser("catalogs", help="regenerate gettext after reviewing the merged sources and extractor")
    args = parser.parse_args()
    try:
        os.chdir(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n")))
        if args.command == "catalogs":
            catalogs()
            return 0
        if not re.fullmatch(f"v{VERSION}", args.tag):
            raise SyncError("Expected an upstream tag vX.Y.Z")
        if git("config", "--get-regexp", r"^merge\..*\.driver$", ok=(0, 1)):
            raise SyncError("Disable configured external Git merge drivers for this checkout")
        tag_ref = f"refs/tags/upstream/{args.tag}"
        git("fetch", "--no-tags", "--no-write-fetch-head", "upstream", f"refs/tags/{args.tag}:{tag_ref}")
        upstream = ref(tag_ref)
        fork = ref("refs/heads/main")
        base = merge_base(fork, upstream)
        if "\n" in base or base == upstream or version_at(upstream) != args.tag[1:]:
            raise SyncError("Expected a new release with a single merge base and matching Cargo version")
        if args.command == "plan":
            result = git("merge-tree", "--write-tree", "--name-only", "--no-messages", "-z", fork, upstream, ok=(0, 1))
            print(f"{args.tag}: {base[:12]} -> {upstream[:12]}")
            report("Predicted conflict paths", list(map(os.fsdecode, filter(None, result.split(b"\0")[1:]))))
        else:
            prepare(args.tag, upstream, version_at(base), base)
        return 0
    except (SyncError, OSError, ValueError, KeyError) as error:
        print(f"sync-upstream: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
