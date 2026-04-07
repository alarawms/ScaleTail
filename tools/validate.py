#!/usr/bin/env python3
"""Validation script for ScaleTail Synology generated output.

Validates all generated compose files under synology/services/ against
Synology compliance, Portainer compliance, and structural correctness rules.

Usage:
    python tools/validate.py
    python tools/validate.py --services-dir synology/services --verbose
    python tools/validate.py --idempotency
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("scaletail-validate")

# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

try:
    from ruamel.yaml import YAML as _RuamelYAML

    def _load_yaml(path: Path) -> dict[str, Any]:
        yaml = _RuamelYAML(typ="safe")
        with open(path) as f:
            return yaml.load(f)

except ImportError:
    import yaml as _pyyaml  # type: ignore[no-redef]

    def _load_yaml(path: Path) -> dict[str, Any]:  # type: ignore[misc]
        with open(path) as f:
            return _pyyaml.safe_load(f)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


@dataclass
class CheckResult:
    """Single check outcome."""

    status: str  # PASS, FAIL, WARN
    message: str


@dataclass
class ServiceValidation:
    """Aggregated results for one service directory."""

    service_name: str
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == PASS)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == FAIL)

    @property
    def warned(self) -> int:
        return sum(1 for c in self.checks if c.status == WARN)


# ---------------------------------------------------------------------------
# Helper: extract env entries from various formats
# ---------------------------------------------------------------------------


def _get_env_entries(env: Any) -> dict[str, str]:
    """Parse environment block (list or dict) into a flat key->value dict."""
    result: dict[str, str] = {}
    if env is None:
        return result

    if isinstance(env, list):
        for entry in env:
            s = str(entry)
            if "=" in s:
                k, v = s.split("=", 1)
                result[k.strip()] = v.strip()
    elif isinstance(env, dict):
        for k, v in env.items():
            result[str(k)] = str(v) if v is not None else ""

    return result


def _get_labels(svc_def: dict) -> list[str]:
    """Extract labels as a list of 'key=value' strings."""
    labels = svc_def.get("labels")
    if labels is None:
        return []
    if isinstance(labels, list):
        return [str(l) for l in labels]
    if isinstance(labels, dict):
        return [f"{k}={v}" for k, v in labels.items()]
    return []


def _get_env_file_entries(svc_def: dict) -> list[str]:
    """Extract env_file entries as a flat list of strings."""
    ef = svc_def.get("env_file")
    if ef is None:
        return []
    if isinstance(ef, str):
        return [ef]
    if isinstance(ef, list):
        return [str(e) for e in ef]
    return []


def _get_devices(svc_def: dict) -> list[str]:
    """Extract devices entries as a flat list of strings."""
    devices = svc_def.get("devices")
    if devices is None:
        return []
    if isinstance(devices, list):
        return [str(d) for d in devices]
    return []


def _get_cap_add(svc_def: dict) -> list[str]:
    """Extract cap_add entries as a flat list of strings."""
    caps = svc_def.get("cap_add")
    if caps is None:
        return []
    if isinstance(caps, list):
        return [str(c) for c in caps]
    return []


def _get_volumes(svc_def: dict) -> list[str]:
    """Extract volume entries as strings, skipping dict-style (tmpfs) entries."""
    vols = svc_def.get("volumes")
    if vols is None:
        return []
    result = []
    for v in vols:
        if isinstance(v, str):
            result.append(v)
        elif isinstance(v, dict):
            # dict-style volumes (tmpfs, etc.) - skip
            pass
    return result


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_compose_exists(service_dir: Path) -> CheckResult:
    compose = service_dir / "compose.yaml"
    if compose.exists():
        return CheckResult(PASS, "compose.yaml exists")
    return CheckResult(FAIL, "compose.yaml missing")


def check_compose_valid_yaml(service_dir: Path) -> CheckResult | None:
    compose = service_dir / "compose.yaml"
    if not compose.exists():
        return None  # already caught by check_compose_exists
    try:
        doc = _load_yaml(compose)
        if doc is None:
            return CheckResult(FAIL, "compose.yaml is empty or null")
        return CheckResult(PASS, "compose.yaml is valid YAML")
    except Exception as e:
        return CheckResult(FAIL, f"compose.yaml is invalid YAML: {e}")


def check_env_exists(service_dir: Path) -> CheckResult:
    env = service_dir / ".env"
    if env.exists():
        return CheckResult(PASS, ".env file exists")
    return CheckResult(FAIL, ".env file missing")


def check_has_services_block(doc: dict | None) -> CheckResult | None:
    if doc is None:
        return None
    if "services" in doc and doc["services"]:
        return CheckResult(PASS, "has services: block")
    return CheckResult(FAIL, "missing or empty services: block")


def check_has_tailscale_service(doc: dict | None) -> CheckResult | None:
    if doc is None:
        return None
    services = doc.get("services", {})
    if not services:
        return None

    # Direct name match
    if "tailscale" in services:
        return CheckResult(PASS, "tailscale service found")

    # Fallback: check image
    for svc_name, svc_def in services.items():
        if isinstance(svc_def, dict):
            image = svc_def.get("image", "")
            if isinstance(image, str) and "tailscale/tailscale" in image:
                return CheckResult(PASS, f"tailscale service found (as '{svc_name}')")

    return CheckResult(WARN, "no tailscale service found (may be intentional)")


def check_no_dev_net_tun(doc: dict | None) -> CheckResult | None:
    """No /dev/net/tun in any devices: block."""
    if doc is None:
        return None
    services = doc.get("services", {})
    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        for device in _get_devices(svc_def):
            if "/dev/net/tun" in device:
                return CheckResult(FAIL, f"/dev/net/tun found in {svc_name} devices")
    return CheckResult(PASS, "no /dev/net/tun in devices")


def check_no_net_admin(doc: dict | None) -> CheckResult | None:
    """No net_admin in any cap_add: block."""
    if doc is None:
        return None
    services = doc.get("services", {})
    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        for cap in _get_cap_add(svc_def):
            if cap.upper() == "NET_ADMIN":
                return CheckResult(
                    FAIL, f"net_admin found in {svc_name} cap_add"
                )
    return CheckResult(PASS, "no net_admin in cap_add")


def _find_tailscale_svc(doc: dict) -> tuple[str | None, dict | None]:
    """Find the tailscale service key and definition."""
    services = doc.get("services", {})
    if "tailscale" in services and isinstance(services["tailscale"], dict):
        return "tailscale", services["tailscale"]
    for svc_name, svc_def in services.items():
        if isinstance(svc_def, dict):
            image = svc_def.get("image", "")
            if isinstance(image, str) and "tailscale/tailscale" in image:
                return svc_name, svc_def
    return None, None


def check_ts_userspace_true(doc: dict | None) -> CheckResult | None:
    """TS_USERSPACE=true must be present in tailscale environment."""
    if doc is None:
        return None
    ts_name, ts_svc = _find_tailscale_svc(doc)
    if ts_svc is None:
        return None  # no tailscale service, caught by other check

    env = _get_env_entries(ts_svc.get("environment"))
    if env.get("TS_USERSPACE") == "true":
        return CheckResult(PASS, "TS_USERSPACE=true present")
    if "TS_USERSPACE" not in env:
        return CheckResult(FAIL, "TS_USERSPACE not set in tailscale environment")
    return CheckResult(FAIL, f"TS_USERSPACE={env['TS_USERSPACE']} (expected true)")


def check_ts_userspace_not_false(doc: dict | None) -> CheckResult | None:
    """TS_USERSPACE=false must NOT be present."""
    if doc is None:
        return None
    ts_name, ts_svc = _find_tailscale_svc(doc)
    if ts_svc is None:
        return None

    env = _get_env_entries(ts_svc.get("environment"))
    if env.get("TS_USERSPACE") == "false":
        return CheckResult(FAIL, "TS_USERSPACE=false found in tailscale environment")
    return CheckResult(PASS, "TS_USERSPACE=false not present")


def check_no_relative_volume_paths(doc: dict | None) -> CheckResult | None:
    """All relative volume paths (./) should use ${DATA_ROOT}."""
    if doc is None:
        return None
    services = doc.get("services", {})
    violations = []
    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        for vol in _get_volumes(svc_def):
            host_part = vol.split(":")[0] if ":" in vol else vol
            if host_part.startswith("./"):
                violations.append(f"{svc_name}: {vol}")

    if violations:
        return CheckResult(
            FAIL,
            f"relative volume paths not rewritten: {', '.join(violations)}",
        )
    return CheckResult(PASS, "no relative volume paths (all use ${{DATA_ROOT}})")


def check_puid_pgid_parameterized(doc: dict | None) -> CheckResult | None:
    """PUID/PGID must not be bare hardcoded 1000."""
    if doc is None:
        return None
    services = doc.get("services", {})
    violations = []
    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        env = _get_env_entries(svc_def.get("environment"))
        for key in ("PUID", "PGID"):
            val = env.get(key)
            if val is not None and val == "1000":
                violations.append(f"{svc_name}: {key}=1000")

    if violations:
        return CheckResult(
            FAIL,
            f"bare PUID/PGID=1000 found: {', '.join(violations)}",
        )
    return CheckResult(PASS, "PUID/PGID properly parameterized")


def check_portainer_label(doc: dict | None) -> CheckResult | None:
    """Non-tailscale services must have com.portainer.stack label."""
    if doc is None:
        return None
    services = doc.get("services", {})
    ts_name, _ = _find_tailscale_svc(doc)
    violations = []

    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        if svc_name == ts_name:
            continue
        labels = _get_labels(svc_def)
        has_stack = any(l.startswith("com.portainer.stack=") for l in labels)
        if not has_stack:
            violations.append(svc_name)

    if violations:
        return CheckResult(
            FAIL,
            f"missing com.portainer.stack label: {', '.join(violations)}",
        )
    return CheckResult(PASS, "all non-tailscale services have com.portainer.stack label")


def check_env_file_shared(doc: dict | None) -> CheckResult | None:
    """Tailscale service env_file must include ../../.env.shared."""
    if doc is None:
        return None
    ts_name, ts_svc = _find_tailscale_svc(doc)
    if ts_svc is None:
        return None

    env_files = _get_env_file_entries(ts_svc)
    if "../../.env.shared" in env_files:
        return CheckResult(PASS, "tailscale env_file includes ../../.env.shared")
    return CheckResult(
        FAIL, "tailscale env_file missing ../../.env.shared"
    )


# ---------------------------------------------------------------------------
# Service-level validation
# ---------------------------------------------------------------------------


def validate_service(service_dir: Path, verbose: bool = False) -> ServiceValidation:
    """Run all checks for a single service directory."""
    name = service_dir.name
    sv = ServiceValidation(service_name=name)

    # Structural checks
    sv.checks.append(check_compose_exists(service_dir))
    r = check_compose_valid_yaml(service_dir)
    if r:
        sv.checks.append(r)
    sv.checks.append(check_env_exists(service_dir))

    # Load YAML for deeper checks
    compose_path = service_dir / "compose.yaml"
    doc: dict | None = None
    if compose_path.exists():
        try:
            doc = _load_yaml(compose_path)
        except Exception:
            doc = None

    r = check_has_services_block(doc)
    if r:
        sv.checks.append(r)

    r = check_has_tailscale_service(doc)
    if r:
        sv.checks.append(r)

    # Synology compliance
    for check_fn in (
        check_no_dev_net_tun,
        check_no_net_admin,
        check_ts_userspace_true,
        check_ts_userspace_not_false,
        check_no_relative_volume_paths,
        check_puid_pgid_parameterized,
    ):
        r = check_fn(doc)
        if r:
            sv.checks.append(r)

    # Portainer compliance
    for check_fn in (check_portainer_label, check_env_file_shared):
        r = check_fn(doc)
        if r:
            sv.checks.append(r)

    return sv


# ---------------------------------------------------------------------------
# Cross-service checks
# ---------------------------------------------------------------------------


def check_env_shared_exists(synology_root: Path) -> CheckResult:
    path = synology_root / ".env.shared"
    if path.exists():
        return CheckResult(PASS, "synology/.env.shared exists")
    return CheckResult(FAIL, "synology/.env.shared missing")


def check_portainer_templates(
    synology_root: Path, service_count: int
) -> list[CheckResult]:
    """Check portainer-templates.json existence, validity, and count."""
    results: list[CheckResult] = []
    path = synology_root / "portainer-templates.json"

    if not path.exists():
        results.append(
            CheckResult(WARN, "synology/portainer-templates.json not found (optional)")
        )
        return results

    try:
        with open(path) as f:
            data = json.load(f)
        results.append(CheckResult(PASS, "portainer-templates.json is valid JSON"))
    except (json.JSONDecodeError, OSError) as e:
        results.append(
            CheckResult(FAIL, f"portainer-templates.json is invalid: {e}")
        )
        return results

    templates = data.get("templates", [])
    template_count = len(templates)
    if template_count == service_count:
        results.append(
            CheckResult(
                PASS,
                f"template count ({template_count}) matches service count ({service_count})",
            )
        )
    else:
        results.append(
            CheckResult(
                WARN,
                f"template count ({template_count}) differs from service count ({service_count})",
            )
        )

    return results


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


def check_idempotency(repo_root: Path, verbose: bool = False) -> list[CheckResult]:
    """Re-run the generator and diff the output."""
    results: list[CheckResult] = []
    generate_script = repo_root / "tools" / "generate.py"

    if not generate_script.exists():
        results.append(CheckResult(WARN, "tools/generate.py not found, skipping idempotency"))
        return results

    try:
        proc = subprocess.run(
            [sys.executable, str(generate_script), "--all", "--portainer"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(repo_root),
        )
        if proc.returncode != 0:
            results.append(
                CheckResult(FAIL, f"generator failed (exit {proc.returncode}): {proc.stderr[:500]}")
            )
            return results
    except subprocess.TimeoutExpired:
        results.append(CheckResult(FAIL, "generator timed out"))
        return results
    except Exception as e:
        results.append(CheckResult(FAIL, f"generator error: {e}"))
        return results

    # Check git diff for changes in synology/
    try:
        diff_proc = subprocess.run(
            ["git", "diff", "--name-only", "synology/"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_root),
        )
        changed = [
            line.strip()
            for line in diff_proc.stdout.splitlines()
            if line.strip()
        ]

        # Also check untracked files
        untracked_proc = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "synology/"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_root),
        )
        untracked = [
            line.strip()
            for line in untracked_proc.stdout.splitlines()
            if line.strip()
        ]

        all_changes = changed + untracked
        if all_changes:
            files_str = ", ".join(all_changes[:10])
            suffix = f" (and {len(all_changes) - 10} more)" if len(all_changes) > 10 else ""
            results.append(
                CheckResult(
                    FAIL,
                    f"idempotency: re-running generator produced differences in: {files_str}{suffix}",
                )
            )
        else:
            results.append(CheckResult(PASS, "idempotency: no differences after re-running generator"))

    except FileNotFoundError:
        # git not available; fall back to a simpler check
        results.append(
            CheckResult(
                WARN, "git not available, cannot verify idempotency via diff"
            )
        )

    return results


# ---------------------------------------------------------------------------
# Main validation orchestration
# ---------------------------------------------------------------------------


def run_validation(
    services_dir: Path,
    synology_root: Path,
    repo_root: Path,
    *,
    idempotency: bool = False,
    verbose: bool = False,
) -> int:
    """Run all validation checks and print results.

    Returns: exit code (0 = all pass, 1 = failures present).
    """
    # Discover service directories
    if not services_dir.is_dir():
        print(f"ERROR: services directory not found: {services_dir}")
        return 1

    service_dirs = sorted(
        d for d in services_dir.iterdir()
        if d.is_dir() and (d / "compose.yaml").exists()
    )

    if not service_dirs:
        print(f"ERROR: no services found in {services_dir}")
        return 1

    # Per-service validation
    all_validations: list[ServiceValidation] = []
    for svc_dir in service_dirs:
        sv = validate_service(svc_dir, verbose=verbose)
        all_validations.append(sv)

    # Cross-service checks
    cross_checks: list[CheckResult] = []
    cross_checks.append(check_env_shared_exists(synology_root))
    cross_checks.extend(check_portainer_templates(synology_root, len(service_dirs)))

    # Idempotency (optional)
    idempotency_checks: list[CheckResult] = []
    if idempotency:
        idempotency_checks = check_idempotency(repo_root, verbose=verbose)

    # --------------- Output ---------------
    total_pass = 0
    total_fail = 0
    total_warn = 0

    for sv in all_validations:
        if sv.failed > 0:
            # Show first failure
            first_fail = next(c for c in sv.checks if c.status == FAIL)
            print(f"[FAIL] {sv.service_name}: {first_fail.message}")
            if verbose:
                for c in sv.checks:
                    marker = {"PASS": " ok ", "FAIL": "FAIL", "WARN": "WARN"}[c.status]
                    print(f"       [{marker}] {c.message}")
        elif sv.warned > 0:
            first_warn = next(c for c in sv.checks if c.status == WARN)
            print(f"[WARN] {sv.service_name}: {first_warn.message}")
            if verbose:
                for c in sv.checks:
                    marker = {"PASS": " ok ", "FAIL": "FAIL", "WARN": "WARN"}[c.status]
                    print(f"       [{marker}] {c.message}")
        else:
            print(f"[PASS] {sv.service_name}: all {sv.passed} checks passed")
            if verbose:
                for c in sv.checks:
                    print(f"       [ ok ] {c.message}")

        total_pass += sv.passed
        total_fail += sv.failed
        total_warn += sv.warned

    # Cross-service
    if cross_checks:
        print()
        print("Cross-service checks:")
        for c in cross_checks:
            tag = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN"}[c.status]
            print(f"  [{tag}] {c.message}")
            if c.status == PASS:
                total_pass += 1
            elif c.status == FAIL:
                total_fail += 1
            else:
                total_warn += 1

    # Idempotency
    if idempotency_checks:
        print()
        print("Idempotency checks:")
        for c in idempotency_checks:
            tag = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN"}[c.status]
            print(f"  [{tag}] {c.message}")
            if c.status == PASS:
                total_pass += 1
            elif c.status == FAIL:
                total_fail += 1
            else:
                total_warn += 1

    # Summary
    print()
    print(f"Summary: {total_pass} passed, {total_fail} failed, {total_warn} warnings")

    return 1 if total_fail > 0 else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="validate.py",
        description="Validate ScaleTail Synology generated compose files.",
    )
    parser.add_argument(
        "--services-dir",
        type=str,
        default="synology/services",
        help="Path to services directory (default: synology/services)",
    )
    parser.add_argument(
        "--idempotency",
        action="store_true",
        help="Re-run the generator and fail if output differs",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show all individual check results per service",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    # Resolve paths relative to repo root
    repo_root = Path(__file__).resolve().parent.parent
    services_dir = repo_root / args.services_dir
    synology_root = services_dir.parent  # e.g., synology/

    return run_validation(
        services_dir=services_dir,
        synology_root=synology_root,
        repo_root=repo_root,
        idempotency=args.idempotency,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
