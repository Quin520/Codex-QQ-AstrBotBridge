"""Check structured plugin responses through the real AstrBot provider API."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import jsonschema
from astrbot.api.star import Context
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot_plugin_local_codex.provider import LocalCodexProvider


class ResponseFormatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch(
            "astrbot_plugin_local_codex.provider.StarTools.get_data_dir",
            return_value=Path(self.directory.name),
        ):
            self.provider = LocalCodexProvider(
                {"codex_exe": sys.executable, "model": "default"}, {}
            )
        self.context = object.__new__(Context)
        self.context.provider_manager = SimpleNamespace(
            get_provider_by_id=AsyncMock(return_value=self.provider)
        )
        self.payload = {"answer": "{}", "tool_calls": []}
        self.requests = []

        async def run(prompt, _thread, **kwargs):
            self.requests.append(
                (
                    json.loads(prompt.split("\n")[-1]),
                    json.loads(kwargs["output_schema"].read_text(encoding="utf-8")),
                )
            )
            return None, json.dumps(self.payload)

        self.run = AsyncMock(side_effect=run)
        runner_patch = patch(
            "astrbot_plugin_local_codex.provider.CodexRunner.run", self.run
        )
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        # Same array-root shape used by the installed daily-analysis plugin.
        self.format = {
            "type": "json_schema",
            "json_schema": {
                "name": "topics",
                "strict": True,
                "schema": {
                    "type": "array",
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "properties": {
                            "topic": {"type": "string"},
                            "contributors": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "detail": {"type": "string"},
                        },
                        "required": ["topic", "contributors", "detail"],
                        "additionalProperties": False,
                    },
                },
            },
        }

    async def generate(self, response_format, **kwargs):
        return await self.context.llm_generate(
            chat_provider_id="local_codex",
            prompt="Summarize the fictional conversation.",
            response_format=response_format,
            **kwargs,
        )

    async def test_daily_array_schema_survives_context_and_is_not_mutated(self):
        expected = [
            {"topic": "Lunch", "contributors": ["Alpha"], "detail": "Discussed soup"}
        ]
        self.payload["answer"] = json.dumps(expected)
        before = copy.deepcopy(self.format)
        response = await self.generate(self.format)
        self.assertEqual(json.loads(response.completion_text), expected)
        self.assertEqual(self.format, before)
        request, transport = self.requests[-1]
        self.assertEqual(request["response_format"], self.format)
        self.assertEqual(transport["properties"]["answer"], {"type": "string"})
        self.assertEqual(transport["properties"]["tool_calls"]["maxItems"], 0)

    async def test_json_object_and_text_output(self):
        for value in ({}, {"answer": "Hello", "count": 0, "active": False}):
            with self.subTest(value=value):
                self.payload["answer"] = json.dumps(value)
                response = await self.generate({"type": "json_object"})
                self.assertEqual(json.loads(response.completion_text), value)
        self.payload["answer"] = "Plain answer"
        response = await self.generate({"type": "text"})
        self.assertEqual(response.completion_text, "Plain answer")
        self.assertNotIn("response_format", self.requests[-1][0])

    async def test_invalid_json_or_wrong_object_root_is_an_error(self):
        for text in (
            "Not JSON",
            '```json\n{"ok":true}\n```',
            "[]",
            "null",
            "42",
            '{"score": NaN}',
            '{"score": Infinity}',
        ):
            with self.subTest(text=text):
                self.payload["answer"] = text
                with self.assertRaises((ValueError, jsonschema.ValidationError)):
                    await self.generate({"type": "json_object"})

    async def test_daily_schema_rejects_missing_fields_and_wrong_types(self):
        for value in (
            {},
            [{"topic": "Lunch", "detail": "Soup"}],
            [{"topic": "Lunch", "contributors": [123], "detail": "Soup"}],
            [{"topic": "Lunch", "contributors": [], "detail": "Soup", "extra": 1}],
        ):
            with self.subTest(value=value):
                self.payload["answer"] = json.dumps(value)
                with self.assertRaises(jsonschema.ValidationError):
                    await self.generate(self.format)

    async def test_bad_format_fails_before_model_request(self):
        for value in (
            "json_object",
            {},
            {"type": "unsupported"},
            {"type": "json_schema", "json_schema": {}},
            {"type": "json_schema", "json_schema": {"schema": {"type": "bad"}}},
            {
                "type": "json_schema",
                "json_schema": {"schema": {"$ref": "https://example.invalid/schema"}},
            },
        ):
            with (
                self.subTest(value=value),
                self.assertRaises((TypeError, ValueError, jsonschema.SchemaError)),
            ):
                await self.generate(value)
        self.run.assert_not_awaited()

    async def test_local_schema_references_are_supported(self):
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "linked",
                "schema": {
                    "$defs": {"answer": {"type": "string"}},
                    "type": "object",
                    "properties": {"answer": {"$ref": "#/$defs/answer"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
        }
        self.payload["answer"] = '{"answer":"Hello"}'
        response = await self.generate(response_format)
        self.assertEqual(json.loads(response.completion_text), {"answer": "Hello"})
        self.payload["answer"] = '{"answer":1}'
        with self.assertRaises(jsonschema.ValidationError):
            await self.generate(response_format)

    async def test_ref_property_names_and_literal_values_are_not_schema_references(
        self,
    ):
        literal = {"$ref": "value"}
        for schema in (
            {
                "type": "object",
                "properties": {"$ref": {"type": "string"}},
                "required": ["$ref"],
                "additionalProperties": False,
            },
            {"const": literal},
            {"enum": [literal]},
            {"type": "object", "default": literal, "examples": [literal]},
        ):
            with self.subTest(schema=schema):
                self.payload["answer"] = json.dumps(literal)
                response = await self.generate(
                    {"type": "json_schema", "json_schema": {"schema": schema}}
                )
                self.assertEqual(json.loads(response.completion_text), literal)

    async def test_nested_external_schema_references_are_rejected_before_model(self):
        for reference in (
            "https://example.invalid/schema",
            "file:///private/schema.json",
        ):
            for schema in (
                {"properties": {"value": {"$ref": reference}}},
                {"$defs": {"value": {"$ref": reference}}},
                {"items": {"$ref": reference}},
                {"anyOf": [{"$ref": reference}]},
                {"dependentSchemas": {"value": {"$ref": reference}}},
            ):
                with (
                    self.subTest(schema=schema),
                    self.assertRaisesRegex(ValueError, "only local schema references"),
                ):
                    await self.generate(
                        {"type": "json_schema", "json_schema": {"schema": schema}}
                    )
        self.run.assert_not_awaited()

    async def test_tools_still_require_allowlisted_names_and_valid_arguments(self):
        tool = FunctionTool(
            name="lookup",
            description="Look up fictional information",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
        )
        tools = ToolSet(tools=[tool])
        self.payload = {
            "answer": "",
            "tool_calls": [{"name": "lookup", "arguments_json": '{"key":"Alpha"}'}],
        }
        response = await self.generate(self.format, tools=tools)
        self.assertEqual(response.role, "tool")
        self.assertEqual(response.completion_text, "")
        self.assertEqual(response.tools_call_args, [{"key": "Alpha"}])
        for name, arguments in (("unlisted", "{}"), ("lookup", '{"key":123}')):
            with self.subTest(name=name, arguments=arguments):
                self.payload["tool_calls"] = [
                    {"name": name, "arguments_json": arguments}
                ]
                with self.assertRaises(jsonschema.ValidationError):
                    await self.generate(self.format, tools=tools)
        self.payload = {"answer": "[]", "tool_calls": []}
        response = await self.generate(self.format, tools=tools)
        self.assertEqual(response.completion_text, "[]")
        self.payload["answer"] = "Final answer that forgot JSON"
        with self.assertRaises(ValueError):
            await self.generate(self.format, tools=tools)


if __name__ == "__main__":
    unittest.main()
