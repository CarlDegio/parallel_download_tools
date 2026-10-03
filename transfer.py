#!/usr/bin/env python3
"""Selected-file HTTP sharing and supervised, resumable aria2 downloads."""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import resource
import secrets
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parent
PRINT_LOCK = threading.Lock()
STOP = threading.Event()
MIB = 1024 * 1024

DEFAULT_CONFIG = """# Local configuration: ignored by Git. Copy securely between trusted machines.
# IPs/ports belong here, not in scripts. Paths are relative to this YAML file.
auth:
  token: "__TOKEN__"  # Both machines must use the same token.
sender:
  bind_ip: "0.0.0.0"
  port: 8080
  advertised_ip: "CHANGE_ME"
  tls_cert: null
  tls_key: null
  paths: []  # e.g. ["/data/model.bin", "/data/dataset"]
receiver:
  source_ip: "CHANGE_ME"
  port: 8080
  scheme: "http"  # Use https and sender certificates for public networks.
  ca_file: null   # Optional custom CA; certificate verification always enabled.
  output_dir: "downloads"
  rpc_port: 0     # 0 = select a free local port. RPC always binds to loopback.
transfer:
  max_files: 16
  connections_per_file: 64  # Custom binary supports 1..512.
  min_split_mib: 1
  lowest_speed_kib: 32     # 0 disables slow-connection eviction.
  connect_timeout_seconds: 15
  network_timeout_seconds: 60
  refresh_below_connections: 32
  refresh_low_seconds: 30
  startup_grace_seconds: 30
  refresh_cooldown_seconds: 60
  refresh_min_remaining_mib: 64  # Avoid refreshing small files / nearly finished files.
  max_refreshes: 20       # Fail with resumable data retained if exhausted.
  max_error_retries: 10
  retry_wait_seconds: 5
  poll_seconds: 2
  save_interval_seconds: 5
  status_interval_seconds: 10
"""


