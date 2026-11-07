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
from server.network import get_network_info


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


class NetworkInfoTestCase(unittest.TestCase):
    def test_network_info_structure(self):
        info = get_network_info()
        self.assertIn("tailscale", info)


class ServerDnsRebindingTestCase(unittest.TestCase):
    def test_localhost_valid(self):
        self.assertTrue(is_valid_host_header("localhost"))
        # Tailscale: IP из 100.64.0.0/10 и MagicDNS-имена (раньше отклонялись -> приложение не подключалось)
        self.assertTrue(is_valid_host_header("100.101.102.103:8765"))
        self.assertTrue(is_valid_host_header("my-pc.tail1234.ts.net:8765"))
        self.assertFalse(is_valid_host_header("evil.example.com"))
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



class AudioConverterTestCase(unittest.TestCase):
    def test_light_mode_is_mono_and_smaller(self):
        from server.audio import AudioConverter
        import struct
        conv = AudioConverter(48000, 2, True)
        self.assertEqual((conv.out_rate, conv.out_channels), (24000, 1))
        data = struct.pack("<" + "h" * 960 * 2, *([1000, -1000] * 960))
        out = conv.convert(data)
        self.assertEqual(len(out), 960 // 2 * 2)

    def test_lan_mode_passthrough(self):
        from server.audio import AudioConverter
        conv = AudioConverter(44100, 2, False)
        self.assertEqual(conv.convert(b"abcd"), b"abcd")
        self.assertEqual((conv.out_rate, conv.out_channels), (44100, 2))


if __name__ == "__main__":
    unittest.main()


class PairingViaTunnelTestCase(unittest.TestCase):
    """Через Cloudflare-туннель запросы приходят с 127.0.0.1: токен через /api/pair отдаваться не должен."""

    @classmethod
    def setUpClass(cls):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from server import Handler
        cls.http = http.client
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _pair(self, headers=None):
        c = self.http.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.request("GET", "/api/pair", headers=headers or {})
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_pair_closed_by_default(self):
        from server.auth import close_pairing
        close_pairing()
        self.assertEqual(self._pair(), 403)

    def test_pair_allowed_locally_when_open_and_only_once(self):
        from unittest import mock
        from server.auth import open_pairing
        open_pairing(60)
        with mock.patch("server.config.save_config"):
            self.assertEqual(self._pair(), 200)
        self.assertEqual(self._pair(), 403)  # окно закрылось после выдачи токена

    def test_pair_blocked_via_cloudflare_headers(self):
        from server.auth import open_pairing
        open_pairing(60)  # даже при открытом окне через туннель токен не отдаётся
        self.assertEqual(self._pair({"CF-Connecting-IP": "203.0.113.5"}), 403)
        self.assertEqual(self._pair({"X-Forwarded-For": "203.0.113.5"}), 403)

    def test_pair_blocked_for_trycloudflare_host(self):
        self.assertEqual(self._pair({"Host": "abc-def.trycloudflare.com"}), 403)
