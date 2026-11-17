import base64
import io
import os
import shutil
import subprocess
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from subusage import __version__, update
from subusage.core import HttpError, ProviderError

from .helpers import FakeHTTP, HomeTestCase


class VersionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(update.parse_version("v2.1.0"), (2, 1, 0))
        self.assertEqual(update.parse_version("2.10"), (2, 10, 0))
        self.assertEqual(update.parse_version("3.0.0-beta.1"), (3, 0, 0))
        self.assertGreater(update.parse_version("2.10.0"), update.parse_version("2.9.9"))

    def test_version_string(self):
        self.assertTrue(update.version_string().startswith(__version__))


LOG = """# Changelog

intro text

## 2.4.0
- **Big** thing

## 2.3.1
- fix

## [2.3.0] - 2026-11-17
- older

## 2.2.0
- ancient
"""


class NotesTests(unittest.TestCase):
    def test_parse_and_between(self):
        self.assertEqual([v for v, _ in update.parse_changelog(LOG)], ["2.4.0", "2.3.1", "2.3.0", "2.2.0"])
        notes = update.notes_between(LOG, "2.3.0", "2.4.0")
        self.assertIn("## 2.4.0", notes)
        self.assertIn("## 2.3.1", notes)
        self.assertNotIn("older", notes)
        self.assertEqual(update.notes_between(LOG, "2.4.0", "2.4.0"), "")

    def test_every_version_has_notes(self):
        """A version bump without a CHANGELOG entry fails CI (the release workflow refuses it too)."""
        root = Path(__file__).resolve().parent.parent
        notes = dict(update.parse_changelog((root / "CHANGELOG.md").read_text(encoding="utf-8")))
        self.assertTrue(notes.get(__version__, "").strip(), f"CHANGELOG.md needs a '## {__version__}' section")


class CheckTests(unittest.TestCase):
    def test_release(self):
        log = f"## 99.0.0\n- new stuff\n\n## 98.0.0\n- skipped release\n\n## {__version__}\n- yours\n"
        http = FakeHTTP({"/releases/latest": {"tag_name": "v99.0.0", "html_url": "https://x/rel", "body": "body",
                                              "zipball_url": "https://api/zip/v99.0.0"},
                         "/contents/CHANGELOG.md?ref=v99.0.0": {"content": base64.b64encode(log.encode()).decode()}})
        with mock.patch.object(update, "http_json", http):
            info = update.check()
        self.assertTrue(info.available)
        self.assertEqual((info.latest, info.ref), ("99.0.0", "v99.0.0"))
        self.assertIn("new stuff", info.notes)
        self.assertIn("skipped release", info.notes)     # notes for every version since yours
        self.assertNotIn("yours", info.notes)

    def test_release_body_when_changelog_unavailable(self):
        http = FakeHTTP({"/releases/latest": {"tag_name": "v99.0.0", "body": "from the release"},
                         "/contents/CHANGELOG.md": HttpError(404, "")})
        with mock.patch.object(update, "http_json", http):
            self.assertEqual(update.check().notes, "from the release")

    def test_fallback_to_main(self):
        content = base64.b64encode(f'__version__ = "{__version__}"\n'.encode()).decode()
        http = FakeHTTP({"/releases/latest": HttpError(404, ""), "/contents/subusage/__init__.py": {"content": content}})
        with mock.patch.object(update, "http_json", http):
            info = update.check()
        self.assertFalse(info.available)
        self.assertEqual(info.ref, "main")
        self.assertIn("ref=main", http.last("contents")["url"])

    def test_errors_propagate(self):
        with mock.patch.object(update, "http_json", FakeHTTP({"/releases/latest": HttpError(403, "rate limit")})):
            with self.assertRaises(HttpError):
                update.check()


