"""Migrate a private Music 2.2 configuration without resetting source preferences.

Run offline after backing up the configuration, before starting Music 3.3.1.
The output contains the input's credentials; keep both files private.
"""

import argparse
import copy
import json
from pathlib import Path


def schema_defaults(schema: dict) -> dict:
    """Read values from the supplied, pinned upstream schema."""
    return {
        key: schema_defaults(value["items"])
        if value["type"] == "object"
        else copy.deepcopy(value.get("default", [] if value["type"] in {"list", "template_list"} else ""))
        for key, value in schema.items()
    }


def migrate(old: dict, schema: dict, templates: list[dict]) -> dict:
    """Keep old fields for rollback and explicitly map them into the new layout."""
    if old.get("players_template"):
        return copy.deepcopy(old)
    result = schema_defaults(schema)
    for section in ("search_config", "send_config"):
        for key in result[section]:
            if key in old:
                result[section][key] = copy.deepcopy(old[key])
    aliases = {
        "netease": {"netease", "网易云音乐", "网易云", "网易点歌"},
        "netease_api": {"netease_nodejs", "网易云nodejs版", "nj点歌", "网易nj"},
        "txqq": {"txqq", "txqq聚合平台", "qq点歌", "酷狗点歌", "酷我点歌", "百度点歌", "一听点歌", "咪咕点歌", "荔枝点歌", "蜻蜓点歌", "喜马点歌", "5sing原创", "5sing翻唱", "全民k歌"},
    }
    selected = str(old.get("default_player_name", "网易点歌")).strip().lower()
    preferred = next((name for name, values in aliases.items() if selected in values), None)
    if preferred is None:
        raise ValueError("Unknown legacy default player; choose its replacement explicitly.")
    sources = copy.deepcopy(templates)
    for node in sources:
        name = node["__template_key"]
        node["enable"] = name in aliases
        node["priority"] = 1 if name == preferred else int(node["priority"]) + 1
        if "proxy" in old:
            node["proxy"] = old["proxy"]
        if name == "netease":
            node["trigger_words"] = ["网易云", "网易点歌"]
            for key in ("enc_sec_key", "enc_params"):
                if key in old:
                    node[key] = old[key]
        elif name == "netease_api":
            node["trigger_words"] = ["nj点歌", "网易nj"]
            if "nodejs_base_url" in old:
                node["base_url"] = old["nodejs_base_url"]
        elif name == "txqq":
            node["trigger_words"] = [
                "qq点歌:qq", "酷狗点歌:kugou", "酷我点歌:kuwo", "百度点歌:baidu",
                "一听点歌:1ting", "咪咕点歌:migu", "荔枝点歌:lizhi", "蜻蜓点歌:qingting",
                "喜马点歌:ximalaya", "5sing原创:5singyc", "5sing翻唱:5singfc", "全民K歌:kg",
            ]
    result["players_template"] = sources
    # Preserve unknown fields and the old rollback layout, without letting its
    # empty new-schema defaults overwrite the explicitly migrated sections.
    return {**copy.deepcopy(old), **result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    read = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
    result = migrate(read(args.input), read(args.plugin / "_conf_schema.json"), read(args.plugin / "default_template.json"))
    # Refuse to overwrite the input or any previous output.
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print("Migrated configuration written; no values printed.")


if __name__ == "__main__":
    main()
