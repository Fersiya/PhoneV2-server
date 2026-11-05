"""Тесты для сервера PhoneV2."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.config import DEFAULT_APPS, PORT
from server.files import is_safe_path, MEDIA_EXTS
from server.power import POWER
from server.media_keys import MEDIA_KEYS, COMBO_KEYS
from server.ota import get_apk_manifest_version
from server.auth import _auth_fails, check_authorized
from server.handler import _is_local_request, is_valid_host_header
from server.network import get_tunnel_info, is_cloudflare_running, stop_cloudflare_tunnel


class ServerConfigTestCase(unittest.TestCase):
    def test_port_is_8765(self):
        self.assertEqual(PORT, 8765)

    def test_default_apps_structure(self):
        self.assertTrue(len(DEFAULT_APPS) > 0)
        for app in DEFAULT_APPS:
            self.assertIn("id", app)
            self.assertIn("title", app)
            self.assertIn("launch", app)
            self.assertIsInstance(app["launch"], list)
            self.assertIn("force", app)


class ServerSecurityTestCase(unittest.TestCase):
    def test_safe_path_valid(self):
        # Обычные локальные пути безопасны
        self.assertTrue(is_safe_path("C:\\Users\\Public\\Documents"))
        self.assertTrue(is_safe_path("D:\\Photos\\vacation.jpg"))

    def test_safe_path_blocks_unc(self):
        # UNC пути типа \\attacker\share блокируются
        self.assertFalse(is_safe_path(r"\\192.168.1.50\share\passwords.txt"))
        self.assertFalse(is_safe_path("//attacker.com/evil"))

    def test_safe_path_blocks_sam_and_security(self):
        # Доступ к системным кустам реестра SAM/SECURITY блокируется
        self.assertFalse(is_safe_path(r"C:\Windows\System32\config\SAM"))
        self.assertFalse(is_safe_path(r"C:\Windows\System32\config\SECURITY"))

    def test_media_extensions(self):
        self.assertIn(".jpg", MEDIA_EXTS)
        self.assertIn(".png", MEDIA_EXTS)
        self.assertIn(".mp4", MEDIA_EXTS)

    def test_is_local_request(self):
        self.assertTrue(_is_local_request("127.0.0.1"))
        self.assertTrue(_is_local_request("192.168.0.152"))
        self.assertTrue(_is_local_request("192.168.1.100"))
        self.assertTrue(_is_local_request("10.0.0.5"))
        self.assertTrue(_is_local_request("172.16.0.10"))
        # Внешние IP публичного интернета не должны иметь доступ к сопряжению
        self.assertFalse(_is_local_request("8.8.8.8"))
        self.assertFalse(_is_local_request("185.2.186.215"))
        self.assertNotIn(".exe", MEDIA_EXTS)
        self.assertNotIn(".bat", MEDIA_EXTS)
        self.assertNotIn(".ps1", MEDIA_EXTS)


class ServerPowerAndMediaTestCase(unittest.TestCase):
    def test_power_commands_structure(self):
        for route, (cmd, timer_sec) in POWER.items():
            self.assertTrue(route.startswith("/"))
            self.assertIsInstance(cmd, list)
            self.assertTrue(len(cmd) > 0)
            if timer_sec is not None:
                self.assertIsInstance(timer_sec, int)

    def test_media_keys_valid(self):
        for action, (vk, repeats) in MEDIA_KEYS.items():
            self.assertIsInstance(vk, int)
            self.assertGreater(vk, 0)
            self.assertGreater(repeats, 0)

    def test_combo_keys_valid(self):
        for action, keys in COMBO_KEYS.items():
            self.assertIsInstance(keys, list)
            self.assertTrue(len(keys) > 0)
            for k in keys:
                self.assertIsInstance(k, int)
                self.assertGreater(k, 0)


class ServerOtaTestCase(unittest.TestCase):
    def test_invalid_apk_manifest_version(self):
        # На несуществующем или некорректном файле функция должна возвращать (None, None)
        v_code, v_name = get_apk_manifest_version(__file__)
        self.assertIsNone(v_code)
        self.assertIsNone(v_name)


class ServerTunnelTestCase(unittest.TestCase):
    def test_get_tunnel_info_structure(self):
        info = get_tunnel_info()
        self.assertIn("installed", info)
        self.assertIn("running", info)
        self.assertIn("url", info)
        self.assertIn("cf_path", info)
        self.assertIsInstance(info["running"], bool)
        self.assertIsInstance(info["installed"], bool)

    def test_stop_tunnel_when_not_running(self):
        ok, msg = stop_cloudflare_tunnel()
        self.assertTrue(ok)
        self.assertFalse(is_cloudflare_running())



class ServerDnsRebindingTestCase(unittest.TestCase):
    def test_localhost_valid(self):
        self.assertTrue(is_valid_host_header("localhost"))
        self.assertTrue(is_valid_host_header("localhost:8765"))
        self.assertTrue(is_valid_host_header("127.0.0.1"))
        self.assertTrue(is_valid_host_header("127.0.0.1:8765"))
        self.assertTrue(is_valid_host_header("[::1]:8765"))

    def test_lan_ips_valid(self):
        self.assertTrue(is_valid_host_header("192.168.0.10:8765"))
        self.assertTrue(is_valid_host_header("10.0.0.5:8765"))
        self.assertTrue(is_valid_host_header("172.16.1.2:8765"))
        self.assertTrue(is_valid_host_header("169.254.1.1:8765"))

    def test_cloudflare_tunnel_valid(self):
        self.assertTrue(is_valid_host_header("my-demo-tunnel.trycloudflare.com"))
        self.assertTrue(is_valid_host_header("abc-123.trycloudflare.com:8765"))

    def test_malicious_domains_blocked(self):
        self.assertFalse(is_valid_host_header("attacker.com"))
        self.assertFalse(is_valid_host_header("attacker.com:8765"))
        self.assertFalse(is_valid_host_header("evil-site.org"))
        self.assertFalse(is_valid_host_header("8.8.8.8"))
        self.assertFalse(is_valid_host_header("1.1.1.1:8765"))
        self.assertFalse(is_valid_host_header("malicious.internal.attacker.com"))


class ServerTrayModuleTestCase(unittest.TestCase):
    def test_tray_functions_imported(self):
        from server import start_tray_thread, show_status_dialog, remove_tray_icon
        self.assertTrue(callable(start_tray_thread))
        self.assertTrue(callable(show_status_dialog))
        self.assertTrue(callable(remove_tray_icon))


if __name__ == "__main__":
    unittest.main()
