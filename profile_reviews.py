"""Persist reviewed, evidence-backed changes to one configured QQ profile."""

import json
import time
import uuid
from copy import deepcopy
from pathlib import Path

SCHEMA = json.loads(
    Path(__file__).with_name("_conf_schema.json").read_text(encoding="utf-8")
)["user_profiles"]["templates"]["person"]["items"]
FIELDS = {
    key: value for key, value in SCHEMA.items() if key not in {"qq", "allow_learning"}
}


def normalized_profile(profile: dict) -> dict:
    """Compare effective values even when the dashboard has filled defaults."""
    return {
        key: deepcopy(profile.get(key, spec["default"])) for key, spec in SCHEMA.items()
    }


def validate_value(field: str, value, *, edited: bool = False):
    """Restrict both model suggestions and administrator edits to schema fields."""
    if field not in FIELDS:
        raise ValueError("不允许学习或修改这个字段。")
    spec = FIELDS[field]
    if spec["type"] == "list":
        if edited and isinstance(value, str):
            value = [line.strip() for line in value.splitlines() if line.strip()]
        if not isinstance(value, list) or len(value) > 24:
            raise ValueError("列表最多包含 24 项。")
        if any(not isinstance(item, str) or len(item) > 120 for item in value):
            raise ValueError("列表各项必须是长度不超过 120 的文本。")
        value = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if spec.get("options") and any(item not in spec["options"] for item in value):
            raise ValueError("列表包含未知选项。")
    elif not isinstance(value, str) or len(value) > (
        80 if field == "nickname" else 2000
    ):
        raise ValueError("字段必须是长度受限的文本。")
    else:
        value = value.strip()
        if spec.get("options") and value not in spec["options"]:
            raise ValueError("字段包含未知选项。")
    return value


def display_value(value) -> str:
    """Render lists as editable lines rather than executable JSON."""
    return "\n".join(value) if isinstance(value, list) else str(value)


