#!/usr/bin/env python3
"""Move one checkout from its v1 per-repo constitution to constitution 2.0.0.

Offline and in place, like convert_repo.py. It:

1. reads the manifest's `Profile:` line and writes the matching
   `[tool.sdlc] repo-kinds` into pyproject.toml (or sdlc.toml without one);
2. drafts the README section "What's different here" from the manifest's own
   sections, dropping `History` tables (CHANGELOG and git hold them);
3. deletes `.specify/memory/constitution.md`;
4. runs the v2 check on the result.

The draft is mechanical on purpose. Condensing it, and moving long design
material to docs/internal/, is an editor's job; this prints what it left for one.

    scripts/constitution_v2.py REPO            # show what it would do
    scripts/constitution_v2.py REPO --apply    # do it

Exit: 0 done and v2-clean, 1 done with v2 findings, 2 refused (nothing written).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# convert_repo.py beside this script, by path: not an installed package.
_CONVERT = importlib.util.spec_from_file_location("convert_repo", HERE / "convert_repo.py")
if _CONVERT is None or _CONVERT.loader is None:
    raise ImportError("scripts/convert_repo.py is missing beside constitution_v2.py")
convert_repo = importlib.util.module_from_spec(_CONVERT)
sys.modules["convert_repo"] = convert_repo
_CONVERT.loader.exec_module(convert_repo)
config_file, with_keys = convert_repo.config_file, convert_repo.with_keys

# The v2 checks themselves, from this tree's sdlc_jobs, so the converter and the
# gate can never disagree about what passes.
sys.path.insert(0, str(HERE.parent))
try:
    constitution = importlib.import_module("sdlc_jobs.constitution")
finally:
    sys.path.remove(str(HERE.parent))

MANIFEST = Path(".specify/memory/constitution.md")
PROFILE_KINDS = {
    "MCP Server": "mcp-server",
    "Forked MCP Server": "forked-mcp-server",
    "CLI Tool": "cli-tool",
    "Host Tool": "host-tool",
    "Web Application": "web-application",
    "Container Image": "container-image",
    "Bootc Image": "bootc-image",
    "Autonomous Agent": "autonomous-agent",
    "Security Gateway": "security-gateway",
    "Workflow Automation": "workflow-automation",
    "Claude Skill": "claude-skill",
    "Host Config": "host-config",
    "Package Repository": "package-repository",
    "Data Archive": "data-archive",
    "Governance": "governance",
}
DROPPED_SECTIONS = re.compile(r"^(history|ratification history)$", re.IGNORECASE)
NOTE = "# What this repository is, for constitution 2.0.0 (section K).\n"


class RefusedError(Exception):
    """Nothing was written."""


def kinds_of(manifest: str) -> list[str]:
    found = re.search(r"^>\s*\*\*Profile:\*\*\s*(.+?)\s*$", manifest, re.M)
    if not found:
        raise RefusedError("the manifest names no Profile")
    names = [p.strip() for p in found.group(1).split(",") if p.strip()]
    unknown = [n for n in names if n not in PROFILE_KINDS]
    if unknown:
        raise RefusedError(f"no kind for profile(s) {', '.join(unknown)}")
    return [PROFILE_KINDS[n] for n in names]


def sections(manifest: str) -> list[tuple[str, str]]:
    """Each `## ` section after the header block, as (title, body)."""
    parts = re.split(r"^## +(.+?)\s*$", manifest, flags=re.M)
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]


def draft_section(manifest: str) -> str:
    kept = [(t, b) for t, b in sections(manifest) if not DROPPED_SECTIONS.match(t) and b]
    if not kept:
        return ""
    out = [f"## {constitution.SECTION_TITLE}\n"]
    for title, body in kept:
        out.append(f"### {title}\n\n{body}\n")
    return "\n".join(out)


def with_section(readme: str, section: str) -> str:
    if not section or constitution.readme_section(readme):
        return readme
    # After the README's opening (title and first paragraph), before its first `## `.
    first = re.search(r"^## ", readme, re.M)
    if first is None:
        return readme.rstrip("\n") + "\n\n" + section
    return readme[: first.start()] + section + "\n" + readme[first.start() :]


def plan(repo: Path) -> dict[Path, str | None]:
    manifest_path = repo / MANIFEST
    if not manifest_path.is_file():
        raise RefusedError(f"no {MANIFEST}: already on v2, or never on v1")
    manifest = manifest_path.read_text(encoding="utf-8")
    cfg = config_file(repo)
    cfg_text = cfg.read_text(encoding="utf-8") if cfg.is_file() else ""
    if re.search(r"^repo-kinds\s*=", cfg_text, re.M):
        raise RefusedError(f"{cfg.name} already sets repo-kinds")
    readme_path = repo / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
    return {
        cfg: with_keys(cfg_text, "tool.sdlc", [("repo-kinds", kinds_of(manifest))], NOTE),
        readme_path: with_section(readme, draft_section(manifest)),
        manifest_path: None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        planned = plan(args.repo)
    except RefusedError as err:
        print(f"refused: {err}", file=sys.stderr)
        return 2
    for path, text in planned.items():
        print(f"{'delete' if text is None else 'write '} {path.relative_to(args.repo)}")
    if not args.apply:
        return 0
    for path, text in planned.items():
        if text is None:
            path.unlink()
            for parent in (path.parent, path.parent.parent):
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
        else:
            path.write_text(text, encoding="utf-8")
    section = constitution.readme_section((args.repo / "README.md").read_text(encoding="utf-8"))
    print(f"README section: {len(section.splitlines())} lines (an editor condenses it)")
    report = constitution.check(args.repo)
    for finding in report.findings:
        print(f"FAIL {finding}")
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
