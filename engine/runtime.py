"""Standalone lifecycle host. Business routes are connected in I50-04/05."""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import errno
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from engine.application_schema import CONTRACT_VERSION, CONTRACT_VERSIONS
from engine.runtime_schema import openapi_document
from engine.runtime_config import resolve, ConfigError
from engine.runtime_storage import RuntimeStorage, RequestLedger, ArtifactStore, SafeLog, StorageError, build_metadata, private_directory

APP_VERSION = "0.5.2"


class RuntimeErrorCode(Exception):
    def __init__(self, code, exit_code=5):
        self.code, self.exit_code = code, exit_code
        super().__init__(code)


class RuntimeHTTPServer(ThreadingHTTPServer):
    """Keep the loopback listener exclusive on Winsock as well as POSIX."""
    allow_reuse_port = False

    def server_bind(self):
        if sys.platform == "win32":
            # HTTPServer inherits SO_REUSEADDR, which permits a second bind on
            # Windows. Set exclusivity before bind, without address reuse.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            super().server_bind()
        except OSError as error:
            if (error.errno == errno.EADDRINUSE or
                    (sys.platform == "win32" and getattr(error, "winerror", None) in (10013, 10048))):
                raise RuntimeErrorCode("PORT_IN_USE", 3) from error
            raise


class WriterLock:
    """A persistent lock inode; never unlink it while another process can open it."""
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.file = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                # Windows permits byte-range locks beyond EOF. Do not write
                # into another writer's locked byte while probing ownership.
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            self.file = None
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeErrorCode("DATA_DIR_IN_USE", 3) from error
            raise
        return self

    def close(self):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


