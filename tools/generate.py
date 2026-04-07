#!/usr/bin/env python3
"""Synology Compose Generator for ScaleTail.

Reads each services/*/compose.yaml and produces a Synology-optimized variant
under synology/services/*/compose.yaml. Transforms kernel-mode Tailscale
settings to userspace mode, rewrites volume paths, parameterizes hardcoded
values, and optionally generates Portainer templates.

Usage:
    python tools/generate.py --all
    python tools/generate.py --services adguardhome,plex --verbose
    python tools/generate.py --all --validate --portainer
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("scaletail-generate")

# ---------------------------------------------------------------------------
# YAML I/O abstraction
# ---------------------------------------------------------------------------

try:
    from ruamel.yaml import YAML as _RuamelYAML

    _YAML_ENGINE = "ruamel"
except ImportError:
    _YAML_ENGINE = "pyyaml"


@dataclass
class TransformContext:
    """Shared configuration passed to every transform function."""

    data_root: str = "/volume1/docker"
    default_puid: int = 1026
    default_pgid: int = 100
    default_tz: str = "UTC"
    portainer_category: str = "scaletail"


@dataclass
class GenerationResult:
    """Outcome of generating a single service."""

    service_name: str
    success: bool
    output_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, preserving comments when ruamel.yaml is available.

    Returns the parsed document as a dict-like object (CommentedMap for
    ruamel, plain dict for PyYAML).

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If the file cannot be parsed.
    """
    if not path.exists():
        raise FileNotFoundError(f"YAML file not found: {path}")

    if _YAML_ENGINE == "ruamel":
        yaml = _RuamelYAML(typ="rt")
        yaml.preserve_quotes = True
        try:
            with open(path, "r") as f:
                doc = yaml.load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse {path}: {e}") from e
    else:
        import yaml

        logger.warning(
            "ruamel.yaml not available, falling back to PyYAML. "
            "Comments will not be preserved in output."
        )
        try:
            with open(path, "r") as f:
                doc = yaml.safe_load(f)
        except Exception as e:
            raise ValueError(f"Failed to parse {path}: {e}") from e

    if doc is None:
        raise ValueError(f"Empty YAML document: {path}")
    return doc