def make_zip(version: str, top: str = "chrisdelrey-ai_subusage-abc123") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{top}/subusage/__init__.py", f'__version__ = "{version}"\n')
        z.writestr(f"{top}/subusage/new_module.py", "X = 1\n")
        z.writestr(f"{top}/subusage.pyw", "# launcher\n")
        z.writestr(f"{top}/README.md", "new readme\n")
        z.writestr(f"{top}/examples/custom_provider.py", "# example\n")
    return buf.getvalue()


class ApplyTests(HomeTestCase):
    def source_install(self) -> Path:
        root = self.home / "SubUsage"
        (root / "subusage").mkdir(parents=True)
        (root / "subusage" / "__init__.py").write_text('__version__ = "1.0.0"\n')
        (root / "subusage" / "stale.py").write_text("old\n")
        (root / "subusage.pyw").write_text("# old\n")
        return root

    def test_install_kind(self):
        root = self.source_install()
        self.assertEqual(update.install_kind(root), "source")
        (root / ".git").mkdir()
        self.assertEqual(update.install_kind(root), "git")
        self.assertEqual(update.install_kind(self.home / "site-packages"), "pip")

    def test_source_zip(self):
        root = self.source_install()
        info = update.UpdateInfo("1.0.0", "9.9.9", "v9.9.9", "https://zip", "https://page")
        msg = update._apply_source(info, root, data=make_zip("9.9.9"))
        self.assertIn("9.9.9", msg)
        self.assertIn("9.9.9", (root / "subusage/__init__.py").read_text())
        self.assertTrue((root / "subusage/new_module.py").exists())
        self.assertFalse((root / "subusage/stale.py").exists())     # package replaced wholesale
        self.assertEqual((root / "subusage/BUILD").read_text(), "v9.9.9")
        self.assertEqual((root / "README.md").read_text(), "new readme\n")
        self.assertFalse(list(root.glob("subusage.old-*")))

    def test_source_zip_rejects_bad_archives(self):
        root = self.source_install()
        info = update.UpdateInfo("1.0.0", "9.9.9", "v9.9.9", "", "")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("x/../../evil.py", "boom")
        with self.assertRaisesRegex(ProviderError, "unsafe"):
            update._apply_source(info, root, data=buf.getvalue())
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("x/readme.txt", "not subusage")
        with self.assertRaisesRegex(ProviderError, "doesn't contain"):
            update._apply_source(info, root, data=buf.getvalue())
        self.assertIn("1.0.0", (root / "subusage/__init__.py").read_text())  # untouched

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_git_pull(self):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        g = lambda *a, cwd=None: subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True)  # noqa: E731
        origin, work, clone = self.home / "origin.git", self.home / "work", self.home / "clone"
        g("init", "-q", "--bare", str(origin))
        g("clone", "-q", str(origin), str(work))
        (work / "f.txt").write_text("1")
        g("add", ".", cwd=work)
        g("commit", "-q", "-m", "one", cwd=work)
        g("push", "-q", "origin", "HEAD", cwd=work)
        g("clone", "-q", str(origin), str(clone))
        (work / "f.txt").write_text("2")
        g("commit", "-q", "-am", "two", cwd=work)
        g("push", "-q", "origin", "HEAD", cwd=work)
        self.assertEqual(update._apply_git(clone), "Updated with git pull")
        self.assertEqual((clone / "f.txt").read_text(), "2")
        self.assertEqual(update._apply_git(clone), "Already up to date")
        (clone / "f.txt").write_text("local edit")
        with self.assertRaisesRegex(ProviderError, "local changes"):
            update._apply_git(clone)

    def test_pip_command(self):
        info = update.UpdateInfo("1.0.0", "9.9.9", "v9.9.9", "", "")
        with mock.patch.object(update, "_run", return_value="") as run:
            update._apply_pip(info)
        args = run.call_args[0][0]
        self.assertEqual(args[1:5], ["-m", "pip", "install", "--upgrade"])
        self.assertTrue(args[-1].endswith("ai_subusage.git@v9.9.9"))


if __name__ == "__main__":
    unittest.main()
