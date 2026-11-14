"""Approved resource routing and live environment identity for Generator sampling."""
NEMO_PIN = "98de01c6168062fb746d45f8cd56b38b5580555b"
ENVIRONMENTS = {"shared": "nemo-toolathlon-resources-v1", "isolated": "nemo-isolated-toolathlon-resources-v1"}


def resource_profile(backend, server_name):
    if backend not in ENVIRONMENTS:
        raise ValueError("Unknown Generator resource backend")
    if backend == "shared" and server_name == "toolathlon_resources":
        prefix = ""
    elif backend == "isolated" and server_name == "toolathlon_resources":
        prefix = "/isolated"
    elif backend == "isolated" and server_name == "isolated_toolathlon_resources":
        prefix = ""
    else:
        raise ValueError("Resource server name does not match the declared Generator backend")
    return {"backend": backend, "server_name": server_name, "route_prefix": prefix,
            "environment_version": ENVIRONMENTS[backend], "training_parallelism_enabled": False}


def validate_resource_provenance(profile, provenance):
    if (not isinstance(provenance, dict) or provenance.get("framework") != "nemo_gym" or provenance.get("source_commit") != NEMO_PIN
        or provenance.get("resource_backend") != profile["backend"]
        or provenance.get("environment_version") != profile["environment_version"]):
        raise ValueError("Live NeMo resource identity differs from Generator sampling configuration")
    return profile["environment_version"]
