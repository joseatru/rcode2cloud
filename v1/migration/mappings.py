"""
migration.mappings
------------------
Loading and schema validation for mappings.yml.

Validation is deliberately eager and noisy: a typo in a `pat_env` key would
otherwise surface as a deep KeyError hours into a run, after issues have
already been written with the wrong authorship.
"""

import sys

import yaml

from pathlib import Path

from .logging_setup import log


# ------------------------------------------------------------------ #
# mappings.yml Schema Validation
# ------------------------------------------------------------------ #

def validate_mappings(mappings) -> list:
    """
    Validate the structure of a loaded mappings.yml.

    Checks that the two project blocks and the user map are present and
    correctly shaped, so a typo surfaces here rather than as a deep KeyError
    partway through a migration.

    Args:
        mappings: Whatever yaml.safe_load() returned for mappings.yml.

    Returns:
        List of human-readable error strings. Empty list means valid.
    """
    errors: list = []

    if not isinstance(mappings, dict):
        return ["mappings.yml must be a YAML mapping at the top level."]

    # ---- Project blocks ---- #
    for section in ("source", "destination"):
        block = mappings.get(section)
        if block is None:
            errors.append(f"missing required section: '{section}'")
            continue
        if not isinstance(block, dict):
            errors.append(f"'{section}' must be a mapping")
            continue

        project_id = block.get("project_id")
        if project_id is None:
            errors.append(f"'{section}.project_id' is required")
        elif not isinstance(project_id, int):
            errors.append(
                f"'{section}.project_id' must be an integer, got "
                f"{type(project_id).__name__}: {project_id!r}"
            )

        if not isinstance(block.get("path", ""), str):
            errors.append(f"'{section}.path' must be a string")

    # ---- User map ---- #
    users = mappings.get("users")
    if users is None:
        errors.append("missing required section: 'users'")
    elif not isinstance(users, dict):
        errors.append("'users' must be a mapping of source_username -> mapping")
    elif not users:
        errors.append("'users' is empty — at least one user mapping is required")
    else:
        for src_username, mapping in users.items():
            where = f"users.{src_username}"
            if not isinstance(mapping, dict):
                errors.append(f"'{where}' must be a mapping")
                continue
            for key in ("dest_username", "pat_env"):
                value = mapping.get(key)
                if not value:
                    errors.append(f"'{where}.{key}' is required")
                elif not isinstance(value, str):
                    errors.append(f"'{where}.{key}' must be a string")

        # A duplicated pat_env means two users would write as the same
        # identity, silently losing authorship for one of them.
        seen_pat_envs: dict = {}
        for src_username, mapping in users.items():
            if not isinstance(mapping, dict):
                continue
            pat_env = mapping.get("pat_env")
            if isinstance(pat_env, str) and pat_env:
                if pat_env in seen_pat_envs:
                    errors.append(
                        f"pat_env '{pat_env}' is reused by "
                        f"'{seen_pat_envs[pat_env]}' and '{src_username}'"
                    )
                else:
                    seen_pat_envs[pat_env] = src_username

    return errors


def load_mappings(path: Path) -> dict:
    """
    Load and validate mappings.yml.

    Args:
        path: Path to the mappings file.

    Returns:
        The validated mappings dict.

    Exits with code 1 if the file is missing, unparseable, or fails schema
    validation — a typo in a pat_env key would otherwise surface as a
    KeyError hours into a run.
    """
    if not path.exists():
        log.error(f"mappings.yml not found at: {path.resolve()}")
        sys.exit(1)

    try:
        with open(path, encoding="utf-8") as f:
            mappings = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        log.error(f"Could not parse {path}: {exc}")
        sys.exit(1)

    errors = validate_mappings(mappings)
    if errors:
        log.error(f"{path} failed validation:")
        for error in errors:
            log.error(f"  - {error}")
        sys.exit(1)

    return mappings

