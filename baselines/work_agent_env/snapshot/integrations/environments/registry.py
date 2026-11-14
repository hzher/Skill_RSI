"""Explicit environment plugin registry; task data cannot import arbitrary code."""
class EnvironmentRegistry:
    def __init__(self):
        self._factories = {}

    def register(self, name, factory):
        if not isinstance(name, str) or not name or name in self._factories or not callable(factory):
            raise ValueError("Duplicate or invalid environment plugin")
        self._factories[name] = factory

    def create(self, name, **options):
        if name not in self._factories:
            raise ValueError("Unknown environment plugin")
        return self._factories[name](**options)


def default_environment_registry():
    from .docker import DockerEnvironment
    registry = EnvironmentRegistry()
    registry.register("docker", DockerEnvironment)
    return registry