def dump_yaml(doc: dict[str, Any], path: Path) -> None:
    """Write a YAML document to *path*, creating parent directories as needed.

    Uses the same engine that loaded the document so comment preservation
    round-trips correctly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if _YAML_ENGINE == "ruamel":
        yaml = _RuamelYAML(typ="rt")
        yaml.preserve_quotes = True
        yaml.default_flow_style = False
        yaml.best_map_representor = None
        yaml.width = 4096  # Prevent line wrapping of long values (e.g., image digests)
        with open(path, "w") as f:
            yaml.dump(doc, f)
    else:
        import yaml

        with open(path, "w") as f:
            yaml.dump(doc, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Helper: find the tailscale service key name
# ---------------------------------------------------------------------------


def _find_tailscale_service(doc: dict) -> str | None:
    """Find the service key that represents the tailscale sidecar.

    Looks for a service named 'tailscale' or one whose image starts with
    'tailscale/tailscale'.
    """
    services = doc.get("services", {})
    if not services:
        return None

    # Direct name match first
    if "tailscale" in services:
        return "tailscale"

    # Fallback: look for tailscale image
    for svc_name, svc_def in services.items():
        if isinstance(svc_def, dict):
            image = svc_def.get("image", "")
            if isinstance(image, str) and "tailscale/tailscale" in image:
                return svc_name

    return None


# ---------------------------------------------------------------------------
# Helper: process environment entries (both list and dict style)
# ---------------------------------------------------------------------------


def _process_env_entries(
    env: Any,
    key: str,
    match_value: str | None,
    new_value: str,
    *,
    match_any_value: bool = False,
    skip_variable_refs: bool = False,
) -> bool:
    """Process environment entries, handling both list and dict styles.

    For list-style: entries like "KEY=VALUE"
    For dict-style: entries like {KEY: VALUE}

    Args:
        env: The environment list or dict.
        key: The env var name to match (e.g., "TS_USERSPACE").
        match_value: The value to match (e.g., "false"). None means match any.
        new_value: The replacement value.
        match_any_value: If True, match any value for the key.
        skip_variable_refs: If True, skip values containing "${".

    Returns:
        True if a replacement was made.
    """
    if env is None:
        return False

    replaced = False

    if isinstance(env, list):
        for i, entry in enumerate(env):
            entry_str = str(entry)
            if "=" not in entry_str:
                continue
            eq_pos = entry_str.index("=")
            entry_key = entry_str[:eq_pos]
            entry_val = entry_str[eq_pos + 1:]

            if entry_key != key:
                continue

            if skip_variable_refs and "${" in entry_val:
                continue

            if match_any_value or entry_val == match_value:
                env[i] = f"{key}={new_value}"
                replaced = True

    elif isinstance(env, dict):
        if key in env:
            current_val = str(env[key])
            if skip_variable_refs and "${" in current_val:
                return False
            if match_any_value or current_val == match_value:
                env[key] = new_value
                replaced = True

    return replaced


# ---------------------------------------------------------------------------
# Individual transforms
# ---------------------------------------------------------------------------


def set_userspace_true(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Replace TS_USERSPACE=false with TS_USERSPACE=true in the tailscale service."""
    ts_key = _find_tailscale_service(doc)
    if ts_key is None:
        logger.warning(
            "%s: no tailscale sidecar found, skipping set_userspace_true",
            service_name,
        )
        return

    svc = doc["services"][ts_key]
    env = svc.get("environment")
    if env is None:
        logger.warning(
            "%s: tailscale service has no environment block, "
            "TS_USERSPACE not found, may already be set",
            service_name,
        )
        return

    if _process_env_entries(env, "TS_USERSPACE", "false", "true"):
        logger.info("%s: TS_USERSPACE=false -> TS_USERSPACE=true", service_name)
    else:
        logger.warning(
            "%s: TS_USERSPACE not found or not set to false, may already be set",
            service_name,
        )


