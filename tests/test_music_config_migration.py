"""Keep private settings during the Music 2.x to 3.x schema migration."""

import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("music_migration", Path(__file__).resolve().parents[1] / "compat/music/migrate_config.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MusicMigrationTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "search_config": {"type": "object", "items": {"song_limit": {"type": "int", "default": 5}}},
            "send_config": {"type": "object", "items": {"ark_key": {"type": "string", "default": ""}, "send_modes": {"type": "list", "default": ["card"]}}},
            "players_template": {"type": "template_list", "default": []},
        }
        self.templates = [{"__template_key": key, "priority": i + 1, "enable": True} for i, key in enumerate(["netease", "netease_api", "txqq", "neko", "apple", "qqmusic"])]
        self.old = {"default_player_name": "网易点歌", "song_limit": 2, "ark_key": "fixture-secret", "send_modes": ["record_local"], "nodejs_base_url": "http://127.0.0.1:12345", "proxy": "http://127.0.0.1:12346"}

    def test_keeps_credentials_modes_default_platform_and_disables_new_sources(self):
        result = migration.migrate(self.old, self.schema, self.templates)
        self.assertEqual(result["send_config"], {"ark_key": "fixture-secret", "send_modes": ["record_local"]})
        self.assertEqual(result["search_config"]["song_limit"], 2)
        nodes = {node["__template_key"]: node for node in result["players_template"]}
        self.assertEqual({k for k, n in nodes.items() if n["enable"]}, {"netease", "netease_api", "txqq"})
        self.assertEqual(nodes["netease"]["priority"], 1)
        self.assertEqual(nodes["netease_api"]["base_url"], self.old["nodejs_base_url"])
        self.assertTrue(all(n["proxy"] == self.old["proxy"] for n in nodes.values()))
        self.assertNotIn("players_template", self.old)
        self.assertEqual(migration.migrate(result, self.schema, self.templates), result)

    def test_unknown_default_is_not_silently_replaced(self):
        with self.assertRaises(ValueError):
            migration.migrate({**self.old, "default_player_name": "unknown"}, self.schema, self.templates)