def log(message):
    with PRINT_LOCK:
        print(time.strftime("%H:%M:%S"), message, flush=True)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(4 * MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def signature(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def init_config(path):
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        log(f"保留已有配置：{path}")
        return
    with os.fdopen(fd, "w") as stream:
        stream.write(DEFAULT_CONFIG.replace("__TOKEN__", secrets.token_urlsafe(32)))
    log(f"已创建配置（权限 600）：{path}")


def load_config(path):
    path = Path(path).expanduser().resolve()
    with path.open() as stream:
        cfg = yaml.safe_load(stream)
    defaults = yaml.safe_load(DEFAULT_CONFIG.replace("__TOKEN__", ""))
    if not isinstance(cfg, dict):
        raise ValueError("YAML 顶层必须是映射")
    for section, values in defaults.items():
        if not isinstance(cfg.get(section, {}), dict):
            raise ValueError(f"配置 {section} 必须是映射")
        defaults[section].update(cfg.get(section, {}))
    cfg = defaults
    token = cfg["auth"]["token"]
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token):
        raise ValueError("auth.token 应为至少 16 位字母/数字/下划线/短横线")
    t = cfg["transfer"]
    bounds = {
        "max_files": (1, 128), "connections_per_file": (1, 512),
        "min_split_mib": (1, 1024), "lowest_speed_kib": (0, 2**30),
        "connect_timeout_seconds": (1, 600),
        "network_timeout_seconds": (1, 600), "refresh_below_connections": (0, 512),
        "refresh_low_seconds": (1, 86400), "startup_grace_seconds": (0, 86400),
        "refresh_cooldown_seconds": (0, 86400), "refresh_min_remaining_mib": (0, 2**30),
        "max_refreshes": (0, 100000), "max_error_retries": (0, 100000),
        "retry_wait_seconds": (1, 600), "poll_seconds": (1, 60),
        "save_interval_seconds": (1, 600), "status_interval_seconds": (1, 600),
    }
    for key, (low, high) in bounds.items():
        if type(t[key]) is not int or not low <= t[key] <= high:
            raise ValueError(f"transfer.{key} 必须在 {low}..{high} 之间")
    if t["refresh_below_connections"] > t["connections_per_file"]:
        raise ValueError("刷新连接数阈值不能大于每文件连接数；小并发测试时请同时调整")
    for section, key, low in [("sender", "port", 1), ("receiver", "port", 1),
                              ("receiver", "rpc_port", 0)]:
        val = cfg[section][key]
        if type(val) is not int or not low <= val <= 65535:
            raise ValueError(f"{section}.{key} 端口无效")
    if cfg["receiver"]["scheme"] not in ("http", "https"):
        raise ValueError("receiver.scheme 只能为 http 或 https")
    for section, key in [("sender", "bind_ip"), ("sender", "advertised_ip"),
                         ("receiver", "source_ip")]:
        host = cfg[section][key]
        if not isinstance(host, str) or not host or re.search(r"[\s/@?#]", host):
            raise ValueError(f"{section}.{key} 应只填 IP 或主机名，不带协议/路径")
    if not isinstance(cfg["sender"]["paths"], list):
        raise ValueError("sender.paths 必须是路径列表")
    cfg["_base"] = path.parent
    cfg["_path"] = path
    return cfg


def configured_path(cfg, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else cfg["_base"] / path


def origin(host, port, scheme="http"):
    host = host.strip("[]")
    return f"{scheme}://{'[' + host + ']' if ':' in host else host}:{port}"


def raise_file_limit(required):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    desired = min(max(soft, required), hard) if hard != resource.RLIM_INFINITY else max(soft, required)
    resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
    if desired < required:
        raise RuntimeError(f"文件描述符硬上限 {hard} 不足，需要至少 {required}")


def safe_relative(name):
    if not isinstance(name, str) or not name or "\\" in name or any(ord(c) < 32 for c in name):
        raise ValueError(f"无效的相对路径：{name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..", ".download-tools") for p in parts) or PurePosixPath(name).is_absolute():
        raise ValueError(f"不安全的相对路径：{name!r}")
    return name


def safe_target(root, name):
    safe_relative(name)
    target = root.joinpath(*name.split("/"))
    current = root
    for component in name.split("/"):
        current = current / component
        if current.is_symlink():
            raise ValueError(f"目标路径包含符号链接：{current}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"目标路径越界：{name}")
    return target


def build_manifest(paths):
    files, directories, lookup, used = [], set(), {}, set()
    for given in paths:
        root = Path(given).expanduser().resolve(strict=True)
        name = safe_relative(root.name)
        if name in used:
            raise ValueError(f"所选根路径重名：{name}；请分别分享或调整名称")
        used.add(name)
        candidates = []
        if root.is_file():
            candidates.append((root, name))
        elif root.is_dir():
            directories.add(name)
            for parent, subdirs, leaves in os.walk(root, followlinks=False):
                subdirs[:] = sorted(d for d in subdirs if d != ".download-tools" and not (Path(parent) / d).is_symlink())
                relative = Path(parent).relative_to(root)
                directory = (Path(name) / relative).as_posix()
                safe_relative(directory)
                directories.add(directory)
                for leaf in sorted(leaves):
                    path = Path(parent) / leaf
                    if path.is_symlink() or not path.is_file():
                        continue
                    candidates.append((path, (Path(directory) / leaf).as_posix()))
        else:
            raise ValueError(f"只支持普通文件/目录：{root}")
        for path, relative in candidates:
            safe_relative(relative)
            log(f"计算 SHA-256：{relative}")
            before = signature(path)
            digest = sha256(path)
            if signature(path) != before:
                raise RuntimeError(f"计算校验值时文件发生变化：{path}")
            file_id = hashlib.sha256((relative + "\0" + digest).encode()).hexdigest()
            entry = {"id": file_id, "path": relative, "size": before[2], "sha256": digest}
            files.append(entry)
            lookup[file_id] = (path, before)
    return {"version": 1, "files": files, "directories": sorted(directories)}, lookup


def make_app(manifest, lookup, token):
    from aiohttp import web

    @web.middleware
    async def authenticate(request, handler):
        header = request.headers.get("Authorization", "")
        if not hmac.compare_digest(header.encode(), ("Bearer " + token).encode()):
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def listing(request):
        return web.json_response(manifest, headers={"Cache-Control": "no-store"})

    async def download(request):
        # FileResponse can otherwise negotiate an unselected .gz/.br sibling.
        if request.headers.get("Accept-Encoding", "identity").strip().lower() != "identity":
            raise web.HTTPNotAcceptable(text="Use Accept-Encoding: identity")
        item = lookup.get(request.match_info["file_id"])
        if item is None:
            raise web.HTTPNotFound()
        path, expected = item
        try:
            unchanged = signature(path) == expected
        except OSError:
            unchanged = False
        if not unchanged:
            raise web.HTTPConflict(text="Shared file changed; restart sender and refresh manifest.")
        return web.FileResponse(path, headers={"Cache-Control": "no-transform", "Content-Type": "application/octet-stream"})

    app = web.Application(middlewares=[authenticate])
    app.router.add_get("/manifest", listing)
    app.router.add_get("/files/{file_id}", download)
    return app


def serve(cfg, paths):
    from aiohttp import web
    if not paths:
        paths = [str(configured_path(cfg, p)) for p in cfg["sender"]["paths"]]
    if not paths:
        if not sys.stdin.isatty():
            raise ValueError("请传入文件/文件夹路径，或配置 sender.paths")
        paths = shlex.split(input("输入要分享的文件/文件夹路径（空格分隔，含空格路径加引号）：\n> "))
    if not paths:
        raise ValueError("未选择文件")
    raise_file_limit(max(4096, cfg["transfer"]["max_files"] * cfg["transfer"]["connections_per_file"] * 3 + 256))
    manifest, lookup = build_manifest(paths)
    sender = cfg["sender"]
    tls = None
    if bool(sender["tls_cert"]) != bool(sender["tls_key"]):
        raise ValueError("tls_cert 和 tls_key 必须同时设置")
    if sender["tls_cert"]:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(configured_path(cfg, sender["tls_cert"]), configured_path(cfg, sender["tls_key"]))
    address = origin(sender["advertised_ip"], sender["port"], "https" if tls else "http")
    log(f"分享 {len(manifest['files'])} 个文件，接收端地址：{address}（需要相同 auth.token）")
    if sender["advertised_ip"] == "CHANGE_ME":
        log("请在 config.yaml 填写 sender.advertised_ip，并在接收端填写 receiver.source_ip")
    web.run_app(make_app(manifest, lookup, cfg["auth"]["token"]),
                host=sender["bind_ip"], port=sender["port"], ssl_context=tls,
                access_log=None, print=log, shutdown_timeout=5)


def http_json(url, *, token=None, payload=None, ca_file=None, timeout=15):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    # Direct connection: do not collapse traffic through an environment proxy.
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("不允许重定向；请直接填写发送端地址")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect())
    request = urllib.request.Request(url, data=data, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        if response.url != url:
            raise ValueError("不允许重定向；请直接填写发送端地址")
        body = response.read(64 * MIB + 1)
    if len(body) > 64 * MIB:
        raise ValueError("清单/RPC 响应过大")
    return json.loads(body)


def validate_manifest(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("不支持的发送端清单格式")
    files, directories = data.get("files"), data.get("directories")
    if not isinstance(files, list) or not isinstance(directories, list):
        raise ValueError("清单缺少文件/目录列表")
    seen, ids = set(), set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("无效文件条目")
        name = safe_relative(entry.get("path"))
        for key in ("id", "sha256"):
            if not isinstance(entry.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", entry[key]):
                raise ValueError(f"无效 {key}")
        if type(entry.get("size")) is not int or entry["size"] < 0:
            raise ValueError("无效文件大小")
        if name in seen or entry["id"] in ids:
            raise ValueError("清单包含重复文件")
        seen.add(name)
        ids.add(entry["id"])
    for name in directories:
        safe_relative(name)
        if name in seen:
            raise ValueError("清单包含文件/目录冲突")
    for name in seen | set(directories):
        if any(str(parent) in seen for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise ValueError("清单包含父路径文件冲突")
    return data


def select_entries(manifest, selections, all_files, list_only=False):
    choices = sorted(set(manifest["directories"]) | {f["path"] for f in manifest["files"]})
    if list_only or (not all_files and not selections):
        directories = set(manifest["directories"])
        for index, name in enumerate(choices, 1):
            print(f"{index:4d}. {name}{'/' if name in directories else ''}")
        if list_only:
            return [], []
        if not sys.stdin.isatty():
            raise ValueError("非交互运行请明确指定 --all 或 --select PATH")
        answer = input("选择编号（空格分隔），或输入 all： ").strip()
        if answer == "all":
            all_files = True
        else:
            try:
                indexes = [int(i) for i in answer.split()]
                if not indexes or any(i < 1 or i > len(choices) for i in indexes):
                    raise ValueError()
                selections = [choices[i - 1] for i in indexes]
            except ValueError:
                raise ValueError("无效的选择编号") from None
    selections = [s.rstrip("/") for s in selections]
    if not all_files and any(s not in choices for s in selections):
        raise ValueError("--select 必须使用清单中的完整相对文件/目录名")
    def wanted(name):
        return all_files or any(name == s or name.startswith(s + "/") for s in selections)
    return [f for f in manifest["files"] if wanted(f["path"])], [d for d in manifest["directories"] if wanted(d)]


class Aria2:
    def __init__(self, cfg):
        self.cfg, self.process, self.temp, self.logfile = cfg, None, None, None
        self.secret = secrets.token_urlsafe(32)

    def rpc(self, method, *args):
        result = http_json(self.url, payload={"jsonrpc": "2.0", "id": "download-tools",
                          "method": "aria2." + method, "params": ["token:" + self.secret, *args]})
        if "error" in result:
            raise RuntimeError(f"aria2 {method}: {result['error'].get('message', 'RPC error')}")
        return result["result"]

    def __enter__(self):
        binary = ROOT / "bin/aria2c"
        metadata = ROOT / "bin/build-info.json"
        if not binary.is_file() or not metadata.is_file():
            raise RuntimeError("缺少特殊编译 aria2，请先执行 ./build_aria2.sh")
        info = json.loads(metadata.read_text())
        if info.get("low_speed_policy") != "upstream-native" or info.get("max_connections") != 512 or info.get("binary_sha256") != sha256(binary):
            raise RuntimeError("aria2 与编译记录不匹配，请重新执行 ./build_aria2.sh")
        t = self.cfg["transfer"]
        raise_file_limit(max(4096, t["max_files"] * t["connections_per_file"] * 2 + 256))
        port = self.cfg["receiver"]["rpc_port"]
        if port == 0:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/jsonrpc"
        self.temp = tempfile.TemporaryDirectory(prefix="download-tools-rpc-")
        conf = Path(self.temp.name) / "aria2.conf"
        conf.write_text("\n".join([
            "enable-rpc=true", "rpc-listen-all=false", f"rpc-listen-port={port}",
            "rpc-allow-origin-all=false", f"rpc-secret={self.secret}",
            f"max-concurrent-downloads={t['max_files']}", "enable-color=false",
            "console-log-level=warn", "summary-interval=0", "download-result=hide",
            "disk-cache=64M", "check-certificate=true", "max-download-result=10000",
        ]) + "\n")
        conf.chmod(0o600)
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        self.log_path = logs / f"aria2-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
        self.logfile = self.log_path.open("a")
        environment = os.environ.copy()
        # Each range must use the real remote connection, never an inherited proxy.
        for key in list(environment):
            if key.lower().endswith("_proxy"):
                del environment[key]
        try:
            self.process = subprocess.Popen([str(binary), f"--conf-path={conf}"],
                                            stdout=self.logfile, stderr=subprocess.STDOUT,
                                            env=environment, start_new_session=True)
            for _ in range(100):
                if self.process.poll() is not None:
                    raise RuntimeError(f"aria2 启动失败；查看 {self.log_path}")
                try:
                    self.rpc("getVersion")
                    # Recheck process: do not attach to an unrelated daemon on a raced port.
                    if self.process.poll() is not None:
                        raise RuntimeError("aria2 已退出")
                    return self
                except (OSError, ValueError, RuntimeError):
                    time.sleep(0.1)
            raise RuntimeError("aria2 RPC 启动超时")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        if self.process is not None and self.process.poll() is None:
            # SIGTERM asks aria2 to save control files before exiting.
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.logfile:
            self.logfile.close()
        if self.temp:
            self.temp.cleanup()

    def options(self, entry, state_dir):
        t = self.cfg["transfer"]
        options = {
            "dir": str(state_dir), "out": entry["id"] + ".part",
            "split": str(t["connections_per_file"]),
            "max-connection-per-server": str(t["connections_per_file"]),
            "min-split-size": f"{t['min_split_mib']}M", "piece-length": "1M",
            "continue": "true", "always-resume": "true", "auto-file-renaming": "false",
            "allow-overwrite": "true", "file-allocation": "none",
            "checksum": "sha-256=" + entry["sha256"], "check-integrity": "true",
            "lowest-speed-limit": f"{t['lowest_speed_kib']}K",
            "connect-timeout": str(t["connect_timeout_seconds"]),
            "timeout": str(t["network_timeout_seconds"]),
            "auto-save-interval": str(t["save_interval_seconds"]),
            "max-tries": "3", "retry-wait": str(t["retry_wait_seconds"]),
            "header": ["Authorization: Bearer " + self.cfg["auth"]["token"], "Accept-Encoding: identity"],
            "http-accept-gzip": "false",
            "enable-http-pipelining": "false",
        }
        if self.cfg["receiver"]["ca_file"]:
            options["ca-certificate"] = str(configured_path(self.cfg, self.cfg["receiver"]["ca_file"]))
        return options

    def pause_and_resume(self, gid):
        self.rpc("pause", gid)
        for _ in range(100):
            status = self.rpc("tellStatus", gid, ["status"])["status"]
            if status == "paused":
                self.rpc("unpause", gid)
                return
            if status in ("complete", "error", "removed"):
                return
            if STOP.wait(0.1):
                return
        raise RuntimeError("aria2 暂停超时；保留断点，停止该批任务")


class RefreshPolicy:
    def __init__(self, settings, now):
        self.settings = settings
        self.started = now
        self.last_refresh = -math.inf
        self.low_since = None

    def should_refresh(self, status, now):
        t = self.settings
        size, done = int(status.get("totalLength", 0)), int(status.get("completedLength", 0))
        # Piece availability, checksum verification and the natural download tail
        # must not be mistaken for lost connections.
        threshold = t["refresh_below_connections"]
        tail = max(t["refresh_min_remaining_mib"] * MIB, threshold * 2 * t["min_split_mib"] * MIB)
        eligible = (threshold > 0 and status["status"] == "active" and size > 0
                    and size - done >= tail and done / size < 0.98
                    and status.get("verifyIntegrityPending") != "true"
                    and "verifiedLength" not in status
                    and now - self.started >= t["startup_grace_seconds"]
                    and now - self.last_refresh >= t["refresh_cooldown_seconds"])
        if not eligible or int(status.get("connections", 0)) >= threshold:
            self.low_since = None
            return False
        if self.low_since is None:
            self.low_since = now
        return now - self.low_since >= t["refresh_low_seconds"]

    def refreshed(self, now):
        self.started = self.last_refresh = now
        self.low_since = None


def download_file(aria, base_url, entry, destination, state_dir):
    target = safe_target(destination, entry["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.is_file() and target.stat().st_size == entry["size"] and sha256(target) == entry["sha256"]:
            log(f"已校验，跳过：{entry['path']}")
            return
        raise RuntimeError(f"目标已有不同内容，拒绝覆盖：{target}")
    options = aria.options(entry, state_dir)
    url = base_url + "/files/" + entry["id"]
    gid = aria.rpc("addUri", [url], options)
    log(f"开始：{entry['path']} ({entry['size'] / MIB:.1f} MiB)")
    t = aria.cfg["transfer"]
    policy = RefreshPolicy(t, time.monotonic())
    refreshes = retries = 0
    last_log = 0
    while not STOP.is_set():
        status = aria.rpc("tellStatus", gid)
        now = time.monotonic()
        if status["status"] == "complete":
            partial = state_dir / options["out"]
            # aria2 has already checked the manifest's SHA-256.
            if partial.stat().st_size != entry["size"]:
                raise RuntimeError("完成文件大小与清单不符")
            safe_target(destination, entry["path"])
            # Atomic publication without overwriting a file created meanwhile.
            os.link(partial, target)
            partial.unlink()
            aria.rpc("removeDownloadResult", gid)
            log(f"完成并校验：{entry['path']}")
            return
        if status["status"] in ("error", "removed"):
            retries += 1
            if retries > t["max_error_retries"]:
                raise RuntimeError(f"{entry['path']} 重试耗尽，aria2 错误 {status.get('errorCode')}: {status.get('errorMessage', '')}")
            log(f"续传重试 {retries}/{t['max_error_retries']}：{entry['path']}，错误码 {status.get('errorCode')}")
            if STOP.wait(t["retry_wait_seconds"]):
                break
            aria.rpc("removeDownloadResult", gid)
            gid = aria.rpc("addUri", [url], options)
            policy.refreshed(time.monotonic())
            continue
        if policy.should_refresh(status, now):
            if refreshes >= t["max_refreshes"]:
                raise RuntimeError(f"{entry['path']} 刷新次数耗尽；请降低并发/阈值后重试，断点已保留")
            refreshes += 1
            log(f"连接 {status.get('connections', 0)} < {t['refresh_below_connections']}，续传刷新 {refreshes}：{entry['path']}")
            aria.pause_and_resume(gid)
            policy.refreshed(time.monotonic())
        if now - last_log >= t["status_interval_seconds"]:
            size = int(status.get("totalLength", 0))
            done = int(status.get("completedLength", 0))
            log(f"{entry['path']}: {100 * done / size if size else 0:.1f}% | {int(status.get('downloadSpeed', 0))/MIB:.2f} MiB/s | {status.get('connections', 0)} 连接")
            last_log = now
        STOP.wait(t["poll_seconds"])
    raise InterruptedError("已停止，保留断点")


def receive(cfg, args):
    receiver = cfg["receiver"]
    if receiver["source_ip"] == "CHANGE_ME":
        raise ValueError("请先填写 config.yaml 中 receiver.source_ip")
    base_url = origin(receiver["source_ip"], receiver["port"], receiver["scheme"])
    ca = configured_path(cfg, receiver["ca_file"]) if receiver["ca_file"] else None
    manifest = validate_manifest(http_json(base_url + "/manifest", token=cfg["auth"]["token"], ca_file=ca))
    files, directories = select_entries(manifest, args.select, args.all, args.list)
    if args.list:
        return
    destination = Path(args.dest).expanduser().absolute() if args.dest else configured_path(cfg, receiver["output_dir"]).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    state_dir = destination / ".download-tools"
    if state_dir.is_symlink():
        raise ValueError("状态目录不能是符号链接")
    state_dir.mkdir(mode=0o700, exist_ok=True)
    with (state_dir / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("该输出目录已有下载器运行；请等待或选择另一目录") from None
        for directory in directories:
            safe_target(destination, directory).mkdir(parents=True, exist_ok=True)
        if not files:
            log("已创建所选空目录；没有文件需要下载")
            return
        with Aria2(cfg) as aria:
            with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["transfer"]["max_files"]) as pool:
                pending = {pool.submit(download_file, aria, base_url, entry, destination, state_dir) for entry in files}
                errors = []
                for future in concurrent.futures.as_completed(pending):
                    try:
                        future.result()
                    except BaseException as error:
                        errors.append(error)
                        STOP.set()
                        for other in pending:
                            other.cancel()
                if errors:
                    raise RuntimeError(str(next((e for e in errors if not isinstance(e, (InterruptedError, concurrent.futures.CancelledError))), errors[0])))
        log(f"全部完成：{destination}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("init", help="生成本地配置，不覆盖已有配置")
    upload = subs.add_parser("upload", help="分享所选文件/文件夹，由另一台机器拉取")
    upload.add_argument("paths", nargs="*")
    download = subs.add_parser("download", help="选择远端文件/文件夹，断点续传下载")
    download.add_argument("--select", action="append", default=[], metavar="PATH")
    download.add_argument("--all", action="store_true")
    download.add_argument("--list", action="store_true")
    download.add_argument("--dest", help="本地输出目录，覆盖 YAML 默认值")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "init":
            init_config(args.config)
            return 0
        cfg = load_config(args.config)
        if args.command == "upload":
            serve(cfg, args.paths)
        else:
            def stop(signum, frame):
                STOP.set()
                log("正在停止并保存 aria2 断点，请稍候…")
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            receive(cfg, args)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        log(f"错误：{error}")
        return 130 if STOP.is_set() and str(error) == "已停止，保留断点" else 1


if __name__ == "__main__":
    sys.exit(main())