def atomic_private_json(path, value):
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Runtime:
    def __init__(self, data_dir, port=8765, shutdown_timeout_seconds=30, *, config=None, checkpoint_root=None):
        if type(port) is not int or not 0 <= port <= 65535:
            raise RuntimeErrorCode("CONFIG_INVALID", 2)
        if type(shutdown_timeout_seconds) is not int or not 1 <= shutdown_timeout_seconds <= 300:
            raise RuntimeErrorCode("CONFIG_INVALID", 2)
        try:
            self.config = config or resolve({"data_dir": str(data_dir), "port": port, "shutdown_timeout_seconds": shutdown_timeout_seconds}, environ={})
        except (ValueError, OSError):
            raise RuntimeErrorCode("CONFIG_INVALID", 2)
        self.checkpoint_root = Path(checkpoint_root).absolute() if checkpoint_root is not None else None
        if self.checkpoint_root is not None and any(path.is_symlink() for path in (self.checkpoint_root, *self.checkpoint_root.parents)):
            raise RuntimeErrorCode("CONFIG_INVALID", 2)
        self.log = None
        self.storage = None
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.stop_started = None
        self.exit_code = 0
        self.active_handlers = 0
        self.data_dir = Path(data_dir).resolve()
        assets = self.config["assets_dir"]
        if self.data_dir == assets or assets in self.data_dir.parents:
            raise RuntimeErrorCode("CONFIG_INVALID", 2)
        self.run_dir = self.data_dir / "run"
        self.port = port
        self.instance_id = secrets.token_hex(16)
        self.token = secrets.token_urlsafe(32)
        self.state = "STARTING"
        self.server = None
        self.writer = None
        self.published = False
        self.guard = threading.Lock()
        self.stopping = threading.Event()
        self.signal_stop = False

    def health(self):
        with self.guard:
            return {"state": self.state, "ready": self.state == "READY",
                    "instance_id": self.instance_id, "app_version": APP_VERSION,
                    "contract_versions": list(CONTRACT_VERSIONS), "recovery_required": bool(self.storage and self.storage.recovery_required),
                    "reason_code": None}

    def start(self):
        try:
            metadata = build_metadata(self.config["assets_dir"])
            private_directory(self.data_dir)
            if self.run_dir.is_symlink():
                raise RuntimeErrorCode("STORAGE_UNAVAILABLE")
            private_directory(self.run_dir)
            if (self.run_dir / "writer.lock").is_symlink():
                raise RuntimeErrorCode("STORAGE_UNAVAILABLE")
            self.writer = WriterLock(self.run_dir / "writer.lock").acquire()
            self.log = SafeLog(self.config["log_dir"], self.instance_id, self.config["log_level"], self.log_failed)
            self.storage = RuntimeStorage(self.data_dir, metadata, assets_root=self.config["assets_dir"], checkpoint_root=self.checkpoint_root)
            self.ledger = RequestLedger(self.data_dir / "requests", store=self.storage.store)
            self.artifacts = ArtifactStore(self.data_dir / "artifacts")
            self.storage.recovery_required |= any(r["state"] == "PREPARED" for r in self.ledger.records)
            from engine.runtime_application import RuntimeApplication
            from engine.runtime_http import RuntimeHandler
            from engine.application import Application
            from engine.navigator import registered_navigator_profiles
            from engine.session_credentials import SessionCredentials
            self.credentials = SessionCredentials()
            from engine.byok_provider import ProviderAdapter
            self.services = RuntimeApplication(self, send_port=ProviderAdapter(self.credentials))
            self.application = Application(concepts_dir=self.config["assets_dir"] / "concepts", services=self.services)
            self.services.manager.application = self.application
            self.services.manager.codex.application = self.application
            runtime = self

            class Handler(RuntimeHandler):
                def log_message(self, *_args):
                    pass  # Never log headers, tokens, request bodies, or arbitrary paths.

                def send_json(self, status, body):
                    payload = json.dumps(body).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(payload)

                def trusted_host(self):
                    return self.headers.get_all("Host") == [f"127.0.0.1:{runtime.port}"]

                def do_GET(self):
                    if not self.trusted_host():
                        return self.send_json(403, {"code": "HOST_REJECTED"})
                    if self.path == "/api/v1/openapi.json":
                        return self.send_json(200, openapi_document())
                    if self.path == "/api/v1/health/ready":
                        health = runtime.health()
                        return self.send_json(200 if health["ready"] else 503, health)
                    if runtime.state != "READY":
                        return self.send_json(503, {"code": "CAPABILITY_UNAVAILABLE"})
                    return super().do_GET()

                def do_POST(self):
                    self.close_connection = True
                    origin = self.headers.get("Origin")
                    if not self.trusted_host() or (origin is not None and origin != runtime.url.rstrip("/")):
                        return self.send_json(403, {"code": "ORIGIN_REJECTED"})
                    if self.path != "/api/v1/control/stop":
                        if runtime.state != "READY":
                            return self.send_json(503, {"code": "CAPABILITY_UNAVAILABLE"})
                        return super().do_POST()
                    authorization = self.headers.get_all("Authorization")
                    if not authorization or len(authorization) != 1 or not hmac.compare_digest(
                            authorization[0], "Bearer " + runtime.token):
                        return self.send_json(403, {"code": "CONTROL_UNAUTHORIZED"})
                    if (self.headers.get_all("Content-Length") != ["2"] or
                            self.headers.get("Transfer-Encoding") is not None or
                            self.headers.get("Content-Encoding") is not None or
                            self.headers.get("Content-Type") != "application/json"):
                        return self.send_json(400, {"code": "INPUT_INVALID"})
                    self.connection.settimeout(2)
                    try:
                        if self.rfile.read(2) != b"{}":
                            return self.send_json(400, {"code": "INPUT_INVALID"})
                    except (OSError, TimeoutError):
                        return
                    runtime.request_stop()
                    return self.send_json(202, {"state": "STOPPING", "instance_id": runtime.instance_id})

            class Server(RuntimeHTTPServer):
                def process_request(self, request, client_address):
                    with runtime.guard:
                        runtime.active_handlers += 1
                    try:
                        super().process_request(request, client_address)
                    except Exception:
                        with runtime.guard:
                            runtime.active_handlers -= 1
                        raise

                def process_request_thread(self, request, client_address):
                    try:
                        super().process_request_thread(request, client_address)
                    finally:
                        with runtime.guard:
                            runtime.active_handlers -= 1

                def handle_error(self, request, client_address):
                    pass  # Socket failures must not print request diagnostics.

                def get_request(self):
                    connection, address = super().get_request()
                    connection.settimeout(2)
                    return connection, address

            self.server = Server(("127.0.0.1", self.port), Handler)
            self.server.application = self.application
            self.server.services = self.services
            self.server.manager = self.services.manager
            self.server.rule_store = self.services.rules
            self.server.profiles = registered_navigator_profiles()
            self.server.state_lock = self.services.lock
            self.server.busy = False
            self.server.published_state = None
            self.server.replay_error = None
            self.server.daemon_threads = False
            self.port = self.server.server_address[1]
            self.url = f"http://127.0.0.1:{self.port}/"
            atomic_private_json(self.run_dir / "instance.json", {
                "instance_id": self.instance_id, "port": self.port,
                "pid": os.getpid(), "control_token": self.token})
            self.published = True
            with self.guard:
                self.state = "READY"
            self.log.event("RUNTIME_READY")
            if self.log.failed:
                raise RuntimeErrorCode("STORAGE_UNAVAILABLE")
            return self
        except Exception as error:
            self.state = "FAILED"
            self.close()
            if isinstance(error, StorageError):
                raise RuntimeErrorCode(error.code, 4 if error.code == "BUILD_METADATA_UNAVAILABLE" else 5) from error
            if isinstance(error, RuntimeErrorCode):
                raise
            if isinstance(error, OSError) and error.errno == errno.EADDRINUSE:
                raise RuntimeErrorCode("PORT_IN_USE", 3) from error
            raise RuntimeErrorCode("STORAGE_UNAVAILABLE" if isinstance(error, OSError) else "STARTUP_FAILED") from error

    def log_failed(self):
        print('{"code":"LOG_UNAVAILABLE"}', file=sys.stderr)
        self.exit_code = 5
        self.request_stop()

    def handle_signal(self, *_args):
        # A signal can interrupt this thread while it owns guard. Never lock here.
        self.signal_stop = True

    def request_stop(self):
        with self.guard:
            self.state = "STOPPING"
            if self.stop_started is None:
                self.stop_started = time.monotonic()
            self.stopping.set()
            if hasattr(self, 'services'):
                self.services.byok.close()
            if hasattr(self, "credentials"):
                self.credentials.close()

    def serve(self):
        # handle_request uses a bounded timeout, so a signal never calls shutdown
        # from the serving thread. Non-daemon handlers finish before lock release.
        self.server.timeout = 0.1
        try:
            while True:
                self.credentials.expire()
                self.services.byok.tick()
                if self.signal_stop:
                    self.request_stop()
                with self.guard:
                    drained = self.stopping.is_set() and self.active_handlers == 0
                    if self.stop_started is not None and time.monotonic() - self.stop_started > self.shutdown_timeout_seconds:
                        self.exit_code = 6
                if drained:
                    break
                self.server.handle_request()
        finally:
            self.close()

    def close(self):
        if hasattr(self, 'services'):
            self.services.byok.close()
        if hasattr(self, "credentials"):
            self.credentials.close()
        if self.server:
            self.server.server_close()
            if self.stop_started is not None and time.monotonic() - self.stop_started > self.shutdown_timeout_seconds:
                self.exit_code = 6
            self.server = None
        if self.log:
            self.log.event("RUNTIME_FAILED" if self.state == "FAILED" else "RUNTIME_STOPPED")
            self.log.close()
            self.log = None
        if self.writer:
            path = self.run_dir / "instance.json"
            try:
                if self.published and json.loads(path.read_text())["instance_id"] == self.instance_id:
                    path.unlink()
            except (FileNotFoundError, ValueError, KeyError, TypeError):
                pass
            finally:
                self.writer.close()
                self.writer = None
        if self.state != "FAILED":
            self.state = "STOPPED"


