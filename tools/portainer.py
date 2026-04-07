#!/usr/bin/env python3
"""
Portainer App Templates JSON Generator for ScaleTail Synology.

Reads service directories under services/ and generates a portainer-templates.json
file conforming to Portainer's App Template v2 schema.

Can be run standalone or called as a post-processing step by generate.py.

Usage:
    python tools/portainer.py [--services-dir PATH] [--output PATH] [--repo-url URL]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_REPO_URL = "https://github.com/alarawms/ScaleTail"
DEFAULT_SERVICES_DIR = "services"
DEFAULT_OUTPUT = "synology/portainer-templates.json"

# Standard env vars injected into every template
STANDARD_ENV_VARS: list[dict] = [
    {
        "name": "TS_AUTHKEY",
        "label": "Tailscale Auth Key",
        "description": "Generate at https://login.tailscale.com/admin/settings/keys",
    },
    {
        "name": "DATA_ROOT",
        "label": "Data Root Path",
        "default": "/volume1/docker",
    },
    {
        "name": "PUID",
        "label": "User ID",
        "default": "1026",
    },
    {
        "name": "PGID",
        "label": "Group ID",
        "default": "100",
    },
    {
        "name": "TZ",
        "label": "Timezone",
        "default": "UTC",
    },
]

# Names of the standard vars for dedup
STANDARD_ENV_NAMES: set[str] = {v["name"] for v in STANDARD_ENV_VARS}

# Vars that are internal plumbing and should not appear in templates
SKIP_ENV_VARS: set[str] = {
    "SERVICE",
    "TS_AUTHKEY",  # already in standard vars
    "DNS_SERVER",
    "SERVICEPORT",
    "COMPOSE_PROJECT_NAME",
}

# ---------------------------------------------------------------------------
# Title casing
# ---------------------------------------------------------------------------

# Lookup table for services with non-obvious casing
TITLE_OVERRIDES: dict[str, str] = {
    "adguardhome": "AdGuard Home",
    "adguardhome-sync": "AdGuard Home Sync",
    "audiobookshelf": "Audiobookshelf",
    "bazarr": "Bazarr",
    "bentopdf": "BentoPDF",
    "beszel-agent": "Beszel Agent",
    "beszel-hub": "Beszel Hub",
    "booklore": "BookLore",
    "caddy": "Caddy",
    "changedetection": "Changedetection.io",
    "clipcascade": "ClipCascade",
    "coder": "Coder",
    "configarr": "Configarr",
    "convertx": "ConvertX",
    "copyparty": "Copyparty",
    "cyberchef": "CyberChef",
    "ddns-updater": "DDNS Updater",
    "dockhand": "Dockhand",
    "docmost": "Docmost",
    "donetick": "Donetick",
    "dozzle": "Dozzle",
    "dumbdo": "DumbDo",
    "eigenfocus": "Eigenfocus",
    "excalidraw": "Excalidraw",
    "flatnotes": "Flatnotes",
    "forgejo": "Forgejo",
    "formbricks": "Formbricks",
    "fossflow": "FossFLOW",
    "frigate": "Frigate",
    "ghost": "Ghost",
    "gitsave": "GitSave",
    "glance": "Glance",
    "gokapi": "Gokapi",
    "gotify": "Gotify",
    "grampsweb": "Gramps Web",
    "haptic": "Haptic",
    "hemmelig": "Hemmelig",
    "homarr": "Homarr",
    "home-assistant": "Home Assistant",
    "homebox": "Homebox",
    "homepage": "Homepage",
    "hytale": "Hytale",
    "immich": "Immich",
    "isley": "Isley",
    "it-tools": "IT-Tools",
    "jellyfin": "Jellyfin",
    "kaneo": "Kaneo",
    "karakeep": "Karakeep",
    "kavita": "Kavita",
    "languagetool": "LanguageTool",
    "linkding": "Linkding",
    "lube-logger": "LubeLogger",
    "mattermost": "Mattermost",
    "mealie": "Mealie",
    "memos": "Memos",
    "metube": "MeTube",
    "miniflux": "Miniflux",
    "miniqr": "Mini-QR",
    "nanote": "Nanote",
    "navidrome": "Navidrome",
    "nessus": "Nessus",
    "netbox": "NetBox",
    "nextcloud": "Nextcloud",
    "nodered": "Node-RED",
    "ntfy": "ntfy",
    "picard": "Picard",
    "pihole": "Pi-hole",
    "pingvin-share": "Pingvin Share",
    "plex": "Plex",
    "pocket-id": "Pocket ID",
    "portainer": "Portainer",
    "portracker": "Portracker",
    "posterizarr": "Posterizarr",
    "prowlarr": "Prowlarr",
    "qbittorrent": "qBittorrent",
    "radarr": "Radarr",
    "recyclarr": "Recyclarr",
    "resilio-sync": "Resilio Sync",
    "searxng": "SearXNG",
    "seerr": "Seerr",
    "slink": "Slink",
    "sonarr": "Sonarr",
    "speedtest-tracker": "Speedtest Tracker",
    "stirlingpdf": "Stirling-PDF",
    "subtrackr": "Subtrackr",
    "swingmx": "Swing Music",
    "tailscale-exit-node": "Tailscale Exit Node",
    "tandoor": "Tandoor Recipes",
    "tautulli": "Tautulli",
    "technitium": "Technitium DNS",
    "tracktor": "Tracktor",
    "traefik": "Traefik",
    "uptime-kuma": "Uptime Kuma",
    "vaultwarden": "Vaultwarden",
    "wallos": "Wallos",
    "actual-budget": "Actual Budget",
    "anchor": "Anchor",
    "arcane": "Arcane",
}


def title_case_service(name: str) -> str:
    """Convert a service directory name to a human-readable title.

    Uses a lookup table for services with non-obvious casing, falls back
    to replacing hyphens with spaces and title-casing each word.
    """
    if name in TITLE_OVERRIDES:
        return TITLE_OVERRIDES[name]
    return name.replace("-", " ").title()


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------

CATEGORY_MAP: dict[str, list[str]] = {
    # Networking and Security
    "adguardhome": ["Networking"],
    "adguardhome-sync": ["Networking"],
    "caddy": ["Networking"],
    "ddns-updater": ["Networking"],
    "nessus": ["Networking", "Security"],
    "netbox": ["Networking"],
    "pihole": ["Networking"],
    "pocket-id": ["Networking", "Security"],
    "technitium": ["Networking"],
    "traefik": ["Networking"],
    "tailscale-exit-node": ["Networking"],
    # Media and Entertainment
    "audiobookshelf": ["Media"],
    "bazarr": ["Media"],
    "booklore": ["Media"],
    "frigate": ["Media", "Smart Home"],
    "hytale": ["Media", "Gaming"],
    "immich": ["Media", "Photos"],
    "jellyfin": ["Media"],
    "kavita": ["Media"],
    "miniflux": ["Media"],
    "navidrome": ["Media", "Music"],
    "swingmx": ["Media", "Music"],
    "seerr": ["Media"],
    "picard": ["Media", "Music"],
    "plex": ["Media"],
    "qbittorrent": ["Media", "Downloads"],
    "prowlarr": ["Media"],
    "radarr": ["Media"],
    "sonarr": ["Media"],
    "slink": ["Media"],
    "tautulli": ["Media", "Monitoring"],
    "metube": ["Media"],
    "configarr": ["Media"],
    "posterizarr": ["Media"],
    "recyclarr": ["Media"],
    # Productivity and Collaboration
    "actual-budget": ["Productivity", "Finance"],
    "anchor": ["Productivity"],
    "clipcascade": ["Productivity"],
    "copyparty": ["Productivity"],
    "donetick": ["Productivity"],
    "docmost": ["Productivity"],
    "dumbdo": ["Productivity"],
    "eigenfocus": ["Productivity"],
    "excalidraw": ["Productivity"],
    "flatnotes": ["Productivity"],
    "forgejo": ["Development Tools"],
    "formbricks": ["Productivity"],
    "ghost": ["Productivity"],
    "grampsweb": ["Productivity"],
    "haptic": ["Productivity"],
    "isley": ["Productivity"],
    "karakeep": ["Productivity"],
    "kaneo": ["Productivity"],
    "languagetool": ["Productivity"],
    "linkding": ["Productivity"],
    "mattermost": ["Productivity"],
    "memos": ["Productivity"],
    "nanote": ["Productivity"],
    "nextcloud": ["Productivity"],
    "pingvin-share": ["Productivity"],
    "resilio-sync": ["Productivity"],
    "stirlingpdf": ["Productivity"],
    "bentopdf": ["Productivity"],
    "subtrackr": ["Productivity", "Finance"],
    "vaultwarden": ["Productivity", "Security"],
    "wallos": ["Productivity", "Finance"],
    # Dashboards and Visualization
    "glance": ["Dashboard"],
    "homepage": ["Dashboard"],
    # Development Tools
    "arcane": ["Development Tools"],
    "changedetection": ["Development Tools"],
    "coder": ["Development Tools"],
    "cyberchef": ["Development Tools"],
    "dockhand": ["Development Tools"],
    "dozzle": ["Development Tools"],
    "fossflow": ["Development Tools"],
    "gitsave": ["Development Tools"],
    "gokapi": ["Development Tools"],
    "homarr": ["Development Tools", "Dashboard"],
    "it-tools": ["Development Tools"],
    "nodered": ["Development Tools"],
    "portainer": ["Development Tools"],
    "searxng": ["Development Tools"],
    # Monitoring and Analytics
    "beszel-agent": ["Monitoring"],
    "beszel-hub": ["Monitoring"],
    "portracker": ["Monitoring"],
    "speedtest-tracker": ["Monitoring"],
    "uptime-kuma": ["Monitoring"],
    # Smart Home
    "home-assistant": ["Smart Home"],
    # Utilities
    "convertx": ["Utilities"],
    "gotify": ["Utilities", "Notifications"],
    "ntfy": ["Utilities", "Notifications"],
    "lube-logger": ["Utilities"],
    "tracktor": ["Utilities"],
    "miniqr": ["Utilities"],
    "hemmelig": ["Utilities", "Security"],
    "homebox": ["Utilities"],
    # Food & Wellness
    "mealie": ["Food & Wellness"],
    "tandoor": ["Food & Wellness"],
}


def categorize_service(name: str, readme_text: str) -> list[str]:
    """Map a service name to Portainer categories.

    Uses the static CATEGORY_MAP derived from the ScaleTail README categories.
    Always includes 'scaletail' as a category.

    Args:
        name: Service directory name (e.g. 'adguardhome').
        readme_text: Contents of the service README.md (currently unused but
                     available for future keyword-based categorisation).

    Returns:
        List of category strings, always ending with 'scaletail'.
    """
    categories = list(CATEGORY_MAP.get(name, []))
    categories.append("scaletail")
    return categories


# ---------------------------------------------------------------------------
# Description extraction
# ---------------------------------------------------------------------------

def extract_description(readme_path: Path) -> str:
    """Extract the first meaningful paragraph from a service README.

    Reads the README, skips the title line (starting with #), skips blank
    lines, then returns the first non-empty paragraph.
    """
    if not readme_path.is_file():
        return ""

    try:
        text = readme_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", readme_path, exc)
        return ""

    lines = text.splitlines()
    # Skip leading title lines and blank lines
    idx = 0
    # Skip the first heading
    while idx < len(lines) and (lines[idx].startswith("#") or not lines[idx].strip()):
        idx += 1

    # Collect the first paragraph (consecutive non-blank, non-heading lines)
    paragraph_lines: list[str] = []
    while idx < len(lines) and lines[idx].strip() and not lines[idx].startswith("#"):
        paragraph_lines.append(lines[idx].strip())
        idx += 1

    if not paragraph_lines:
        return ""

    paragraph = " ".join(paragraph_lines)
    # Strip markdown links: [text](url) -> text
    paragraph = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", paragraph)
    return paragraph


# ---------------------------------------------------------------------------
# Env var extraction
# ---------------------------------------------------------------------------

def parse_env_file(env_path: Path) -> list[dict]:
    """Parse a .env file and return additional env var entries for the template.

    Skips vars that are in STANDARD_ENV_NAMES or SKIP_ENV_VARS.
    Skips comment-only lines (starting with #) and version/URL metadata.
    """
    if not env_path.is_file():
        return []

    extra_vars: list[dict] = []
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read %s: %s", env_path, exc)
        return []

    for line in text.splitlines():
        stripped = line.strip()
        # Skip blanks, comments, metadata
        if not stripped or stripped.startswith("#"):
            continue

        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)", stripped)
        if not match:
            continue

        var_name = match.group(1)
        var_value = match.group(2).strip()

        # Skip standard and internal vars
        if var_name in STANDARD_ENV_NAMES or var_name in SKIP_ENV_VARS:
            continue

        # Skip IMAGE_URL -- it's build metadata, not a runtime config
        if var_name == "IMAGE_URL":
            continue

        entry: dict = {
            "name": var_name,
            "label": var_name.replace("_", " ").title(),
        }
        if var_value:
            entry["default"] = var_value

        extra_vars.append(entry)

    return extra_vars


# ---------------------------------------------------------------------------
# Service metadata extraction
# ---------------------------------------------------------------------------

def extract_service_metadata(service_dir: Path, repo_url: str = DEFAULT_REPO_URL) -> dict | None:
    """Read a single service directory and return a Portainer App Template v2 entry.

    Args:
        service_dir: Path to the service directory (e.g. services/adguardhome).
        repo_url: GitHub repository URL for the stackfile reference.

    Returns:
        A dict conforming to Portainer template v2, or None if the service
        directory does not contain a compose.yaml.
    """
    compose_path = service_dir / "compose.yaml"
    if not compose_path.is_file():
        logger.error("No compose.yaml found in %s, skipping.", service_dir)
        return None

    name = service_dir.name
    title = title_case_service(name)

    # Description
    readme_path = service_dir / "README.md"
    description = extract_description(readme_path)
    if not description:
        description = title
        logger.info("No README description for %s, using title.", name)

    # Categories
    readme_text = ""
    if readme_path.is_file():
        try:
            readme_text = readme_path.read_text(encoding="utf-8")
        except OSError:
            pass
    categories = categorize_service(name, readme_text)

    # Env vars: standard + service-specific
    env_path = service_dir / ".env"
    extra_vars = parse_env_file(env_path)
    env_vars = list(STANDARD_ENV_VARS) + extra_vars

    # Stackfile path relative to repo root
    stackfile = f"synology/services/{name}/compose.yaml"

    template: dict = {
        "type": 3,
        "title": title,
        "description": description,
        "categories": categories,
        "platform": "linux",
        "logo": "",
        "repository": {
            "url": repo_url,
            "stackfile": stackfile,
        },
        "env": env_vars,
    }

    logger.info("Extracted template for %s (%d env vars).", title, len(env_vars))
    return template


# ---------------------------------------------------------------------------
# Template generation
# ---------------------------------------------------------------------------

def generate_templates(
    services_dir: Path,
    repo_url: str = DEFAULT_REPO_URL,
    output_path: Path | None = None,
) -> None:
    """Iterate all service directories, extract metadata, and write the JSON file.

    Args:
        services_dir: Path to the services directory containing subdirectories.
        repo_url: GitHub repository URL.
        output_path: Where to write the resulting JSON file.
    """
    if output_path is None:
        output_path = services_dir.parent / "synology" / "portainer-templates.json"

    templates: list[dict] = []
    errors: list[str] = []

    # Sort for deterministic output
    service_dirs = sorted(
        [d for d in services_dir.iterdir() if d.is_dir()],
        key=lambda p: p.name,
    )

    if not service_dirs:
        logger.error("No service directories found in %s", services_dir)
        sys.exit(1)

    for svc_dir in service_dirs:
        try:
            template = extract_service_metadata(svc_dir, repo_url=repo_url)
            if template is not None:
                templates.append(template)
        except Exception as exc:
            logger.error("Failed to process %s: %s", svc_dir.name, exc)
            errors.append(svc_dir.name)

    # Wrap in v2 envelope
    output_doc = {
        "version": "2",
        "templates": templates,
    }

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(output_doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")  # trailing newline

    logger.info(
        "Wrote %d templates to %s (%d errors).",
        len(templates),
        output_path,
        len(errors),
    )

    if errors:
        logger.error("Services with errors: %s", ", ".join(errors))
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Portainer App Templates JSON from ScaleTail service directories.",
    )
    parser.add_argument(
        "--services-dir",
        type=Path,
        default=Path(DEFAULT_SERVICES_DIR),
        help=f"Path to services directory (default: {DEFAULT_SERVICES_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Output JSON file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--repo-url",
        type=str,
        default=DEFAULT_REPO_URL,
        help=f"GitHub repository URL (default: {DEFAULT_REPO_URL})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging.",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    services_dir = args.services_dir.resolve()
    if not services_dir.is_dir():
        logger.error("Services directory does not exist: %s", services_dir)
        sys.exit(1)

    output_path = args.output
    if output_path is not None:
        output_path = output_path.resolve()

    generate_templates(
        services_dir=services_dir,
        repo_url=args.repo_url,
        output_path=output_path,
    )


if __name__ == "__main__":
    main()
