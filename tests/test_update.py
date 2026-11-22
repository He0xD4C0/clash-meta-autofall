"""离线验证刷新脚本在纯净度阶段之后继续完成评分和重载。"""

import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml


UPDATE = Path(__file__).resolve().parents[1] / "shell" / "update.sh"


class Controller(BaseHTTPRequestHandler):
    def do_PUT(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.reloads.append(self.path)
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_):
        pass


class UpdatePurityTests(unittest.TestCase):
    def test_purity_probe_failure_on_first_run_does_not_stop_update(self):
        self.run_update(probe_fails=True)

    def test_successful_probe_is_archived(self):
        self.run_update(probe_fails=False)

    def run_update(self, probe_fails):
        with tempfile.TemporaryDirectory() as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Controller) as server:
            root = Path(tmp)
            for name in ("secrets", "assets", "shell", "tools", "bin"):
                (root / name).mkdir()
            source = root / "source.yaml"
            source.write_text("proxies: [{name: fixture}]\n")
            (root / "secrets/subscriptions.yaml").write_text(yaml.safe_dump({
                "subscriptions": [{"name": "offline", "url": source.as_uri()}],
            }))
            for name in ("geoip.metadb", "geosite.dat"):
                (root / "assets" / name).write_text("stub")
            (root / "shell/merge.py").write_text(
                "from pathlib import Path\nimport sys\n"
                "def _load_source(*args): pass\n"
                "if __name__ == '__main__': Path(sys.argv[1]).write_text('{}')\n"
            )
            shutil.copyfile(UPDATE.with_name("subscriptions.py"), root / "shell/subscriptions.py")
            (root / "tools/rules.py").touch()
            write_snapshot = (
                "from pathlib import Path\nimport sys\n"
                "Path(sys.argv[sys.argv.index('--json') + 1]).write_text('{}')\n"
            )
            (root / "shell/score.py").write_text(write_snapshot)
            (root / "shell/make_probe_config.py").write_text(
                "from pathlib import Path\nimport sys\nPath(sys.argv[2]).write_text('{}')\n"
            )
            shutil.copyfile(UPDATE.parents[1] / "tools/reload.py", root / "tools/reload.py")
            (root / "tools/purity.py").write_text(
                "raise SystemExit(1)\n" if probe_fails else write_snapshot
            )
            kernel = root / "bin/mihomo"
            kernel.write_text("#!/bin/sh\nexit 0\n")
            kernel.chmod(0o700)
            env = {
                "PATH": str(kernel.parent) + os.pathsep + os.environ["PATH"],
                "DATA_DIR": str(root), "SHELL_DIR": str(root / "shell"),
                "TOOLS_DIR": str(root / "tools"), "PY": sys.executable,
                "MIHOMO_SECRET": "test", "MIHOMO_API": f"http://127.0.0.1:{server.server_port}",
                "PROBE_API": f"http://127.0.0.1:{server.server_port}/probe", "PURITY_ENABLED": "1",
            }
            server.reloads = []
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                result = subprocess.run(
                    ["bash", str(UPDATE)], env=env, capture_output=True, text=True, timeout=15,
                )
            finally:
                server.shutdown()
                worker.join()
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(server.reloads, ["/configs?force=true", "/probe/configs?force=true", "/configs?force=true"])
            self.assertTrue((root / "artifacts/scores.json").exists())
            self.assertEqual(len(list((root / "archive").glob("scores-*.json.gz"))), 1)
            archives = list((root / "archive").glob("purity-*.json.gz"))
            if probe_fails:
                self.assertIn("purity probe unavailable", result.stdout)
                self.assertEqual(archives, [])
            else:
                self.assertEqual(len(archives), 1)
                self.assertEqual(gzip.decompress(archives[0].read_bytes()), b"{}")
