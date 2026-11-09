#!/usr/bin/env python3
"""Validate manually reviewed fork releases and prepare their local release data."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib


TAG_RE = re.compile(
    r"v(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))"
    r"-ru\.(?P<revision>[1-9][0-9]*)"
)
SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class ReleaseError(Exception):
    """An invalid release input or an unsupported manifest shape."""


def release_parts(tag: str) -> tuple[str, str]:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseError("tag must have the form vX.Y.Z-ru.N, with N >= 1")
    return match["version"], match["revision"]


def validate_sha(sha: str) -> str:
    if SHA_RE.fullmatch(sha) is None:
        raise ReleaseError("SHA must contain exactly 40 hexadecimal characters")
    return sha.lower()


def validate_repository(repository: str) -> str:
    parts = repository.split("/")
    if (
        len(parts) != 2
        or OWNER_RE.fullmatch(parts[0]) is None
        or "--" in parts[0]
        or REPOSITORY_RE.fullmatch(parts[1]) is None
        or parts[1] in {".", ".."}
    ):
        raise ReleaseError("repository must be a GitHub owner/repository, not a URL or path")
    return repository


def git(*arguments: str) -> bytes:
    environment = os.environ.copy()
    # Read only local objects, including in a partial clone; never fetch lazily.
    environment["GIT_NO_LAZY_FETCH"] = "1"
    # Also block transports on Git versions that do not honor the lazy-fetch guard.
    environment["GIT_ALLOW_PROTOCOL"] = ""
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "--no-pager", "--no-replace-objects", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseError(f"git {arguments[0]} failed: {detail or 'no diagnostic'}")
    return result.stdout


def prepare(arguments: argparse.Namespace) -> None:
    if not arguments.ref.startswith("refs/tags/"):
        raise ReleaseError("prepare requires a full refs/tags/vX.Y.Z-ru.N ref")
    tag = arguments.ref.removeprefix("refs/tags/")
    version, _ = release_parts(tag)
    sha = validate_sha(arguments.sha)
    resolved = git(
        "rev-parse", "--verify", "--end-of-options", f"{arguments.ref}^{{commit}}"
    ).decode("ascii").strip()
    if SHA_RE.fullmatch(resolved) is None or resolved.lower() != sha:
        raise ReleaseError(f"tag {tag} does not resolve to the requested commit {sha}")

    cargo_bytes = git("cat-file", "blob", f"{sha}:Cargo.toml")
    try:
        cargo = tomllib.loads(cargo_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"Cargo.toml at {sha} is not valid UTF-8 TOML: {error}") from error
    try:
        cargo_version = cargo["workspace"]["package"]["version"]
    except (KeyError, TypeError) as error:
        raise ReleaseError(f"Cargo.toml at {sha} has no workspace.package.version") from error
    if cargo_version != version:
        raise ReleaseError(
            f"tag version {version} does not match workspace.package.version {cargo_version!r}"
        )
    if git("cat-file", "-t", f"{sha}:po/ru.po").strip() != b"blob":
        raise ReleaseError(f"po/ru.po at {sha} must be a file")

    # Do not emit partial GITHUB_OUTPUT data before every validation succeeds.
    print(f"tag={tag}\nsha={sha}\nversion={version}")


def pin_flatpak(arguments: argparse.Namespace) -> None:
    sha = validate_sha(arguments.sha)
    repository = validate_repository(arguments.repository)
    original = arguments.manifest.read_bytes()
    text = original.decode("utf-8")
    applications = list(re.finditer(r"^  - name: rustconn *$", text, re.MULTILINE))
    if len(applications) != 1 or "\r" in text:
        raise ReleaseError("expected one plain rustconn module in an LF-formatted manifest")
    start = applications[0].end() + 1
    application = text[start:]
    # ponytail: support the two reviewed templates, with rustconn last.
    # Use a YAML parser if the manifest layout needs to become more general.
    if re.search(r"^\S|^  - |^    (?:name|modules):", application, re.MULTILINE):
        raise ReleaseError("rustconn must be the final, non-nested module")
    sources = list(re.finditer(r"^    sources:\n", application, re.MULTILINE))
    if len(sources) != 1:
        raise ReleaseError("expected one block-style rustconn sources list")
    source_start = sources[0].end()
    lines = application[source_start:].splitlines()
    if (
        len(lines) < 3
        or lines[0] != "      - type: git"
        or lines[-1] != "      - cargo-sources.json"
    ):
        raise ReleaseError("expected one application Git source and cargo-sources.json")
    fields = set()
    in_checker = False
    for line in lines[1:-1]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if in_checker and line.startswith("          "):
            continue
        in_checker = False
        if line == "        x-checker-data:":
            key = "x-checker-data"
            in_checker = True
        else:
            match = re.fullmatch(r"        (url|tag|branch|commit): \S.*", line)
            if match is None:
                raise ReleaseError("unexpected application Git source field or indentation")
            key = match[1]
        if key in fields:
            raise ReleaseError(f"duplicate application Git source field: {key}")
        fields.add(key)
    if "url" not in fields:
        raise ReleaseError("application Git source must contain a URL")
    replacement = (
        "      - type: git\n"
        f"        url: https://github.com/{repository}.git\n"
        f"        commit: {sha}\n"
        "      - cargo-sources.json\n"
    )
    prefix = text[:start] + application[:source_start]
    arguments.manifest.write_bytes((prefix + replacement).encode("utf-8"))


def notes(arguments: argparse.Namespace) -> None:
    version, _ = release_parts(arguments.tag)
    repository = validate_repository(arguments.repository)
    upstream_body = arguments.upstream_notes.read_bytes()
    deb = f"rustconn_{version}_amd64.deb"
    rpm = f"rustconn-{version}-1.x86_64.rpm"
    appimage = f"RustConn-{version}-x86_64.AppImage"
    flatpak = f"RustConn-{version}.flatpak"
    header = f"""# RustConn-ru {arguments.tag}

