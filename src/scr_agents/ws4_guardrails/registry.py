"""Approved registry: prompts and skills load only if their hash matches a signed entry.

Manifest (YAML)::

  entries:
    subagent_bugs:
      path: subagent_bugs.md        # relative to the manifest file
      sha256: ...
      approved: true
      version: "1"
      variant: A                    # for A/B testing
      description: ...

Human verdicts feed back into per-prompt outcome statistics (the dashed feedback loop).
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from scr_agents.models import now_iso


class RegistryError(RuntimeError):
    """Raised when a prompt is not registered, missing, altered or not approved."""

    pass


@dataclass
class RegistryEntry:
    """One manifest entry: a prompt file, its signed hash, approval and A/B variant."""

    name: str
    path: str
    sha256: str
    approved: bool = False
    version: str = "1"
    variant: str = "A"
    description: str = ""


def sha256_of(path: Path) -> str:
    """Return the SHA-256 hex digest of the file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PromptRegistry:
    """Load approved prompts by name and record how their findings fare with humans.

    A prompt loads only when its file's hash matches the signed manifest entry and,
    with ``require_approved``, the entry is approved. ``sign`` adds or updates entries.
    """

    def __init__(
        self,
        manifest: Path,
        require_approved: bool = True,
        stats_path: Path | None = None,
    ):
        """Read the manifest.

        Args:
            manifest: Path to the manifest YAML; a missing file means no entries.
            require_approved: Refuse prompts whose entry is not approved.
            stats_path: JSON-lines file for the human verdict outcomes; ``None``
                turns outcome recording off.
        """
        self.manifest = Path(manifest)
        self.require_approved = require_approved
        self.stats_path = stats_path
        self._lock = threading.Lock()
        self._entries = self._read()

    def _read(self) -> dict[str, RegistryEntry]:
        if not self.manifest.exists():
            return {}
        data = yaml.safe_load(self.manifest.read_text()) or {}
        out: dict[str, RegistryEntry] = {}
        for name, e in (data.get("entries") or {}).items():
            out[name] = RegistryEntry(
                name=name,
                path=e["path"],
                sha256=e["sha256"],
                approved=bool(e.get("approved", False)),
                version=str(e.get("version", "1")),
                variant=str(e.get("variant", "A")),
                description=e.get("description", ""),
            )
        return out

    def _write(self) -> None:
        data = {
            "entries": {
                n: {k: v for k, v in asdict(e).items() if k != "name"}
                for n, e in sorted(self._entries.items())
            }
        }
        self.manifest.write_text(yaml.safe_dump(data, sort_keys=False))

    def entries(self) -> list[RegistryEntry]:
        """Return every manifest entry, approved or not."""
        return list(self._entries.values())

    def resolve_path(self, entry: RegistryEntry) -> Path:
        """Resolve the entry's path, relative to the manifest's directory."""
        p = Path(entry.path)
        return p if p.is_absolute() else self.manifest.parent / p

    def load(self, name: str) -> str:
        """Return the text of an approved prompt whose file still matches its hash.

        Raises:
            RegistryError: If the prompt is not registered, its file is missing, its
                hash differs from the manifest, or it is not approved while approval
                is required.
        """
        entry = self._entries.get(name)
        if entry is None:
            raise RegistryError(
                f"prompt '{name}' is not in the approved registry {self.manifest}"
            )
        path = self.resolve_path(entry)
        if not path.exists():
            raise RegistryError(f"prompt '{name}' file missing: {path}")
        actual = sha256_of(path)
        if actual != entry.sha256:
            raise RegistryError(
                f"prompt '{name}' hash mismatch (expected {entry.sha256[:12]}, "
                f"got {actual[:12]});"
                " re-vet it and run `scr-agents registry sign`"
            )
        if self.require_approved and not entry.approved:
            raise RegistryError(f"prompt '{name}' is registered but not approved")
        return path.read_text()

    def sign(
        self,
        name: str,
        path: Path,
        approve: bool = True,
        version: str = "1",
        variant: str = "A",
        description: str = "",
    ) -> RegistryEntry:
        """Record the current hash of ``path`` as entry ``name`` and save the manifest.

        The path is stored relative to the manifest's directory when it lies inside it.

        Args:
            name: Registry key the prompt is loaded by.
            path: The prompt file.
            approve: Mark the entry approved.
            version: Version label of the prompt.
            variant: A/B variant label the outcome statistics are grouped by.
            description: Free-text description.

        Returns:
            The new entry.
        """
        path = Path(path)
        try:
            rel = str(path.resolve().relative_to(self.manifest.parent.resolve()))
        except ValueError:
            rel = str(path.resolve())
        entry = RegistryEntry(
            name=name,
            path=rel,
            sha256=sha256_of(path),
            approved=approve,
            version=version,
            variant=variant,
            description=description,
        )
        with self._lock:
            self._entries[name] = entry
            self._write()
        return entry

    def record_outcome(
        self, name: str, accepted: bool, run_id: str, finding_id: str = ""
    ) -> None:
        """Append a human verdict on a finding to the prompt's outcome statistics.

        Does nothing without a ``stats_path``.

        Args:
            name: The prompt credited with the finding.
            accepted: Whether the human accepted the finding.
            run_id: The run the finding belongs to.
            finding_id: The finding the verdict is about.
        """
        if not self.stats_path:
            return
        entry = self._entries.get(name)
        rec = {
            "ts": now_iso(),
            "prompt": name,
            "variant": entry.variant if entry else None,
            "version": entry.version if entry else None,
            "accepted": accepted,
            "run_id": run_id,
            "finding_id": finding_id,
        }
        with self._lock:
            self.stats_path.parent.mkdir(parents=True, exist_ok=True)
            with self.stats_path.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")

    def stats(self) -> dict[str, dict[str, int]]:
        """Count accepted and rejected verdicts per ``prompt@variant``.

        A verdict recorded again for the same run and finding replaces the earlier one.

        Returns:
            ``{"<prompt>@<variant>": {"accepted": n, "rejected": n}}``; empty when no
            outcomes are recorded.
        """
        out: dict[str, dict[str, int]] = {}
        if not self.stats_path or not self.stats_path.exists():
            return out
        latest: dict[
            tuple[str, str, str], dict[str, Any]
        ] = {}  # a re-recorded verdict replaces the earlier one
        for line in self.stats_path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            latest[
                (
                    f"{rec['prompt']}@{rec.get('variant')}",
                    rec.get("run_id", ""),
                    rec.get("finding_id", ""),
                )
            ] = rec
        for (key, _run, _fid), rec in latest.items():
            bucket = out.setdefault(key, {"accepted": 0, "rejected": 0})
            bucket["accepted" if rec["accepted"] else "rejected"] += 1
        return out
