"""Node identity: the machine's own hostname, and the display name shown in
its place.

Two names, two jobs. `hostname()` is what the machine is really called; the
cluster code uses it to recognise its own entry in a host list and fabtest
uses it to decide whether a command runs locally or over ssh, so it is never
overridden. `display_name()` is what the screens, `--json` and `--serve`'s
payload show; it is the hostname unless the operator chose another name with
`--node-name` or $SPARK_SMI_NODE_NAME (issue #9), for example to keep real
hostnames out of a shared screenshot.
"""
import os
import platform
import re

ENV_VAR = "SPARK_SMI_NODE_NAME"

# One hostname label (RFC 1123 letters, digits and hyphens, at most 63
# characters) plus underscore. No dots: the cluster code shortens names at
# the first dot, so "a.b" would show as "a" on every other node. No spaces
# or control characters: the name is drawn in a terminal and sent as JSON.
_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?\Z")

_override = None


def hostname():
    """The machine's short hostname (up to the first dot), never overridden."""
    try:
        name = platform.node()
    except Exception:
        name = ""
    return (name or "localhost").split(".")[0]


def validate(value):
    """Return `value` stripped if it is a valid display name, else raise
    ValueError saying what is allowed."""
    text = (value or "").strip()
    if not _PATTERN.match(text):
        raise ValueError(
            f"invalid node name {value!r}: use 1-63 letters, digits, '-' or '_', "
            "starting and ending with a letter or digit")
    return text


def set_display_name(value):
    """Set the override after validating it; None or "" clears it."""
    global _override
    _override = validate(value) if value not in (None, "") else None


def resolve(cli_value, environ=None):
    """The override to use: `--node-name` wins over $SPARK_SMI_NODE_NAME;
    None when neither is set. Raises ValueError for an invalid value, so a
    typo fails loudly instead of quietly showing the real hostname."""
    env = os.environ if environ is None else environ
    if cli_value is not None:
        return validate(cli_value)
    raw = env.get(ENV_VAR)
    if raw is None or raw.strip() == "":
        return None
    return validate(raw)


def is_overridden():
    return _override is not None


def display_name(fallback=None):
    """The override when one is set, else `fallback` (when given), else the
    short hostname."""
    if _override is not None:
        return _override
    return fallback if fallback is not None else hostname()
