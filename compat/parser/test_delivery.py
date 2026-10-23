"""Exercise real parser sending logic with synthetic results and no QQ network."""
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

PARSER = Path(os.environ["PARSER_PLUGIN_DIR"]).resolve()
sender_path = Path(os.environ.get("PARSER_SENDER_PATH", str(PARSER / "core/sender.py")))
package = ModuleType("parser_delivery_test")
package.__path__ = [str(PARSER)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location("parser_delivery_test.core.sender", sender_path)
sender_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sender_module
spec.loader.exec_module(sender_module)


class ParserDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.observer = Mock()
        self.bridge = SimpleNamespace(activated=True, star_cls=SimpleNamespace(observe_parsed_link=self.observer))
        self.sender = sender_module.MessageSender.__new__(sender_module.MessageSender)
        self.sender.cfg = SimpleNamespace(context=SimpleNamespace(get_registered_star=Mock(return_value=self.bridge)))
        self.sender._send_group = AsyncMock(return_value=True)
        self.sender._resolve_groups = Mock(return_value=[object()])
        self.event = SimpleNamespace(send=AsyncMock(), chain_result=lambda chain: chain)
        self.result = SimpleNamespace(
            header="哔哩哔哩 @外部作者 | 纸船实验", text="蓝色纸船漂了十米", extra={"info": "平台摘要"},
            contents=[], repost=None, url="https://www.bilibili.com/video/example", platform=SimpleNamespace(display_name="哔哩哔哩"),
            get_resource_id=lambda: "fictional-resource",
        )

    async def test_success_publishes_plain_metadata(self):
        await self.sender.send_parse_result(self.event, self.result)
        self.observer.assert_called_once()
        payload = self.observer.call_args.args[1]
        self.assertIn("蓝色纸船", payload["text"])
        self.assertIn("平台摘要", payload["text"])
        self.assertEqual(payload["resource_id"], "fictional-resource")

    async def test_failed_send_does_not_enter_context(self):
        self.sender._send_group.return_value = False
        self.event.send.side_effect = RuntimeError("synthetic send failure")
        await self.sender.send_parse_result(self.event, self.result)
        self.observer.assert_not_called()

    async def test_successful_text_fallback_enters_context(self):
        self.sender._send_group.return_value = False
        await self.sender.send_parse_result(self.event, self.result)
        self.observer.assert_called_once()

    async def test_disabled_bridge_gets_no_context(self):
        self.bridge.activated = False
        await self.sender.send_parse_result(self.event, self.result)
        self.observer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
