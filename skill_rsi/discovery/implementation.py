"""Validate the declarative strategies the discovery model is allowed to configure."""


def configuration(candidate):
    value = candidate.get("implementation")
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"kind", "strategy", "defaults"} or value["kind"] != "builtin":
        raise ValueError("implementation must select one configurable built-in strategy")
    strategy, defaults = value["strategy"], value["defaults"]
    if not isinstance(defaults, dict):
        raise ValueError("implementation defaults must be an object")
    if strategy == "constraint-check" and not defaults:
        return value
    if strategy == "tool-result-view" and not defaults.keys() - {"projection", "min_lines", "context_lines"}:
        min_lines = defaults.get("min_lines", 60)
        context_lines = defaults.get("context_lines", 4)
        if (defaults.get("projection", "error_window") in ("fixed", "error_window")
                and type(min_lines) is int and 2 <= min_lines <= 100_000
                and type(context_lines) is int and 0 <= context_lines <= 30):
            return value
    raise ValueError("invalid built-in strategy configuration")