Это релиз русскоязычного форка [{repository}](https://github.com/{repository}),
а не официальный релиз upstream. Основа: [RustConn v{version}](https://github.com/totoshko88/RustConn/releases/tag/v{version}).

Имя установленной программы и пакета остаётся `rustconn`, идентификатор Flatpak —
`io.github.totoshko88.RustConn`. Форк не предназначен для параллельной установки
с upstream-пакетом с тем же именем или идентификатором.

## Загрузка и проверка

Для проверки полного набора скачайте все четыре файла и `SHA256SUMS` именно из
этого релиза форка. Команды требуют установленного GitHub CLI (`gh`):

```sh
gh release download {arguments.tag} --repo {repository} \\
  --pattern '{deb}' \\
  --pattern '{rpm}' \\
  --pattern '{appimage}' \\
  --pattern '{flatpak}' \\
  --pattern 'SHA256SUMS'
sha256sum -c SHA256SUMS
gh attestation verify {deb} --repo {repository}
gh attestation verify {rpm} --repo {repository}
gh attestation verify {appimage} --repo {repository}
gh attestation verify {flatpak} --repo {repository}
```

## Установка

Выберите **один** подходящий формат для Linux x86_64.

### Debian / Ubuntu (.deb)

```sh
sudo apt install ./{deb}
```

### Fedora / RPM (.rpm)

```sh
sudo dnf install ./{rpm}
```

### AppImage

```sh
chmod +x {appimage}
./{appimage}
```

### Загружаемый Flatpak (.flatpak)

```sh
flatpak install --user ./{flatpak}
flatpak run io.github.totoshko88.RustConn
```

Публикация пакета AUR `rustconn-ru` **не предполагается и не гарантируется**.
Перед использованием AUR самостоятельно проверьте наличие, владельца и содержимое
такого пакета; инструкции выше используют только файлы данного релиза.

## Исходные заметки upstream (дословно)

> **Внимание: инструкции upstream — не инструкции форка.** Текст ниже сохранён
> без изменений как данные из исходного релиза. Его ссылки, имена пакетов,
> репозитории и команды установки относятся к upstream RustConn, а не к
> RustConn-ru. Для этого форка используйте инструкции и файлы выше.

---

"""
    arguments.output.write_bytes(header.encode("utf-8") + upstream_body)


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("prepare", help="validate the reviewed release tag and source")
    command.add_argument("--ref", required=True)
    command.add_argument("--sha", required=True)
    command.set_defaults(handler=prepare)

    command = commands.add_parser("pin-flatpak", help="pin only the application Git source")
    command.add_argument("--manifest", required=True, type=Path)
    command.add_argument("--sha", required=True)
    command.add_argument("--repository", required=True)
    command.set_defaults(handler=pin_flatpak)

    command = commands.add_parser("notes", help="combine fork instructions with upstream notes as data")
    command.add_argument("--tag", required=True)
    command.add_argument("--repository", required=True)
    command.add_argument("--upstream-notes", required=True, type=Path)
    command.add_argument("--output", required=True, type=Path)
    command.set_defaults(handler=notes)
    return parser


def main() -> int:
    parser = argument_parser()
    arguments = parser.parse_args()
    try:
        arguments.handler(arguments)
    except (ReleaseError, OSError, UnicodeError) as error:
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
