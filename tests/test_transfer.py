import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from aiohttp import web
import yaml
import transfer as tr


def config(base):
    cfg = yaml.safe_load(tr.DEFAULT_CONFIG.replace("__TOKEN__", "test-token-0123456789"))
    cfg["_base"] = Path(base)
    cfg["_path"] = Path(base) / "config.yaml"
    cfg["sender"]["bind_ip"] = "127.0.0.1"
    cfg["sender"]["advertised_ip"] = "127.0.0.1"
    cfg["receiver"]["source_ip"] = "127.0.0.1"
    cfg["transfer"].update(connections_per_file=4, refresh_below_connections=2,
                           lowest_speed_kib=0, poll_seconds=1, max_error_retries=0,
                           save_interval_seconds=1)
    return cfg


class TestServer:
    """Real loopback HTTP server, optionally bandwidth-limited for resume tests."""
    def __init__(self, app):
        self.app = app
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        asyncio.set_event_loop(self.loop)
        async def start():
            self.runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=0.1)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
            await self.site.start()
            self.port = self.site._server.sockets[0].getsockname()[1]
            self.ready.set()
        self.loop.run_until_complete(start())
        self.loop.run_forever()
        self.loop.run_until_complete(self.runner.cleanup())
        self.loop.close()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("test HTTP server failed")
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *exc):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)


class ThrottledServer(TestServer):
    def __init__(self, size=32 * tr.MIB, delay=0.01):
        self.size, self.delay = size, delay
        self.ranges = []
        self.connections = self.peak = 0
        # Deterministic data permits inexpensive tests without large fixtures.
        self.entry = {"id": "a" * 64, "path": "large.bin", "size": size,
                      "sha256": self.digest(size)}
        app = web.Application()
        async def manifest(request):
            return web.json_response({"version": 1, "files": [self.entry], "directories": []})
        app.router.add_get("/manifest", manifest)
        app.router.add_get("/files/{file_id}", self.content)
        super().__init__(app)

    @staticmethod
    def digest(size):
        digest = hashlib.sha256()
        block = b"x" * tr.MIB
        while size:
            take = min(size, len(block))
            digest.update(block[:take])
            size -= take
        return digest.hexdigest()

    async def content(self, request):
        start, end, status = 0, self.size - 1, 200
        if "Range" in request.headers:
            value = request.headers["Range"].removeprefix("bytes=")
            first, last = value.split("-")
            start, end, status = int(first), int(last) if last else end, 206
        self.ranges.append((start, end))
        headers = {"Content-Length": str(end - start + 1), "Accept-Ranges": "bytes"}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{self.size}"
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        if request.method == "HEAD":
            return response
        self.connections += 1
        self.peak = max(self.peak, self.connections)
        remaining = end - start + 1
        try:
            while remaining:
                count = min(16 * 1024, remaining)
                await response.write(b"x" * count)
                remaining -= count
                await asyncio.sleep(self.delay)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.connections -= 1
        return response


