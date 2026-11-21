"""纯净度与巡检的纯逻辑测试：不联网、不起内核。"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


purity = load("purity_mod", os.path.join(ROOT, "tools", "purity.py"))


class RegionTest(unittest.TestCase):
    def test_flags_and_names(self):
        self.assertEqual(purity.region_of("🇯🇵日本高速01|BGP|CUCM"), "jp")
        self.assertEqual(purity.region_of("Singapore-02"), "sg")
        self.assertEqual(purity.region_of("🇺🇸洛杉矶 LOS ANGELES"), "us")
        self.assertEqual(purity.region_of("🇭🇰香港IPLC"), "hk")
        self.assertEqual(purity.region_of("未知"), "unknown")


class PurityScoreTest(unittest.TestCase):
    def info(self, **kw):
        base = {"country_code": "JP", "hosting": False, "proxy": False, "mobile": False}
        base.update(kw)
        return base

    def test_residential(self):
        score, reason = purity.score_of(self.info(), "jp")
        self.assertEqual(score, 100.0)
        self.assertEqual(reason, "residential")

    def test_mobile_counts_as_residential(self):
        score, _ = purity.score_of(self.info(mobile=True), "jp")
        self.assertEqual(score, 100.0)

    def test_datacenter_is_penalised(self):
        score, reason = purity.score_of(self.info(hosting=True), "jp")
        self.assertEqual(score, 55.0)
        self.assertEqual(reason, "datacenter")

    def test_flagged_proxy_is_lowest(self):
        score, reason = purity.score_of(self.info(proxy=True), "jp")
        self.assertEqual(score, 20.0)
        self.assertEqual(reason, "flagged-proxy")

    def test_region_mismatch_subtracts(self):
        score, reason = purity.score_of(self.info(hosting=True, country_code="FI"), "de")
        self.assertEqual(score, 30.0)  # 55 - 25
        self.assertIn("region-mismatch", reason)

    def test_mismatch_never_goes_negative(self):
        score, _ = purity.score_of(self.info(proxy=True, country_code="SG"), "us")
        self.assertEqual(score, 0.0)

    def test_unknown_claimed_region_does_not_penalise(self):
        score, reason = purity.score_of(self.info(), "unknown")
        self.assertEqual(score, 100.0)
        self.assertNotIn("mismatch", reason)


class TraceParseTest(unittest.TestCase):
    def test_parses_ip_and_loc(self):
        raw = "fl=123f\nh=www.cloudflare.com\nip=203.0.113.7\nts=1700000000\nloc=JP\n"
        doc = purity.parse_trace(raw)
        self.assertEqual(doc["ip"], "203.0.113.7")
        self.assertEqual(doc["country_code"], "JP")
        self.assertEqual(doc["source"], "cdn-cgi/trace")

    def test_missing_ip_raises(self):
        with self.assertRaises(RuntimeError):
            purity.parse_trace("loc=JP\n")


class WatchdogVerdictTest(unittest.TestCase):
    def test_majority_rule(self):
        wd = load("watchdog_mod", os.path.join(ROOT, "tools", "watchdog.py"))
        self.assertTrue(wd.is_healthy(2, 3))
        self.assertFalse(wd.is_healthy(1, 3))
        self.assertTrue(wd.is_healthy(1, 1))   # 单目标时 1 次通过即健康
        self.assertFalse(wd.is_healthy(0, 1))


class ScanConfigTest(unittest.TestCase):
    """probe 核心配置必须允许 shell 容器从别的网段访问它的混合端口。"""

    def test_probe_config_binds_lan(self):
        make = load("mkprobe", os.path.join(ROOT, "shell", "make_probe_config.py"))
        doc = make.build(
            {"proxies": [{"name": "n1", "type": "ss", "server": "1.2.3.4", "port": 443,
                          "cipher": "aes-128-gcm", "password": "x"}]},
            7899,
            "s3cret",
            "0.0.0.0:9090",
        )
        self.assertEqual(doc["mode"], "global")
        self.assertTrue(doc["allow-lan"])
        self.assertEqual(doc["bind-address"], "*")
        self.assertIn("172.16.0.0/12", doc["lan-allowed-ips"])
        self.assertEqual(doc["mixed-port"], 7899)
        self.assertNotIn("proxy-groups", doc)  # global 模式由内核自动提供 GLOBAL

    def test_rejects_empty_pool(self):
        make = load("mkprobe2", os.path.join(ROOT, "shell", "make_probe_config.py"))
        with self.assertRaises(SystemExit):
            make.build({}, 7899, "s", "0.0.0.0:9090")


class UiStateTest(unittest.TestCase):
    """面板状态聚合：分数、纯净度、选路三者对不上时要能降级而不是崩。"""

    def test_render_with_scores_and_purity(self):
        ui = load("ui_mod", os.path.join(ROOT, "tools", "ui.py"))
        scores = {
            "generated": "2026-11-20T10:00:00+08:00",
            "platforms": {"gstatic": 0.9},
            "purity_weight": 0.1,
            "nodes": {
                "🇯🇵日本A": {
                    "overall": 96.0,
                    "final": 101.0,
                    "region": "jp",
                    "bonus": 5.0,
                    "platforms": {"gstatic": {"score": 98.0, "availability": 1.0}},
                    "purity": {"score": 55.0, "ip": "203.0.113.9", "country_code": "JP",
                               "claimed": "jp", "region_match": True, "hosting": True,
                               "proxy": False, "reason": "datacenter"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "artifacts"), exist_ok=True)
            for name, doc in (("scores.json", scores),):
                with open(os.path.join(tmp, "artifacts", name), "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
            ui.DATA_DIR = tmp
            ui.ARTIFACTS = os.path.join(tmp, "artifacts")
            ui.SCORES = os.path.join(ui.ARTIFACTS, "scores.json")
            ui.PURITY = os.path.join(ui.ARTIFACTS, "purity.json")
            ui.RUN_LOG = os.path.join(tmp, "run.log")
            ui.API = "http://127.0.0.1:1"  # 内核不可达 -> 组为空，页面仍要能出
            st = ui.state()
            page = ui.render(st)
        self.assertEqual(len(st["nodes"]), 1)
        self.assertEqual(st["nodes"][0]["purity"], 55.0)
        self.assertEqual(st["nodes"][0]["country"], "JP")
        self.assertIn("mihomo-stack", page)
        self.assertIn("日本A", page)


if __name__ == "__main__":
    unittest.main()
