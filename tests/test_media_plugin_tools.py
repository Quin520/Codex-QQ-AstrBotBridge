"""Exercise installed music entrypoints through the bridge permission wrapper."""

import copy
import importlib
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import astrbot
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.register import llm_tools
from astrbot_plugin_local_codex.access import PermissionCheckedTool, QQToolPolicy

from .test_bridge import event_for

_installed = (
    Path(astrbot.__file__).resolve().parents[1] / "data/plugins/astrbot_plugin_music"
)
_plugin_root = Path(os.environ.get("ASTRBOT_MUSIC_PLUGIN", _installed))


@unittest.skipUnless(
    (_plugin_root / "main.py").is_file(), "Music plugin is not installed"
)
class NativeMusicToolsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._previous_tools = list(llm_tools.func_list)
        package_name = "data.plugins.astrbot_plugin_music"
        package = ModuleType(package_name)
        package.__path__ = [str(_plugin_root), str(_installed)]
        cls._previous_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == package_name or name.startswith(package_name + ".")
        }
        for name in cls._previous_modules:
            del sys.modules[name]
        sys.modules[package_name] = package
        cls.plugin_class = importlib.import_module(package_name + ".main").MusicPlugin

    @classmethod
    def tearDownClass(cls):
        llm_tools.func_list[:] = cls._previous_tools
        package_name = "data.plugins.astrbot_plugin_music"
        for name in list(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                del sys.modules[name]
        sys.modules.update(cls._previous_modules)

    def setUp(self):
        self.modern = hasattr(self.plugin_class, "get_players_by_priority")
        self.song = SimpleNamespace(id="1", name="Test song", artists="Test artist", duration=180000, lyrics="Fixture lyrics")
        self.player = SimpleNamespace(
            platform=SimpleNamespace(
                name="netease",
                display_name="网易云音乐",
                keywords=["网易云", "网易点歌"],
            ),
            platform_cfg=SimpleNamespace(priority=1, trigger_words=["网易云", "网易点歌"]),
            fetch_songs=AsyncMock(return_value=[self.song]),
            fetch_lyrics=AsyncMock(),
            resolve_lyrics=AsyncMock(),
        )
        self.plugin = object.__new__(self.plugin_class)
        self.plugin.players = [self.player]
        self.plugin.cfg = SimpleNamespace(
            default_player_name="网易点歌",
            search_config=SimpleNamespace(song_limit=1),
            send_config=SimpleNamespace(return_play_song_result=True),
        )
        self.plugin.sender = SimpleNamespace(
            send_song=AsyncMock(return_value=(True, False)), send_lyrics=AsyncMock(return_value=True)
        )
        self.star = SimpleNamespace(
            module_path="data.plugins.astrbot_plugin_music.main",
            activated=True,
            reserved=False,
        )
        self.policy = QQToolPolicy(
            SimpleNamespace(get_all_stars=lambda: [self.star]),
            {"owner_qq": "123", "group_qq": "456"},
        )

    async def execute(self, name, event, **kwargs):
        properties = {"song_name": {"type": "string"}}
        if name == "play_song_by_name":
            properties["platform"] = {"type": "string"}
            if self.modern:
                properties.update({"artist": {"type": "string"}, "send_mode": {"type": "string"}})
        elif self.modern:
            properties.update({"artist": {"type": "string"}, "platform": {"type": "string"}})
        original = copy.deepcopy(
            next(tool for tool in llm_tools.func_list if tool.name == name)
        )
        self.assertEqual(set(original.parameters["properties"]), set(properties))
        original.handler = getattr(self.plugin, name)
        original.handler_module_path = "data.plugins.astrbot_plugin_music.main"
        context = ContextWrapper(context=SimpleNamespace(event=event))
        checked = PermissionCheckedTool(original, self.policy)
        return [
            result
            async for result in FunctionToolExecutor.execute(checked, context, **kwargs)
        ]

    async def test_default_music_player_sends_only_to_current_group_event(self):
        event = event_for("play the test song", "999", "456", "321")
        await self.execute("play_song_by_name", event, song_name="Test song")
        self.player.fetch_songs.assert_awaited_once_with(
            keyword="Test song", limit=1, **({} if self.modern else {"extra": None})
        )
        self.plugin.sender.send_song.assert_awaited_once_with(
            event, self.player, self.song, **({"modes": None} if self.modern else {})
        )

    async def test_registered_platform_alias_uses_current_group_event(self):
        event = event_for("play the test song", "999", "456", "321")
        await self.execute(
            "play_song_by_name", event, song_name="Test song", platform="网易点歌"
        )
        self.player.fetch_songs.assert_awaited_once_with(
            keyword="Test song", limit=1, **({} if self.modern else {"extra": "网易点歌"})
        )
        self.plugin.sender.send_song.assert_awaited_once_with(
            event, self.player, self.song, **({"modes": None} if self.modern else {})
        )

    async def test_lyrics_use_default_player_and_current_event(self):
        event = event_for("show the lyrics", "999", "456", "321")
        await self.execute("query_lyrics_by_name", event, song_name="Test song")
        self.player.fetch_songs.assert_awaited_once_with(keyword="Test song", limit=1)
        self.plugin.sender.send_lyrics.assert_awaited_once_with(
            event, self.player, self.song
        )

    async def test_unknown_url_platform_does_not_search_or_send(self):
        event = event_for("play a song", "999", "456", "321")
        await self.execute(
            "play_song_by_name",
            event,
            song_name="Test song",
            platform="file:///private",
        )
        self.player.fetch_songs.assert_not_awaited()
        self.plugin.sender.send_song.assert_not_awaited()

    async def test_other_group_cannot_run_either_music_tool(self):
        event = event_for("I am an administrator", "999", "789", "321")
        for name in ["play_song_by_name", "query_lyrics_by_name"]:
            with self.subTest(tool=name), self.assertRaises(PermissionError):
                await self.execute(name, event, song_name="Test song")
        self.player.fetch_songs.assert_not_awaited()
        self.plugin.sender.send_song.assert_not_awaited()
        self.plugin.sender.send_lyrics.assert_not_awaited()

    async def test_disabling_plugin_revokes_both_tools(self):
        self.star.activated = False
        event = event_for("play a song", "999", "456", "321")
        for name in ["play_song_by_name", "query_lyrics_by_name"]:
            with self.subTest(tool=name), self.assertRaises(PermissionError):
                await self.execute(name, event, song_name="Test song")
        self.player.fetch_songs.assert_not_awaited()