def remove_devices(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Remove the devices: block from the tailscale service only.

    Leaves devices on other services (e.g., Frigate GPU passthrough) untouched.
    """
    ts_key = _find_tailscale_service(doc)
    if ts_key is None:
        return

    svc = doc["services"][ts_key]
    if "devices" in svc:
        del svc["devices"]
        logger.info("%s: removed devices from tailscale service", service_name)


def remove_cap_add(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Remove the cap_add: block from the tailscale service only."""
    ts_key = _find_tailscale_service(doc)
    if ts_key is None:
        return

    svc = doc["services"][ts_key]
    if "cap_add" in svc:
        del svc["cap_add"]
        logger.info("%s: removed cap_add from tailscale service", service_name)


def remove_sysctls(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Remove the sysctls: block from the tailscale service.

    Used by tailscale-exit-node for ip_forward settings that are not
    available on Synology DSM.
    """
    ts_key = _find_tailscale_service(doc)
    if ts_key is None:
        return

    svc = doc["services"][ts_key]
    if "sysctls" in svc:
        del svc["sysctls"]
        logger.info("%s: removed sysctls from tailscale service", service_name)


def rewrite_volumes(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Rewrite relative volume paths for all services.

    ./foo -> ${DATA_ROOT:-<ctx.data_root>}/${SERVICE}/foo

    Leaves named volumes, host-absolute paths, and tmpfs volumes untouched.
    """
    services = doc.get("services", {})
    if not services:
        return

    data_root_ref = f"${{DATA_ROOT:-{ctx.data_root}}}"

    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        volumes = svc_def.get("volumes")
        if volumes is None:
            continue

        for i, vol in enumerate(volumes):
            # Skip dict-style volume entries (e.g., tmpfs type)
            if isinstance(vol, dict):
                continue

            vol_str = str(vol)

            # Only rewrite relative paths starting with ./
            if not vol_str.startswith("./"):
                continue

            # Split on first colon to separate host path from container path
            # Handle volume strings like ./foo:/bar:ro
            parts = vol_str.split(":")
            host_path = parts[0]  # e.g., ./config
            rest = ":".join(parts[1:])  # e.g., /config or /config:ro

            # Remove leading ./ from host path
            relative_path = host_path[2:]  # e.g., config or ${SERVICE}-data/foo

            new_host = f"{data_root_ref}/${{SERVICE}}/{relative_path}"
            new_vol = f"{new_host}:{rest}"

            volumes[i] = new_vol
            logger.info(
                "%s/%s: volume rewrite %s -> %s",
                service_name,
                svc_name,
                vol_str,
                new_vol,
            )


def parameterize_puid_pgid(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Replace hardcoded PUID=1000 / PGID=1000 with parameterized defaults.

    PUID=1000 -> PUID=${PUID:-<ctx.default_puid>}
    PGID=1000 -> PGID=${PGID:-<ctx.default_pgid>}

    Skips values that are already variable references.
    """
    services = doc.get("services", {})
    if not services:
        return

    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        env = svc_def.get("environment")
        if env is None:
            continue

        if _process_env_entries(
            env, "PUID", "1000", f"${{PUID:-{ctx.default_puid}}}"
        ):
            logger.info(
                "%s/%s: PUID=1000 -> PUID=${PUID:-%d}",
                service_name,
                svc_name,
                ctx.default_puid,
            )

        if _process_env_entries(
            env, "PGID", "1000", f"${{PGID:-{ctx.default_pgid}}}"
        ):
            logger.info(
                "%s/%s: PGID=1000 -> PGID=${PGID:-%d}",
                service_name,
                svc_name,
                ctx.default_pgid,
            )


def parameterize_tz(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Replace hardcoded TZ values with a parameterized default.

    TZ=Europe/Amsterdam -> TZ=${TZ:-<ctx.default_tz>}

    Skips values that are already variable references (contain ${).
    """
    services = doc.get("services", {})
    if not services:
        return

    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        env = svc_def.get("environment")
        if env is None:
            continue

        if _process_env_entries(
            env,
            "TZ",
            None,
            f"${{TZ:-{ctx.default_tz}}}",
            match_any_value=True,
            skip_variable_refs=True,
        ):
            logger.info(
                "%s/%s: TZ parameterized -> TZ=${TZ:-%s}",
                service_name,
                svc_name,
                ctx.default_tz,
            )


def inject_portainer_labels(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Add Portainer stack labels to all non-tailscale services.

    Appends without duplicating if labels already exist on the service.
    """
    services = doc.get("services", {})
    if not services:
        return

    ts_key = _find_tailscale_service(doc)

    label_stack = f"com.portainer.stack=${{SERVICE}}"
    label_category = f"com.portainer.category={ctx.portainer_category}"

    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue

        # Skip the tailscale service
        if svc_name == ts_key:
            continue

        labels = svc_def.get("labels")

        if labels is None:
            svc_def["labels"] = [label_stack, label_category]
            logger.info(
                "%s/%s: injected Portainer labels", service_name, svc_name
            )
        elif isinstance(labels, list):
            # Check for existing labels before appending
            existing = {str(l).split("=")[0] for l in labels if "=" in str(l)}
            if "com.portainer.stack" not in existing:
                labels.append(label_stack)
            if "com.portainer.category" not in existing:
                labels.append(label_category)
            logger.info(
                "%s/%s: appended Portainer labels", service_name, svc_name
            )
        elif isinstance(labels, dict):
            if "com.portainer.stack" not in labels:
                labels["com.portainer.stack"] = "${SERVICE}"
            if "com.portainer.category" not in labels:
                labels["com.portainer.category"] = ctx.portainer_category
            logger.info(
                "%s/%s: appended Portainer labels (dict)", service_name, svc_name
            )


def inject_env_file_shared(doc: dict, service_name: str, ctx: TransformContext) -> None:
    """Add env_file entries to the tailscale service for shared auth key support.

    Ensures the tailscale service has:
        env_file:
          - ../../.env.shared
          - .env
    """
    ts_key = _find_tailscale_service(doc)
    if ts_key is None:
        return

    svc = doc["services"][ts_key]
    desired = ["../../.env.shared", ".env"]

    existing = svc.get("env_file")
    if existing is None:
        svc["env_file"] = list(desired)
        logger.info(
            "%s: injected env_file on tailscale service", service_name
        )
    elif isinstance(existing, list):
        for entry in desired:
            if entry not in [str(e) for e in existing]:
                existing.append(entry)
        logger.info(
            "%s: updated env_file on tailscale service", service_name
        )
    elif isinstance(existing, str):
        # Convert string to list and add our entries
        current = [existing]
        for entry in desired:
            if entry not in current:
                current.append(entry)
        svc["env_file"] = current
        logger.info(
            "%s: converted env_file to list on tailscale service", service_name
        )


# Ordered list of all transforms. Order matters.
TRANSFORMS: list[tuple[str, Any]] = [
    ("set_userspace_true", set_userspace_true),
    ("remove_devices", remove_devices),
    ("remove_cap_add", remove_cap_add),
    ("remove_sysctls", remove_sysctls),
    ("rewrite_volumes", rewrite_volumes),
    ("parameterize_puid_pgid", parameterize_puid_pgid),
    ("parameterize_tz", parameterize_tz),
    ("inject_portainer_labels", inject_portainer_labels),
    ("inject_env_file_shared", inject_env_file_shared),
]


# ---------------------------------------------------------------------------
# .env generation
# ---------------------------------------------------------------------------


def read_env_file(path: Path) -> list[str]:
    """Read an .env file and return its lines verbatim (including comments).

    Returns an empty list if the file does not exist.
    """
    if not path.exists():
        logger.warning(".env file not found: %s", path)
        return []

    with open(path, "r") as f:
        return f.read().splitlines()


def _env_defines_key(lines: list[str], key: str) -> bool:
    """Check if any non-comment line in the env file defines the given key."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(f"{key}="):
            return True
    return False


def generate_env_file(
    original_lines: list[str],
    service_name: str,
    ctx: TransformContext,
) -> list[str]:
    """Produce a Synology .env file by appending defaults to the original.

    Appends a Synology defaults block with PUID, PGID, TZ, DATA_ROOT.
    Comments out defaults that are already defined in the original.
    Adds a comment about the shared auth key source.
    """
    # Comment out the original TS_AUTHKEY= line so it doesn't override
    # the shared key from ../../.env.shared (env_file last-wins semantics).
    output = []
    for line in original_lines:
        stripped = line.strip()
        if not stripped.startswith("#") and stripped.startswith("TS_AUTHKEY="):
            output.append(f"# {line.rstrip()}  # managed via ../../.env.shared")
        else:
            output.append(line)

    # Build the Synology defaults block
    output.append("")
    output.append("# --- Synology Defaults (auto-generated) ---")
    output.append("# Source: shared auth key from ../../.env.shared")
    output.append(
        "# Override TS_AUTHKEY below only if this service needs a different key."
    )
    output.append("# TS_AUTHKEY=")
    output.append("")

    puid_defined = _env_defines_key(original_lines, "PUID")
    pgid_defined = _env_defines_key(original_lines, "PGID")
    tz_defined = _env_defines_key(original_lines, "TZ")

    if puid_defined:
        output.append(f"# PUID={ctx.default_puid}  # already defined above")
    else:
        output.append(f"PUID={ctx.default_puid}")

    if pgid_defined:
        output.append(f"# PGID={ctx.default_pgid}  # already defined above")
    else:
        output.append(f"PGID={ctx.default_pgid}")

    if tz_defined:
        output.append(f"# TZ={ctx.default_tz}  # already defined above")
    else:
        output.append(f"TZ={ctx.default_tz}")

    output.append(f"DATA_ROOT={ctx.data_root}")

    return output


def write_env_file(lines: list[str], path: Path) -> None:
    """Write .env lines to *path*, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines))
        # Ensure trailing newline
        if lines:
            f.write("\n")


# ---------------------------------------------------------------------------
# Portainer template generation
# ---------------------------------------------------------------------------


def _extract_first_paragraph(readme_path: Path) -> str | None:
    """Extract the first non-heading, non-empty paragraph from a README."""
    if not readme_path.exists():
        return None

    lines = readme_path.read_text().splitlines()
    paragraph_lines: list[str] = []
    found_content = False

    for line in lines:
        stripped = line.strip()
        # Skip headings
        if stripped.startswith("#"):
            if found_content:
                break  # We already have content, heading ends paragraph
            continue
        # Skip empty lines before content
        if not stripped:
            if found_content:
                break  # End of first paragraph
            continue
        # Content line
        found_content = True
        paragraph_lines.append(stripped)

    if paragraph_lines:
        return " ".join(paragraph_lines)
    return None


def _extract_image_from_compose(compose_doc: dict) -> str | None:
    """Extract the first non-tailscale service image from compose."""
    services = compose_doc.get("services", {})
    ts_key = _find_tailscale_service(compose_doc)

    for svc_name, svc_def in services.items():
        if svc_name == ts_key:
            continue
        if isinstance(svc_def, dict) and "image" in svc_def:
            return str(svc_def["image"])
    return None


def extract_service_metadata(
    service_dir: Path,
    compose_doc: dict[str, Any],
) -> dict[str, Any]:
    """Extract metadata for a single service for the Portainer template.

    Reads README.md for description, .env for variable definitions,
    and compose for image information.

    Returns a dict conforming to one entry in Portainer's App Template v2.
    """
    service_name = service_dir.name

    # Description from README
    readme_path = service_dir / "README.md"
    description = _extract_first_paragraph(readme_path) or f"{service_name} service"

    # Image from compose or .env
    image = _extract_image_from_compose(compose_doc) or ""

    # Extract env vars from .env
    env_vars: list[dict[str, str]] = []
    env_path = service_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.split("=", 1)[0]
            val = stripped.split("=", 1)[1]
            env_vars.append(
                {"name": key, "label": key, "default": val}
            )

    return {
        "type": 1,
        "title": service_name.replace("-", " ").title(),
        "description": description,
        "categories": ["scaletail"],
        "platform": "linux",
        "logo": "",
        "image": image,
        "repository": {
            "url": "https://github.com/tailscale-dev/ScaleTail",
            "stackfile": f"synology/services/{service_name}/compose.yaml",
        },
        "env": env_vars,
    }


def generate_portainer_templates(
    services: list[tuple[str, Path, dict[str, Any]]],
    output_path: Path,
) -> None:
    """Generate a portainer-templates.json from a list of (name, dir, doc) tuples.

    Writes a JSON file conforming to Portainer App Template v2 schema.
    """
    templates = []
    for name, service_dir, doc in services:
        template = extract_service_metadata(service_dir, doc)
        templates.append(template)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"version": "2", "templates": templates}, f, indent=2)
        f.write("\n")

    logger.info("Wrote Portainer templates to %s (%d services)", output_path, len(templates))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_compose(compose_path: Path) -> tuple[bool, str]:
    """Validate a generated compose file using `docker compose config`.

    Returns (success, output). Requires docker to be installed.
    """
    if not shutil.which("docker"):
        return False, "docker not found, cannot validate"

    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_path), "config"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr
    except subprocess.TimeoutExpired:
        return False, "docker compose config timed out"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def discover_services(services_dir: Path) -> list[str]:
    """Return a sorted list of service directory names that contain a compose.yaml."""
    if not services_dir.is_dir():
        logger.error("Services directory not found: %s", services_dir)
        return []

    services = []
    for child in sorted(services_dir.iterdir()):
        if child.is_dir() and (child / "compose.yaml").exists():
            services.append(child.name)

    logger.info("Discovered %d services", len(services))
    return services


def generate_service(
    service_name: str,
    source_dir: Path,
    output_dir: Path,
    ctx: TransformContext,
    *,
    verbose: bool = False,
) -> GenerationResult:
    """Generate Synology-optimized compose and .env for a single service.

    Steps:
    1. Load source compose.yaml
    2. Apply all transforms in order
    3. Write transformed compose.yaml to output_dir
    4. Generate and write .env file
    5. Return result with any warnings
    """
    result = GenerationResult(service_name=service_name, success=False)
    compose_path = source_dir / service_name / "compose.yaml"

    # Step 1: Load
    if not compose_path.exists():
        result.error = f"compose.yaml missing for {service_name}"
        logger.error(result.error)
        return result

    try:
        doc = load_yaml(compose_path)
    except (FileNotFoundError, ValueError) as e:
        result.error = str(e)
        logger.error("Failed to load %s: %s", compose_path, e)
        return result

    # Step 2: Apply transforms
    for transform_name, transform_fn in TRANSFORMS:
        try:
            transform_fn(doc, service_name, ctx)
            if verbose:
                logger.info("%s: applied %s", service_name, transform_name)
        except Exception as e:
            warning = f"{service_name}: transform {transform_name} failed: {e}"
            result.warnings.append(warning)
            logger.warning(warning)

    # Step 3: Write compose
    out_compose = output_dir / "services" / service_name / "compose.yaml"
    try:
        dump_yaml(doc, out_compose)
        result.output_path = out_compose
        logger.info("Wrote %s", out_compose)
    except Exception as e:
        result.error = f"Failed to write {out_compose}: {e}"
        logger.error(result.error)
        return result

    # Step 4: Generate .env
    original_env_path = source_dir / service_name / ".env"
    original_lines = read_env_file(original_env_path)
    env_lines = generate_env_file(original_lines, service_name, ctx)
    out_env = output_dir / "services" / service_name / ".env"
    try:
        write_env_file(env_lines, out_env)
        logger.info("Wrote %s", out_env)
    except Exception as e:
        warning = f"Failed to write .env for {service_name}: {e}"
        result.warnings.append(warning)
        logger.warning(warning)

    result.success = True
    return result


def generate_env_shared(output_dir: Path) -> None:
    """Create the root-level .env.shared template if it does not exist.

    Writes a placeholder file with TS_AUTHKEY= and instructions.
    Does not overwrite an existing file (operator may have filled in the key).
    """
    shared_path = output_dir / ".env.shared"
    if shared_path.exists():
        logger.info(".env.shared already exists, not overwriting: %s", shared_path)
        return

    shared_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# ScaleTail Synology - Shared Environment\n"
        "# This file is loaded by all services via env_file in compose.yaml.\n"
        "# Set your Tailscale auth key below. All services will use this key\n"
        "# unless overridden in their individual .env file.\n"
        "#\n"
        "# Generate a key at: https://login.tailscale.com/admin/settings/keys\n"
        "# Recommended: reusable + ephemeral key with appropriate tags.\n"
        "\n"
        "TS_AUTHKEY=\n"
    )
    shared_path.write_text(content)
    logger.info("Created %s", shared_path)


def run(
    service_names: list[str],
    source_root: Path,
    output_root: Path,
    ctx: TransformContext,
    *,
    validate: bool = False,
    dry_run: bool = False,
    portainer: bool = False,
    verbose: bool = False,
    clean: bool = False,
) -> int:
    """Main orchestration: generate all requested services and report results.

    Returns exit code: 0 = success, 1 = generation errors, 2 = validation failures.
    """
    if clean and output_root.exists():
        shutil.rmtree(output_root)
        logger.info("Cleaned output directory: %s", output_root)

    if dry_run:
        print(f"Dry run: would generate {len(service_names)} services:")
        for name in service_names:
            compose_path = source_root / name / "compose.yaml"
            status = "OK" if compose_path.exists() else "MISSING"
            print(f"  {name}: {status}")
        return 0

    # Create .env.shared
    generate_env_shared(output_root)

    results: list[GenerationResult] = []
    portainer_data: list[tuple[str, Path, dict[str, Any]]] = []

    for name in service_names:
        print(f"Generating: {name}...")
        result = generate_service(
            name, source_root, output_root, ctx, verbose=verbose
        )
        results.append(result)

        if result.success and portainer and result.output_path:
            # Re-load the generated compose for portainer template
            try:
                generated_doc = load_yaml(result.output_path)
                portainer_data.append(
                    (name, source_root / name, generated_doc)
                )
            except Exception as e:
                logger.warning(
                    "Could not load generated compose for portainer template: %s",
                    e,
                )

    # Portainer templates
    if portainer and portainer_data:
        templates_path = output_root / "portainer-templates.json"
        generate_portainer_templates(portainer_data, templates_path)
        print(f"Generated Portainer templates: {templates_path}")

    # Validation
    validation_failures = 0
    if validate:
        if not shutil.which("docker"):
            print("ERROR: docker not found, cannot validate")
            return 2

        for result in results:
            if result.success and result.output_path:
                ok, output = validate_compose(result.output_path)
                if ok:
                    print(f"  VALID: {result.service_name}")
                else:
                    print(f"  INVALID: {result.service_name}: {output}")
                    validation_failures += 1

    # Summary
    successes = sum(1 for r in results if r.success)
    failures = sum(1 for r in results if not r.success)
    total_warnings = sum(len(r.warnings) for r in results)

    print(f"\nSummary: {successes} succeeded, {failures} failed, {total_warnings} warnings")

    if failures > 0:
        print("\nFailed services:")
        for r in results:
            if not r.success:
                print(f"  {r.service_name}: {r.error}")
        return 1

    if validation_failures > 0:
        return 2

    if total_warnings > 0:
        print("\nWarnings:")
        for r in results:
            for w in r.warnings:
                print(f"  {w}")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the generate CLI."""
    parser = argparse.ArgumentParser(
        prog="generate.py",
        description="Generate Synology-optimized Docker Compose files from ScaleTail services.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate for all services under services/",
    )
    parser.add_argument(
        "--services",
        type=str,
        default="",
        help="Comma-separated list of service names (e.g., adguardhome,plex)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate generated files with `docker compose config`",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be generated without writing files",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="synology",
        help="Override output directory (default: synology/)",
    )
    parser.add_argument(
        "--data-root",
        type=str,
        default="/volume1/docker",
        help="Override DATA_ROOT default (default: /volume1/docker)",
    )
    parser.add_argument(
        "--portainer",
        action="store_true",
        help="Also generate portainer-templates.json",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove output directory before generating",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each transform applied",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Parse args, configure logging, delegate to run().

    Returns:
        Exit code (0 = success, 1 = generation error, 2 = validation failure).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.all and not args.services:
        parser.print_help()
        return 1

    if args.all and args.services:
        parser.error("--all and --services are mutually exclusive")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    # Resolve paths relative to the repo root (parent of tools/)
    repo_root = Path(__file__).resolve().parent.parent
    source_root = repo_root / "services"
    output_root = repo_root / args.output_dir

    ctx = TransformContext(data_root=args.data_root)

    if args.all:
        service_names = discover_services(source_root)
    else:
        service_names = [s.strip() for s in args.services.split(",") if s.strip()]

    return run(
        service_names=service_names,
        source_root=source_root,
        output_root=output_root,
        ctx=ctx,
        validate=args.validate,
        dry_run=args.dry_run,
        portainer=args.portainer,
        verbose=args.verbose,
        clean=args.clean,
    )


if __name__ == "__main__":
    sys.exit(main())
