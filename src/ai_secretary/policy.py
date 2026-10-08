"""Provider route policy; a transport cannot attest an upstream model endpoint."""
from .resource_loader import load_json


def model_policy():
    return load_json("model-policy.json")


def require_on_prem_provider(provider):
    if provider not in model_policy()["on_prem_providers"]:
        raise PermissionError("Secretary accepts only a configured on-prem provider route")