def wait_status(aria, gid, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = aria.rpc("tellStatus", gid)
        if predicate(last):
            return last
        time.sleep(0.15)
    raise AssertionError(f"timeout: {last}")


class UnitTests(unittest.TestCase):
    def test_refresh_requires_sustained_low_connections_and_resets(self):
        t = config(".")["transfer"]
        t.update(connections_per_file=64, refresh_below_connections=32)
        policy = tr.RefreshPolicy(t, 0)
        status = dict(status="active", totalLength=str(1024 * tr.MIB),
                      completedLength=str(100 * tr.MIB), connections="31")
        self.assertFalse(policy.should_refresh(status, 29))
        self.assertFalse(policy.should_refresh(status, 30))
        self.assertFalse(policy.should_refresh(status, 59))
        self.assertTrue(policy.should_refresh(status, 60))
        policy.refreshed(60)
        self.assertFalse(policy.should_refresh(status, 119))
        self.assertFalse(policy.should_refresh(status, 120))
        self.assertTrue(policy.should_refresh(status, 150))
        status["connections"] = "32"
        self.assertFalse(policy.should_refresh(status, 151))
        status["connections"] = "1"
        self.assertFalse(policy.should_refresh(status, 152))
        status["completedLength"] = str(1020 * tr.MIB)
        self.assertFalse(policy.should_refresh(status, 200))
        status["completedLength"] = "0"
        status["verifiedLength"] = "1"
        self.assertFalse(policy.should_refresh(status, 250))

    def test_path_traversal_and_symlink_rejected(self):
        for name in ("../x", "/tmp/x", "a/../x", "a//b", ".download-tools/x", "a\\b", "a\nb"):
            with self.assertRaises(ValueError):
                tr.safe_relative(name)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "escape").symlink_to("/tmp")
            with self.assertRaises(ValueError):
                tr.safe_target(root, "escape/file")

    def test_selection_directory_and_multiple_files(self):
        manifest = {"files": [{"path": "data/a"}, {"path": "data/sub/b"}, {"path": "other"}],
                    "directories": ["data", "data/sub", "empty"]}
        files, dirs = tr.select_entries(manifest, ["data/"], False)
        self.assertEqual([f["path"] for f in files], ["data/a", "data/sub/b"])
        self.assertEqual(dirs, ["data", "data/sub"])
        files, _ = tr.select_entries(manifest, ["data/a", "other"], False)
        self.assertEqual(len(files), 2)

    def test_config_is_private_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.yaml"
            tr.init_config(path)
            content = path.read_bytes()
            tr.init_config(path)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(tr.load_config(path)["transfer"]["connections_per_file"], 64)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        tr.STOP.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.cfg = config(self.base)

    def tearDown(self):
        self.temp.cleanup()
        tr.STOP.clear()

    def test_share_auth_range_and_changed_source(self):
        source = self.base / "source.bin"
        source.write_bytes(b"0123456789" * 100)
        (self.base / "source.bin.gz").write_bytes(b"not-selected")
        manifest, lookup = tr.build_manifest([source])
        token = self.cfg["auth"]["token"]
        with TestServer(tr.make_app(manifest, lookup, token)) as server:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with self.assertRaises(urllib.error.HTTPError) as context:
                tr.http_json(server.url + "/manifest")
            self.assertEqual(context.exception.code, 401)
            context.exception.close()
            self.assertEqual(tr.http_json(server.url + "/manifest", token=token), manifest)
            url = server.url + "/files/" + manifest["files"][0]["id"]
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Range": "bytes=10-19"})
            with opener.open(request) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"0123456789")
            request.add_header("Accept-Encoding", "gzip")
            with self.assertRaises(urllib.error.HTTPError) as context:
                opener.open(request)
            self.assertEqual(context.exception.code, 406)
            context.exception.close()
            source.write_bytes(b"changed")
            request.remove_header("Accept-encoding")
            with self.assertRaises(urllib.error.HTTPError) as context:
                opener.open(request)
            self.assertEqual(context.exception.code, 409)
            context.exception.close()

    def test_corrupt_transfer_never_publishes_final_file(self):
        source = self.base / "corrupt.bin"
        source.write_bytes(b"data" * (tr.MIB // 4))
        manifest, lookup = tr.build_manifest([source])
        entry = manifest["files"][0]
        entry["sha256"] = "0" * 64
        destination = self.base / "destination"
        destination.mkdir()
        state = destination / ".download-tools"
        state.mkdir()
        with TestServer(tr.make_app(manifest, lookup, self.cfg["auth"]["token"])) as server:
            with tr.Aria2(self.cfg) as aria:
                with self.assertRaises(RuntimeError):
                    tr.download_file(aria, server.url, entry, destination, state)
        self.assertFalse((destination / entry["path"]).exists())

    def test_full_cli_multiple_files_empty_directory_and_resume_skip(self):
        source = self.base / "selected folder"
        (source / "empty").mkdir(parents=True)
        (source / "中文 文件.bin").write_bytes(os.urandom(2 * tr.MIB + 17))
        (source / "zero").touch()
        other = self.base / "other.bin"
        other.write_bytes(os.urandom(3 * tr.MIB))
        manifest, lookup = tr.build_manifest([source, other])
        with TestServer(tr.make_app(manifest, lookup, self.cfg["auth"]["token"])) as server:
            cfg = copy.deepcopy(self.cfg)
            cfg.pop("_base")
            cfg.pop("_path")
            cfg["receiver"]["port"] = server.port
            cfg["receiver"]["output_dir"] = str(self.base / "output")
            config_file = self.base / "config.yaml"
            config_file.write_text(yaml.safe_dump(cfg))
            command = [sys.executable, str(tr.ROOT / "transfer.py"), "--config", str(config_file), "download", "--all"]
            first = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            for entry in manifest["files"]:
                self.assertEqual(tr.sha256(self.base / "output" / entry["path"]), entry["sha256"])
            self.assertTrue((self.base / "output/selected folder/empty").is_dir())
            second = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("跳过", second.stdout)

    def test_real_aria_pause_restart_and_checksum(self):
        state = self.base / "state"
        state.mkdir()
        with ThrottledServer(size=24 * tr.MIB) as server:
            with tr.Aria2(self.cfg) as aria:
                options = aria.options(server.entry, state)
                gid = aria.rpc("addUri", [server.url + "/files/" + server.entry["id"]], options)
                before = wait_status(aria, gid, lambda s: int(s["completedLength"]) >= 4 * tr.MIB)
                aria.pause_and_resume(gid)
                after = wait_status(aria, gid, lambda s: s["status"] == "active")
                self.assertGreaterEqual(int(after["completedLength"]), int(before["completedLength"]))
            control = state / (options["out"] + ".aria2")
            self.assertTrue(control.is_file(), "graceful shutdown must preserve aria2 state")
            # A completely new aria2 process resumes and verifies all bytes.
            with tr.Aria2(self.cfg) as aria:
                gid = aria.rpc("addUri", [server.url + "/files/" + server.entry["id"]], options)
                resumed = wait_status(aria, gid, lambda s: int(s["completedLength"]) >= int(before["completedLength"]))
                completed = wait_status(aria, gid, lambda s: s["status"] in ("complete", "error"), timeout=60)
                self.assertEqual(completed["status"], "complete", completed)
            self.assertEqual(tr.sha256(state / options["out"]), server.entry["sha256"])
            self.assertTrue(any(start > 0 for start, end in server.ranges))

    def test_upstream_low_speed_eviction(self):
        state = self.base / "slow"
        state.mkdir()
        self.cfg["transfer"].update(connections_per_file=1, refresh_below_connections=0, lowest_speed_kib=1024)
        with ThrottledServer(size=8 * tr.MIB, delay=0.1) as server:
            with tr.Aria2(self.cfg) as aria:
                options = aria.options(server.entry, state)
                options["max-tries"] = "1"
                gid = aria.rpc("addUri", [server.url + "/files/" + server.entry["id"]], options)
                result = wait_status(aria, gid, lambda s: s["status"] == "error", timeout=35)
                self.assertEqual(result["errorCode"], "5", result)
                self.assertIn("Too slow Downloading speed", aria.log_path.read_text())

    def test_custom_binary_can_open_512_connections(self):
        state = self.base / "many"
        state.mkdir()
        self.cfg["transfer"].update(max_files=1, connections_per_file=512, refresh_below_connections=32)
        with ThrottledServer(size=2 * 1024 * tr.MIB, delay=0.03) as server:
            with tr.Aria2(self.cfg) as aria:
                options = aria.options(server.entry, state)
                gid = aria.rpc("addUri", [server.url + "/files/" + server.entry["id"]], options)
                result = wait_status(aria, gid, lambda s: int(s.get("connections", 0)) == 512 and server.peak >= 512, timeout=30)
                self.assertEqual(int(result["connections"]), 512)
                self.assertGreaterEqual(server.peak, 512)


if __name__ == "__main__":
    unittest.main()