class ProfileReviewStore:
    """Single-loop store with a recovery journal around configuration writes."""

    def __init__(self, config, directory: Path, changed):
        self.config = config
        self.directory = directory
        self.path = directory / "profile-reviews.json"
        self.changed = changed
        self.records = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else []
        )
        self.recover()

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    def current_config(self) -> dict:
        """Read fresh disk state so a dashboard edit cannot be overwritten."""
        path = getattr(self.config, "config_path", None)
        if path and Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return deepcopy(dict(self.config))

    @staticmethod
    def profile(config: dict, qq: str) -> dict:
        matches = [
            p
            for p in config.get("user_profiles", [])
            if str(p.get("qq", "")).strip() == qq
        ]
        if len(matches) != 1:
            raise ValueError("成员已删除或 QQ 号重复，请检查成员配置。")
        return matches[0]

    @staticmethod
    def scope(config: dict) -> dict:
        return {
            key: deepcopy(config.get(key, {} if key == "relationship_presets" else ""))
            for key in ("owner_qq", "group_qq", "relationship_presets")
        }

    def recover(self) -> None:
        """Resolve an interrupted approval without applying it a second time."""
        dirty = False
        for row in self.records:
            if row["status"] not in {"applying", "reverting"}:
                continue
            try:
                current = normalized_profile(
                    self.profile(self.current_config(), row["qq"])
                )
            except ValueError:
                current = None
            if current == row["write_after"]:
                row["status"] = row["write_status"]
                if row["status"] == "approved":
                    row["new_value"] = deepcopy(current[row["field"]])
                    self._rebase_pending(row, row["write_before"], current)
            elif current == row["write_before"]:
                row["status"] = row["previous_status"]
            else:
                row["status"] = "conflict"
            dirty = True
        if dirty:
            self._save()
            self.config.update(self.current_config())
            self.changed()

    def _rebase_pending(self, row: dict, before: dict, after: dict) -> None:
        """Keep other fields from the same reviewed snapshot usable in a batch."""
        for other in self.records:
            if (
                other["status"] == "pending"
                and other["qq"] == row["qq"]
                and other["field"] != row["field"]
                and other["snapshot"] == before
            ):
                other["snapshot"] = deepcopy(after)

    def add(
        self, qq: str, snapshot: dict, scope: dict, changes: list, evidence: list
    ) -> int:
        """Create pending rows only; no model path can approve or write settings."""
        config = self.current_config()
        current = normalized_profile(self.profile(config, qq))
        if not config.get("profile_learning_enabled") or not current.get(
            "allow_learning", True
        ):
            return 0
        if current != snapshot or self.scope(config) != scope:
            return 0
        if sum(row["status"] == "pending" for row in self.records) >= 500:
            raise ValueError("个人设置待审批数量达到 500，请先处理已有建议。")
        valid_evidence = {item["id"]: item for item in evidence}
        rows = []
        for change in changes[:6]:
            if not isinstance(change, dict):
                raise TypeError("Invalid profile change object.")
            field = change.get("field", "")
            value = validate_value(field, change.get("value"))
            ids = change.get("evidence_ids", [])
            if (
                not isinstance(ids, list)
                or not ids
                or any(
                    not isinstance(item, str) or item not in valid_evidence
                    for item in ids
                )
            ):
                raise ValueError("Profile change has no verifiable evidence.")
            confidence = change.get("confidence", 0)
            if not isinstance(confidence, (int, float)) or not 0.8 <= confidence <= 1:
                continue
            # Relationship labels require multiple distinct observations, not one joke.
            if (
                field == "relationship"
                and len({valid_evidence[item]["text"] for item in ids}) < 3
            ):
                continue
            if value == current[field] or any(
                row["qq"] == qq
                and row["field"] == field
                and row["new_value"] == value
                and row["snapshot"] == snapshot
                for row in self.records + rows
            ):
                continue
            rows.append(
                {
                    "id": "qq_profile_" + uuid.uuid4().hex,
                    "review_source": "qq_profile",
                    "status": "pending",
                    "qq": qq,
                    "nickname": current["nickname"],
                    "field": field,
                    "old_value": current[field],
                    "new_value": value,
                    "snapshot": deepcopy(snapshot),
                    "scope": deepcopy(scope),
                    "evidence": [
                        deepcopy(valid_evidence[item]) for item in dict.fromkeys(ids)
                    ],
                    "reason": str(change.get("reason", ""))[:600],
                    "confidence": confidence,
                    "timestamp": time.time(),
                }
            )
        if rows:
            self.records.extend(rows)
            self._save()
        return len(rows)

    def list(self, reviewed: bool = False) -> list[dict]:
        self.recover()
        result = []
        for record in self.records:
            if (record["status"] != "pending") != reviewed:
                continue
            row = deepcopy(record)
            spec = FIELDS[row["field"]]
            row.update(
                {
                    "title": f"{row['nickname']}（{row['qq']}）· {spec['description']}",
                    "created_at": row["timestamp"],
                    "review_detail": row["reason"],
                    "before": display_value(row["old_value"]),
                    "after": display_value(row["new_value"]),
                    "field_type": spec["type"],
                    "field_options": spec.get("options", []),
                    "field_label": spec["description"],
                }
            )
            row.pop("snapshot", None)
            row.pop("scope", None)
            for key in (
                "write_before",
                "write_after",
                "previous_status",
                "write_status",
            ):
                row.pop(key, None)
            result.append(row)
        return result

    def review(
        self, row_id: str, action: str, modified_content=None, comment: str = ""
    ) -> tuple[bool, str]:
        """Called only by the authenticated administrator review endpoint."""
        self.recover()
        row = next((item for item in self.records if item["id"] == row_id), None)
        if row is None:
            return False, "找不到个人设置建议。"
        if action == "delete":
            self.records.remove(row)
            self._save()
            return True, "已删除建议记录；不会撤销已经保存的设置。"
        if action == "reject" and row["status"] == "pending":
            row.update(status="rejected", review_time=time.time(), comment=comment)
            self._save()
            return True, "已拒绝建议，成员设置未更改。"
        if action == "revert" and row["status"] == "rejected":
            row["status"] = "pending"
            self._save()
            return True, "已恢复为待审批。"
        if (action, row["status"]) not in {
            ("approve", "pending"),
            ("revert", "approved"),
        }:
            return False, "这条建议的状态不允许该操作。"
        config = self.current_config()
        try:
            profile = self.profile(config, row["qq"])
            current = normalized_profile(profile)
            if not config.get("profile_learning_enabled") or not current.get(
                "allow_learning", True
            ):
                return False, "个人设置学习已关闭，未修改配置。"
            expected = row["snapshot"] if action == "approve" else row["write_after"]
            if current != expected or self.scope(config) != row["scope"]:
                return (
                    False,
                    "成员设置或关系预设已修改；旧建议已过期，请删除后等待新的建议。",
                )
            value = (
                row["old_value"]
                if action == "revert"
                else validate_value(
                    row["field"],
                    row["new_value"] if modified_content is None else modified_content,
                    edited=modified_content is not None,
                )
            )
        except ValueError as error:
            return False, str(error)
        before_config = deepcopy(config)
        profile[row["field"]] = value
        after = normalized_profile(profile)
        backup = self.directory / "profile-config-backups" / f"{row_id}-{action}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(
            json.dumps(before_config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        row.update(
            previous_status=row["status"],
            status="applying" if action == "approve" else "reverting",
            write_before=current,
            write_after=after,
            write_status="approved" if action == "approve" else "reverted",
            review_time=time.time(),
            comment=comment,
        )
        self._save()
        memory = deepcopy(dict(self.config))
        try:
            self.config.save_config(config)
        except Exception:
            self.config.clear()
            self.config.update(memory)
            # The journal remains unresolved until disk state can be checked.
            raise
        self.changed()
        if normalized_profile(self.profile(self.current_config(), row["qq"])) != after:
            raise RuntimeError(
                "Configuration changed during approval; review was not confirmed."
            )
        row["status"] = row["write_status"]
        if action == "approve":
            row["new_value"] = value
            self._rebase_pending(row, current, after)
        self._save()
        return (
            True,
            "已批准并保存到该成员设置。"
            if action == "approve"
            else "已撤销该次修改，恢复审批前的成员设置。",
        )
