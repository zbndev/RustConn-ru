#!/usr/bin/env python3
"""Regression check for semantic catalog merges using Git blobs and GNU gettext."""

from contextlib import chdir, redirect_stdout
from io import StringIO
from pathlib import Path
import runpy
import subprocess
from tempfile import TemporaryDirectory
import unittest


HELPER = runpy.run_path(str(Path(__file__).with_name("sync-upstream.py")))
HEADER = b'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'


def po(**translations):
    return b"\n".join(
        [HEADER, *(f'msgid "{key}"\nmsgstr "{value}"\n'.encode()
                   for key, value in translations.items())]
    )


def write_stages(base, fork, upstream):
    return {
        stage: ("100644", subprocess.check_output(["git", "hash-object", "-w", "--stdin"], input=contents).decode().strip())
        for stage, contents in enumerate((base, fork, upstream), 1)
    }


class CatalogMergeTest(unittest.TestCase):
    def test_preserves_independent_edits_and_rejects_divergent_active_translation(self):
        base = po(About="About RustConn", Copy="Copy", Old="Old wording")
        fork = po(About="About RustConnRu", Copy="Copy", Old="Fork wording", Branded="RustConnRu")
        upstream = po(About="About RustConn", Copy="Copy selected item", Old="Upstream wording", New="New translation")
        active = HELPER["po_entries"](po(About="", Copy="", Branded="", New=""))[1]
        newly_active = HELPER["po_entries"](po(Old=""))[1]
        with TemporaryDirectory() as tmp, chdir(tmp):
            subprocess.run(["git", "init", "--quiet"], check=True)
            stages = write_stages(base, fork, upstream)
            merged = HELPER["catalog_seed"]("po/en.po", stages, active)
            entries = HELPER["po_entries"](merged)[1]
            for key, translation in {
                "About": "About RustConnRu", "Copy": "Copy selected item",
                "Branded": "RustConnRu", "New": "New translation",
            }.items():
                self.assertIn(f'msgstr "{translation}"'.encode(), entries[(f'msgid "{key}"'.encode(),)][1])
            report = StringIO()
            with redirect_stdout(report):
                rejected = HELPER["catalog_seed"]("po/en.po", stages, active | newly_active)
            self.assertIsNone(rejected)
            self.assertIn('msgid "Old"', report.getvalue())


if __name__ == "__main__":
    unittest.main()
