import contextlib
import importlib.machinery
import importlib.util
import io
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "aria2ctl"


def load_module():
    loader = importlib.machinery.SourceFileLoader("aria2ctl", str(SCRIPT))
    spec = importlib.util.spec_from_loader("aria2ctl", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


aria2ctl = load_module()
# setUp stubs out load_config so tests never read the real config file.
load_config = aria2ctl.load_config

GID = "2089b05ecca3d829"
OTHER_GID = "2089c11111111111"
STOPPED_GID = "d2e1aaaaaaaaaaaa"
REMOTE = ["--rpc-url", "http://nas:6800/jsonrpc"]


class FakeAria2:
    """Stands in for rpc_call. Records every call and answers from simple state."""

    def __init__(self):
        self.calls = []
        self.active = [GID]
        self.waiting = [OTHER_GID]
        self.stopped = [STOPPED_GID]
        self.global_dir = "/srv/dl"
        self.status = {}  # gid -> tellStatus result
        self.status_after_remove = "removed"

    def __call__(self, url, method, params, secret=""):
        self.calls.append((method, params, url, secret))
        if method == "aria2.tellActive":
            return [{"gid": g} for g in self.active]
        if method == "aria2.tellWaiting":
            return [{"gid": g} for g in self.waiting]
        if method == "aria2.tellStopped":
            return [{"gid": g} for g in self.stopped]
        if method == "aria2.getGlobalOption":
            return {"dir": self.global_dir}
        if method == "aria2.tellStatus":
            gid, keys = params
            if keys == ["status"]:
                return {"status": self.status_after_remove}
            return self.status[gid]
        if method in ("aria2.addTorrent", "aria2.addUri"):
            return "abcdef0123456789"
        return "OK"

    def methods(self):
        skip = {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}
        return [c[0] for c in self.calls if c[0] not in skip]

    def last(self, method):
        return [c for c in self.calls if c[0] == method][-1]


class Aria2ctlTestCase(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAria2()
        patches = [
            mock.patch.object(aria2ctl, "rpc_call", self.fake),
            mock.patch.object(aria2ctl, "load_config", lambda path: None),
            mock.patch.dict(os.environ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for key in ("ARIA2_RPC_URL", "ARIA2_RPC_SECRET", "ARIA2_LOCAL_DIR"):
            os.environ.pop(key, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))

    def run_cli(self, *argv, stdin=""):
        """Run main(); return (exit code or message, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["aria2ctl", *argv]), mock.patch.object(
            sys, "stdin", io.StringIO(stdin)
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                aria2ctl.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def touch(self, path, size=0):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path


class ResolveGidTest(Aria2ctlTestCase):
    def resolve(self, prefix):
        args = mock.Mock(rpc_url="", rpc_secret="")
        return aria2ctl.resolve_gid(args, prefix)

    def test_unique_prefix_expands(self):
        self.assertEqual(self.resolve("d2"), STOPPED_GID)

    def test_prefix_is_case_insensitive(self):
        self.assertEqual(self.resolve("2089B"), GID)

    def test_ambiguous_prefix_lists_candidates(self):
        with self.assertRaises(SystemExit) as cm:
            self.resolve("2089")
        self.assertIn("ambiguous", str(cm.exception))
        self.assertIn(GID, str(cm.exception))
        self.assertIn(OTHER_GID, str(cm.exception))

    def test_no_match(self):
        with self.assertRaises(SystemExit) as cm:
            self.resolve("ff")
        self.assertIn("No download matches", str(cm.exception))

    def test_full_gid_skips_lookup(self):
        self.assertEqual(self.resolve(GID.upper()), GID)
        self.assertEqual(self.fake.calls, [])

    def test_main_resolves_prefix_before_command(self):
        self.run_cli("pause", "d2e")
        self.assertEqual(self.fake.last("aria2.pause")[1], [STOPPED_GID])


class ConfigTest(Aria2ctlTestCase):
    def write_config(self, text, mode=0o600):
        path = self.tmp / "aria2ctl.env"
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_config_path_uses_xdg_config_home(self):
        os.environ["XDG_CONFIG_HOME"] = "/xdg"
        self.assertEqual(aria2ctl.config_path(), "/xdg/aria2/aria2ctl.env")

    def test_parses_comments_export_and_quotes(self):
        path = self.write_config(
            "# comment\n\nexport ARIA2_RPC_URL=\"http://cfg:1/jsonrpc\"\nARIA2_RPC_SECRET='s3cret'\n"
        )
        load_config(str(path))
        self.assertEqual(os.environ["ARIA2_RPC_URL"], "http://cfg:1/jsonrpc")
        self.assertEqual(os.environ["ARIA2_RPC_SECRET"], "s3cret")

    def test_environment_wins_over_file(self):
        os.environ["ARIA2_RPC_SECRET"] = "fromenv"
        load_config(str(self.write_config("ARIA2_RPC_SECRET=fromfile\n")))
        self.assertEqual(os.environ["ARIA2_RPC_SECRET"], "fromenv")

    def test_missing_file_is_ignored(self):
        load_config(str(self.tmp / "nope.env"))

    def test_malformed_line_exits_with_location(self):
        path = self.write_config("ARIA2_RPC_URL=x\nnonsense\n")
        with self.assertRaises(SystemExit) as cm:
            load_config(str(path))
        self.assertIn(":2:", str(cm.exception))

    def test_warns_when_readable_by_others(self):
        path = self.write_config("ARIA2_RPC_SECRET=x\n", mode=0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            load_config(str(path))
        self.assertIn("readable by other users", err.getvalue())

    def test_env_values_become_defaults_and_flags_override(self):
        os.environ["ARIA2_RPC_URL"] = "http://env:1/jsonrpc"
        os.environ["ARIA2_RPC_SECRET"] = "envsecret"
        self.run_cli("clean")
        _, _, url, secret = self.fake.last("aria2.purgeDownloadResult")
        self.assertEqual((url, secret), ("http://env:1/jsonrpc", "envsecret"))

        self.run_cli("--rpc-secret", "flagsecret", "clean")
        self.assertEqual(self.fake.last("aria2.purgeDownloadResult")[3], "flagsecret")


class AddTest(Aria2ctlTestCase):
    def test_torrent_file_added_paused(self):
        torrent = self.tmp / "x.torrent"
        torrent.write_bytes(b"d4:infoe")
        self.run_cli("add", str(torrent))
        _, params, _, _ = self.fake.last("aria2.addTorrent")
        self.assertEqual(params[0], "ZDQ6aW5mb2U=")
        self.assertEqual(params[2], {"pause": "true"})

    def test_upload_alias(self):
        torrent = self.tmp / "x.torrent"
        torrent.write_bytes(b"d4:infoe")
        self.run_cli("upload", str(torrent))
        self.assertIn("aria2.addTorrent", self.fake.methods())

    def test_magnet_pauses_after_metadata(self):
        code, out, _ = self.run_cli("add", "magnet:?xt=urn:btih:abc")
        _, params, _, _ = self.fake.last("aria2.addUri")
        self.assertEqual(params, [["magnet:?xt=urn:btih:abc"], {"pause-metadata": "true"}])
        self.assertIn("Followed By", out)

    def test_url_paused_by_default(self):
        self.run_cli("add", "https://example.com/a.iso")
        self.assertEqual(self.fake.last("aria2.addUri")[1][1], {"pause": "true"})

    def test_start_flag(self):
        self.run_cli("add", "https://example.com/a.iso", "--start")
        self.assertEqual(self.fake.last("aria2.addUri")[1][1], {})

    def test_rejects_unknown_source(self):
        code, _, _ = self.run_cli("add", "nonsense")
        self.assertIn("Not a file or a supported URL", code)
        self.assertEqual(self.fake.methods(), [])


class PauseResumeTest(Aria2ctlTestCase):
    def test_pause_one(self):
        self.run_cli("pause", GID)
        self.assertEqual(self.fake.methods(), ["aria2.pause"])

    def test_force_pause_all(self):
        self.run_cli("pause", "--all", "--force")
        self.assertEqual(self.fake.methods(), ["aria2.forcePauseAll"])

    def test_resume_all(self):
        self.run_cli("resume", "--all")
        self.assertEqual(self.fake.methods(), ["aria2.unpauseAll"])

    def test_requires_gid_or_all(self):
        for argv in (["pause"], ["resume", GID, "--all"]):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 2)
            self.assertIn("either a GID or --all", err)
        self.assertEqual(self.fake.methods(), [])


class CleanTest(Aria2ctlTestCase):
    def test_purges_and_reports_count(self):
        _, out, _ = self.run_cli("clean")
        self.assertEqual(self.fake.methods(), ["aria2.purgeDownloadResult"])
        self.assertIn("Cleared 1", out)


class RemoveLocalTest(Aria2ctlTestCase):
    def setUp(self):
        super().setUp()
        self.dl = self.tmp / "dl"
        self.touch(self.dl / "Show/a.mkv")
        self.touch(self.dl / "Show/sub/b.srt")
        self.touch(self.dl / "Show.aria2")
        self.touch(self.dl / "other.txt")
        self.secret = self.touch(self.tmp / "outside/secret")
        (self.dl / "Show/link").symlink_to(self.secret)
        self.fake.status[GID] = {
            "status": "active",
            "dir": str(self.dl),
            "bittorrent": {"info": {"name": "Show"}},
            "files": [
                {"path": str(self.dl / "Show/a.mkv")},
                {"path": str(self.dl / "Show/sub/b.srt")},
                {"path": str(self.dl / "Show/link")},
                {"path": str(self.dl / "../outside/secret")},
            ],
        }

    def test_decline_changes_nothing(self):
        _, out, _ = self.run_cli("remove", GID, "--delete-files", stdin="n\n")
        self.assertIn("Aborted", out)
        self.assertNotIn("aria2.remove", self.fake.methods())
        self.assertTrue((self.dl / "Show/a.mkv").exists())

    def test_accept_deletes_only_download_files(self):
        self.run_cli("remove", GID, "--delete-files", stdin="y\n")
        self.assertEqual(
            self.fake.methods(),
            ["aria2.tellStatus", "aria2.remove", "aria2.tellStatus", "aria2.removeDownloadResult"],
        )
        self.assertFalse((self.dl / "Show/a.mkv").exists())
        self.assertFalse((self.dl / "Show.aria2").exists())
        self.assertFalse((self.dl / "Show/sub").exists(), "empty dir should be removed")
        self.assertTrue((self.dl / "other.txt").exists())
        self.assertTrue(self.secret.exists(), "must not follow paths outside the download dir")

    def test_yes_skips_prompt(self):
        self.run_cli("remove", GID, "--delete-files", "-y")
        self.assertFalse((self.dl / "Show/a.mkv").exists())

    def test_force_remove(self):
        self.run_cli("remove", GID, "--force")
        self.assertIn("aria2.forceRemove", self.fake.methods())

    def test_stopped_download_only_clears_result(self):
        self.fake.status[GID]["status"] = "complete"
        self.run_cli("remove", GID)
        self.assertEqual(self.fake.methods(), ["aria2.tellStatus", "aria2.removeDownloadResult"])
        self.assertTrue((self.dl / "Show/a.mkv").exists())

    def test_times_out_if_download_never_stops(self):
        self.fake.status_after_remove = "active"
        with mock.patch.object(aria2ctl.time, "sleep"):
            code, _, _ = self.run_cli("remove", GID, "--delete-files", "-y")
        self.assertIn("Timed out", code)
        self.assertNotIn("aria2.removeDownloadResult", self.fake.methods())
        self.assertTrue((self.dl / "Show/a.mkv").exists())


class RemoveRemoteTest(Aria2ctlTestCase):
    def setUp(self):
        super().setUp()
        self.mnt = self.tmp / "mnt"
        self.touch(self.mnt / "tv/Show/a.mkv")
        self.touch(self.mnt / "tv/Show.aria2")
        self.touch(self.mnt / "keep.txt")
        self.fake.status[GID] = {
            "status": "paused",
            "dir": "/srv/dl/tv",
            "bittorrent": {"info": {"name": "Show"}},
            "files": [{"path": "/srv/dl/tv/Show/a.mkv"}, {"path": "/etc/passwd"}],
        }

    def assert_nothing_removed(self):
        self.assertNotIn("aria2.removeDownloadResult", self.fake.methods())
        self.assertTrue((self.mnt / "tv/Show/a.mkv").exists())

    def test_refuses_without_local_dir(self):
        code, _, _ = self.run_cli(*REMOTE, "remove", GID, "--delete-files", "-y")
        self.assertIn("--local-dir", code)
        self.assert_nothing_removed()

    def test_refuses_when_mount_missing(self):
        code, _, _ = self.run_cli(
            *REMOTE, "remove", GID, "--delete-files", "-y", "--local-dir", str(self.tmp / "nope")
        )
        self.assertIn("Is the share mounted", code)
        self.assert_nothing_removed()

    def test_refuses_dir_outside_root(self):
        self.fake.status[GID]["dir"] = "/other/tv"
        code, _, _ = self.run_cli(
            *REMOTE, "remove", GID, "--delete-files", "-y", "--local-dir", str(self.mnt)
        )
        self.assertIn("outside aria2's download root", code)
        self.assert_nothing_removed()

    def test_maps_server_paths_via_env(self):
        os.environ["ARIA2_LOCAL_DIR"] = str(self.mnt)
        self.run_cli(*REMOTE, "remove", GID, "--delete-files", "-y")
        self.assertFalse((self.mnt / "tv/Show/a.mkv").exists())
        self.assertFalse((self.mnt / "tv/Show.aria2").exists())
        self.assertTrue((self.mnt / "keep.txt").exists())

    def test_remove_without_delete_needs_no_local_dir(self):
        self.run_cli(*REMOTE, "remove", GID)
        self.assertIn("aria2.removeDownloadResult", self.fake.methods())


def write_control_file(path, piece_len, total_len, done, version=1):
    padded = done + [0] * (-len(done) % 8)
    bitfield = bytes(
        int("".join(map(str, padded[i : i + 8])), 2) for i in range(0, len(padded), 8)
    )
    order = ">" if version == 1 else "<"
    with open(path, "wb") as f:
        f.write(struct.pack(">H", version))
        f.write(struct.pack(order + "II", 0, 0))
        f.write(struct.pack(order + "IQQI", piece_len, total_len, 0, len(bitfield)))
        f.write(bitfield)


class TruncateTest(Aria2ctlTestCase):
    def setUp(self):
        super().setUp()
        self.mnt = self.tmp / "mnt"
        self.data = self.touch(self.mnt / "iso/big.iso", size=9500)
        # 10 pieces of 1000 bytes: 0-3 done, gap at 4, then 6 and 7 done.
        write_control_file(
            str(self.data) + ".aria2", 1000, 9500, [1, 1, 1, 1, 0, 0, 1, 1, 0, 0]
        )
        self.fake.status[GID] = {
            "status": "paused",
            "dir": "/srv/dl/iso",
            "files": [{"path": "/srv/dl/iso/big.iso"}],
        }

    def truncate(self, *extra):
        return self.run_cli(*REMOTE, "truncate", GID, "--local-dir", str(self.mnt), *extra)

    def test_dry_run_reports_without_changing(self):
        _, out, _ = self.truncate()
        self.assertIn("Contiguous prefix : 4000 bytes", out)
        self.assertIn("2 completed piece(s)", out)
        self.assertEqual(self.data.stat().st_size, 9500)

    def test_apply_truncates_to_prefix(self):
        self.truncate("--apply")
        self.assertEqual(self.data.stat().st_size, 4000)
        _, out, _ = self.truncate("--apply")
        self.assertIn("Nothing to do", out)
        self.assertNotIn("completed piece(s)", out)

    def test_version_0_little_endian(self):
        write_control_file(str(self.data) + ".aria2", 1000, 9500, [1, 1, 0], version=0)
        self.truncate("--apply")
        self.assertEqual(self.data.stat().st_size, 2000)

    def test_refuses_torrents(self):
        self.fake.status[GID]["bittorrent"] = {}
        code, _, _ = self.truncate("--apply")
        self.assertIn("not torrents", code)
        self.assertEqual(self.data.stat().st_size, 9500)

    def test_refuses_active_download(self):
        self.fake.status[GID]["status"] = "active"
        code, _, _ = self.truncate("--apply")
        self.assertIn("pause it first", code)
        self.assertEqual(self.data.stat().st_size, 9500)

    def test_refuses_remote_without_local_dir(self):
        code, _, _ = self.run_cli(*REMOTE, "truncate", GID, "--apply")
        self.assertIn("--local-dir", code)
        self.assertEqual(self.data.stat().st_size, 9500)

    def test_missing_control_file(self):
        os.remove(str(self.data) + ".aria2")
        code, _, _ = self.truncate("--apply")
        self.assertIn("Control file", code)


class FilesAndStatusTest(Aria2ctlTestCase):
    def test_files_lists_selection(self):
        files = [
            {"index": "1", "length": "2048", "selected": "true", "path": "/d/a.mkv"},
            {"index": "2", "length": "10", "selected": "false", "path": "/d/b.nfo"},
        ]
        with mock.patch.object(aria2ctl, "rpc_call", lambda *a: files):
            _, out, _ = self.run_cli("files", GID)
        self.assertRegex(out, r"1\s+\|\s+2\.00 KiB \| yes\s+\| /d/a\.mkv")
        self.assertRegex(out, r"2\s+\|.*\| no\s+\| /d/b\.nfo")

    def test_status_shows_followed_by(self):
        self.fake.status[GID] = {"gid": GID, "status": "complete", "followedBy": [OTHER_GID]}
        _, out, _ = self.run_cli("status", GID)
        self.assertIn(f"Followed By:     {OTHER_GID}", out)


if __name__ == "__main__":
    unittest.main()
