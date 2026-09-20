"""Compose Claude settings with an optional settings-hooks.json in the same folder."""
from copy import deepcopy
import json
from pathlib import Path


TEMPLATE_NAME = "settings-hooks.json"


def read_settings(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        # Do not include JSON contents: account settings may contain credentials.
        raise ValueError(f"Invalid JSON in {path.name}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def merge_settings(common, account):
    """Common keys/list items first; account scalar preferences take precedence.

    Combine matching hook groups as well as their handlers to avoid executing
    the same handler twice when a profile already contains part of the template.
    Inputs are never modified.
    """
    if isinstance(common, dict) and isinstance(account, dict):
        result = deepcopy(common)
        for key, value in account.items():
            result[key] = merge_settings(result[key], value) if key in result else deepcopy(value)
        return result
    if isinstance(common, list) and isinstance(account, list):
        result = deepcopy(common)
        for value in account:
            if value in result:
                continue
            if isinstance(value, dict) and isinstance(value.get("hooks"), list):
                metadata = {k: v for k, v in value.items() if k != "hooks"}
                group = next((item for item in result
                              if isinstance(item, dict) and isinstance(item.get("hooks"), list)
                              and {k: v for k, v in item.items() if k != "hooks"} == metadata), None)
                if group is not None:
                    group["hooks"] = merge_settings(group["hooks"], value["hooks"])
                    continue
            result.append(deepcopy(value))
        return result
    return deepcopy(account)


def with_shared_settings(directory, account):
    """Read the template on each write, so account switches pick up edits."""
    if not isinstance(account, dict):
        raise ValueError("Account settings must contain a JSON object")
    template = Path(directory) / TEMPLATE_NAME
    try:
        common = read_settings(template)
    except FileNotFoundError:
        return deepcopy(account)
    for data in (common, account):
        for key in ("hooks", "permissions"):
            if key in data and not isinstance(data[key], dict):
                raise ValueError(f"{key} must contain a JSON object")
        for groups in data.get("hooks", {}).values():
            if not isinstance(groups, list) or any(
                not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
                or any(not isinstance(handler, dict) for handler in group["hooks"])
                for group in groups
            ):
                raise ValueError("Invalid hook groups in settings")
    return merge_settings(common, account)
