"""Tiny parameter schema: the subset task.toml needs, nothing more.

A param spec looks like:
    [params.since_hours]
    type = "integer"      # string | integer | number | boolean
    minimum = 1
    maximum = 72
    default = 24          # params with a default are optional
    enum = [...]
    max_length = 4000     # strings
    min_length = 1
    pattern = "^[a-z]+$"
    required = true       # implied when there is no default
    description = "..."
"""
import re

TYPES = {"string", "integer", "number", "boolean"}
SPEC_KEYS = {"type", "minimum", "maximum", "default", "enum", "max_length",
             "min_length", "pattern", "required", "description"}


def _type_of(spec):
    if "type" in spec:
        return spec["type"]
    if "enum" in spec and spec["enum"]:
        v = spec["enum"][0]
        return {bool: "boolean", int: "integer", float: "number"}.get(type(v), "string")
    return "string"


def check_spec(name, spec):
    """Return a list of problems with one param spec (used by lint)."""
    errs = []
    if not isinstance(spec, dict):
        return [f"param {name}: spec must be a table"]
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        errs.append(f"param {name}: unknown keys {sorted(unknown)}")
    t = _type_of(spec)
    if not isinstance(t, str) or t not in TYPES:
        errs.append(f"param {name}: type must be one of {sorted(TYPES)}")
    if "pattern" in spec:
        try:
            re.compile(spec["pattern"])
        except (re.error, TypeError) as e:
            errs.append(f"param {name}: bad pattern ({e})")
    if t == "string" and "max_length" not in spec and "enum" not in spec:
        errs.append(f"param {name}: free-text string needs max_length")
    if "default" in spec:
        _, e = _coerce(name, spec["default"], spec)
        if e:
            errs.append(f"param {name}: default invalid ({e})")
    return errs


def _coerce(name, v, spec):
    t = _type_of(spec)
    if t == "boolean":
        if isinstance(v, bool):
            pass
        elif isinstance(v, str) and v.lower() in ("true", "false", "1", "0", "yes", "no"):
            v = v.lower() in ("true", "1", "yes")
        else:
            return None, "expected boolean"
    elif t == "integer":
        if isinstance(v, bool):
            return None, "expected integer"
        if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
            v = int(v)
        if not isinstance(v, int):
            return None, "expected integer"
    elif t == "number":
        if isinstance(v, bool):
            return None, "expected number"
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                return None, "expected number"
        if not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
            return None, "expected a finite number"
    else:
        if not isinstance(v, str):
            return None, "expected string"
        if "max_length" in spec and len(v) > spec["max_length"]:
            return None, f"longer than {spec['max_length']} chars"
        if "min_length" in spec and len(v) < spec["min_length"]:
            return None, f"shorter than {spec['min_length']} chars"
        if "pattern" in spec and not re.fullmatch(spec["pattern"], v):
            return None, "does not match pattern"
    if "enum" in spec and v not in spec["enum"]:
        return None, f"must be one of {spec['enum']}"
    if t in ("integer", "number"):
        if "minimum" in spec and v < spec["minimum"]:
            return None, f"must be >= {spec['minimum']}"
        if "maximum" in spec and v > spec["maximum"]:
            return None, f"must be <= {spec['maximum']}"
    return v, None


def validate(params, specs):
    """Validate caller params against specs. Returns (clean_params, errors)."""
    params = params or {}
    errs = []
    if not isinstance(params, dict):
        return {}, ["params must be an object"]
    clean = {}
    for k in params:
        if k not in specs:
            errs.append(f"unknown param {k}")
    for name, spec in specs.items():
        if name in params:
            v, e = _coerce(name, params[name], spec)
            if e:
                errs.append(f"{name}: {e}")
            else:
                clean[name] = v
        elif "default" in spec:
            clean[name] = spec["default"]
        elif spec.get("required", True):
            errs.append(f"missing param {name}")
    return clean, errs


def describe(specs):
    """Schema as returned by GET /tasks."""
    out = {}
    for name, spec in specs.items():
        d = {k: v for k, v in spec.items() if k in SPEC_KEYS}
        d["type"] = _type_of(spec)
        d["required"] = "default" not in spec and spec.get("required", True)
        out[name] = d
    return out