def discover(data_dir):
    run = Path(data_dir).resolve() / "run"
    if not (run / "writer.lock").is_file():
        raise RuntimeErrorCode("RUNTIME_NOT_RUNNING", 3)
    probe = WriterLock(run / "writer.lock")
    try:
        probe.acquire()
    except RuntimeErrorCode as error:
        if error.code != "DATA_DIR_IN_USE":
            raise
    else:
        probe.close()
        raise RuntimeErrorCode("RUNTIME_NOT_RUNNING", 3)
    try:
        info = json.loads((run / "instance.json").read_text())
        if (type(info["port"]) is not int or not 1 <= info["port"] <= 65535 or
                not isinstance(info["instance_id"], str) or not isinstance(info["control_token"], str)):
            raise ValueError()
        return info
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeErrorCode("INSTANCE_MISMATCH", 3) from error


def control_request(info, stop=False):
    request = Request(f'http://127.0.0.1:{info["port"]}/api/v1/' +
                      ("control/stop" if stop else "health/ready"),
                      data=b"{}" if stop else None,
                      headers={"Content-Type": "application/json", **(
                          {"Authorization": "Bearer " + info["control_token"]} if stop else {})})
    try:
        opener = build_opener(ProxyHandler({}))
        try:
            response = opener.open(request, timeout=3)
        except HTTPError as error:
            if error.code != 503 or stop:
                raise
            response = error
        with response:
            body = json.loads(response.read(8192))
        if not isinstance(body, dict) or body.get("instance_id") != info["instance_id"]:
            raise RuntimeErrorCode("INSTANCE_MISMATCH", 3)
        return body
    except (OSError, URLError, ValueError) as error:
        raise RuntimeErrorCode("RUNTIME_NOT_RUNNING", 3) from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "status", "stop"))
    parser.add_argument("--config")
    for key in ("data-dir", "log-dir", "assets-dir", "log-level"):
        parser.add_argument("--" + key)
    parser.add_argument("--port", type=int)
    parser.add_argument("--shutdown-timeout-seconds", type=int)
    flags = parser.add_mutually_exclusive_group()
    flags.add_argument("--allow-evaluation-records", dest="allow_evaluation_records", action="store_true", default=None)
    flags.add_argument("--no-allow-evaluation-records", dest="allow_evaluation_records", action="store_false")
    args = parser.parse_args(argv)
    try:
        try:
            config = resolve(vars(args))
        except (OSError, ValueError, TypeError):
            raise RuntimeErrorCode("CONFIG_INVALID", 2)
        args.data_dir = config["data_dir"]
        args.port = config["port"]
        args.shutdown_timeout_seconds = config["shutdown_timeout_seconds"]
        if args.command == "serve":
            runtime = Runtime(args.data_dir, args.port, args.shutdown_timeout_seconds, config=config).start()
            previous = {}
            for number in (signal.SIGINT, signal.SIGTERM):
                previous[number] = signal.signal(number, runtime.handle_signal)
            try:
                print(json.dumps({"state": "READY", "instance_id": runtime.instance_id,
                                  "url": runtime.url, "app_version": APP_VERSION}), flush=True)
                runtime.serve()
            finally:
                runtime.close()
                for number, handler in previous.items():
                    signal.signal(number, handler)
            return runtime.exit_code
        info = discover(args.data_dir)
        health = control_request(info)
        if args.command == "status":
            print(json.dumps(health))
            return 0 if health["ready"] else 5
        control_request(info, stop=True)
        deadline = time.monotonic() + args.shutdown_timeout_seconds
        while time.monotonic() < deadline:
            try:
                current = discover(args.data_dir)
            except RuntimeErrorCode as error:
                if error.code == "RUNTIME_NOT_RUNNING":
                    return 0
                if error.code == "INSTANCE_MISMATCH" and not (args.data_dir / "run/instance.json").exists():
                    time.sleep(0.05)
                    continue
                raise
            if current["instance_id"] != info["instance_id"]:
                raise RuntimeErrorCode("INSTANCE_MISMATCH", 3)
            time.sleep(0.05)
        raise RuntimeErrorCode("SHUTDOWN_INCOMPLETE", 6)
    except (ConfigError, ValueError, TypeError) as error:
        print(json.dumps({"code": "CONFIG_INVALID"}), file=sys.stderr)
        return 2
    except RuntimeErrorCode as error:
        print(json.dumps({"code": error.code}), file=sys.stderr)
        return error.exit_code
    except OSError:
        print(json.dumps({"code": "STORAGE_UNAVAILABLE"}), file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
