"""Load immutable authoring presets, without rewriting saved instances."""

import json
from copy import deepcopy
from importlib.resources import files

from .models import ItemDefinition, ItemInstance, Placement


def merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else deepcopy(value)
        )
    return result


def catalog():
    raw = json.loads(files(__package__).joinpath("presets.v1.json").read_text(encoding="utf-8"))
    definitions = [ItemDefinition.model_validate(merge(raw["defaults"], p)) for p in raw["presets"]]
    if len({d.id for d in definitions}) != len(definitions):
        raise ValueError("预设标识重复。")
    return {
        "catalog_id": raw["catalog_id"],
        "catalog_version": raw["catalog_version"],
        "units": raw["units"],
        "presets": [d.model_dump() for d in definitions],
    }


def instantiate(preset_id: str, instance_id: str, placement: Placement) -> ItemInstance:
    source = catalog()
    definition = next((d for d in source["presets"] if d["id"] == preset_id), None)
    if definition is None:
        raise ValueError("预设不存在。")
    return ItemInstance(
        id=instance_id,
        definition=definition,
        placement=placement,
        state=definition["initial_state"],
        preset_catalog=source["catalog_id"],
        preset_version=source["catalog_version"],
    )
