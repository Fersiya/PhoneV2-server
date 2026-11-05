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
from server.handler import _is_local_request


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


if __name__ == "__main__":
    unittest.main()
