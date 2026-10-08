"""Canonical package data shared by the core and its host adapters."""
import json
from importlib.resources import files


def resource(name):
    return files("ai_secretary").joinpath("resources", name)


def load_json(name):
    return json.loads(resource(name).read_text(encoding="utf-8"))
