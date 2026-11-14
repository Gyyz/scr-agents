"""Static scanners: external tools plus a built-in regex scanner.

External tools when installed (ruff, bandit, semgrep, cppcheck, gosec) plus a built-in
regex scanner so there is always some signal. Findings are limited to changed lines by
default.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scr_agents.config import ScannerConfig
from scr_agents.models import ChangedFile, ScannerFinding, ScannerReport, split_lines

Runner = Callable[[Path, list[str], ScannerConfig], list[ScannerFinding]]


@dataclass
class ScannerDef:
    """One scanner: the languages it handles, the binary it needs and its runner.

    ``binary`` is ``None`` for a scanner with nothing to install.
    """

    name: str
    languages: set[str] | None  # None = any
    binary: str | None
    run: Runner


# ----------------------------------------------------------------------- built-in rules
# (rule id, languages or None, regex, message, severity, category, CWE list)
BUILTIN_RULES: list[
    tuple[str, set[str] | None, re.Pattern[str], str, str, str, list[str]]
] = [
    (
        "PY-EVAL",
        {"python"},
        re.compile(r"(?<![\w.])eval\("),
        "eval() on possibly untrusted input",
        "high",
        "vulnerability",
        ["CWE-95"],
    ),
    (
        "PY-EXEC",
        {"python"},
        re.compile(r"(?<![\w.])exec\("),
        "exec() on possibly untrusted input",
        "high",
        "vulnerability",
        ["CWE-95"],
    ),
    (
        "PY-SHELL-TRUE",
        {"python"},
        re.compile(r"shell\s*=\s*True"),
        "subprocess with shell=True enables command injection",
        "high",
        "vulnerability",
        ["CWE-78"],
    ),
    (
        "PY-OS-SYSTEM",
        {"python"},
        re.compile(r"\bos\.system\("),
        "os.system() runs a shell command string",
        "medium",
        "vulnerability",
        ["CWE-78"],
    ),
    (
        "PY-PICKLE",
        {"python"},
        re.compile(r"\bpickle\.loads?\("),
        "pickle deserialization of untrusted data",
        "high",
        "vulnerability",
        ["CWE-502"],
    ),
    (
        "PY-YAML-LOAD",
        {"python"},
        re.compile(r"\byaml\.load\((?![^)]*Loader)"),
        "yaml.load without a safe Loader",
        "medium",
        "vulnerability",
        ["CWE-502"],
    ),
    (
        "PY-WEAK-HASH",
        {"python"},
        re.compile(r"\bhashlib\.(md5|sha1)\("),
        "weak hash algorithm",
        "low",
        "vulnerability",
        ["CWE-328"],
    ),
    (
        "PY-SQL-FSTRING",
        {"python"},
        re.compile(r"(?i)execute(?:many)?\(\s*f['\"]"),
        "SQL built with an f-string",
        "high",
        "vulnerability",
        ["CWE-89"],
    ),
    (
        "PY-SQL-CONCAT",
        {"python"},
        re.compile(r"(?i)execute(?:many)?\([^,)]*(?:\+|%\s*\()"),
        "SQL built by concatenation or % formatting",
        "high",
        "vulnerability",
        ["CWE-89"],
    ),
    (
        "PY-TLS-VERIFY",
        {"python"},
        re.compile(r"verify\s*=\s*False"),
        "TLS certificate verification disabled",
        "medium",
        "vulnerability",
        ["CWE-295"],
    ),
    (
        "PY-MKTEMP",
        {"python"},
        re.compile(r"\btempfile\.mktemp\("),
        "insecure temporary file",
        "medium",
        "vulnerability",
        ["CWE-377"],
    ),
    (
        "PY-BARE-EXCEPT",
        {"python"},
        re.compile(r"^\s*except\s*:\s*(?:pass)?\s*$"),
        "bare except hides errors",
        "low",
        "bug",
        [],
    ),
    (
        "PY-MUTABLE-DEFAULT",
        {"python"},
        re.compile(r"def\s+\w+\([^)]*=\s*(?:\[\]|\{\})"),
        "mutable default argument",
        "medium",
        "bug",
        [],
    ),
    (
        "C-GETS",
        {"c", "cpp"},
        re.compile(r"(?<!\w)gets\("),
        "gets() can never be used safely",
        "critical",
        "vulnerability",
        ["CWE-242", "CWE-120"],
    ),
    (
        "C-STRCPY",
        {"c", "cpp"},
        re.compile(r"(?<!\w)strcpy\("),
        "unbounded strcpy()",
        "high",
        "vulnerability",
        ["CWE-120"],
    ),
    (
        "C-STRCAT",
        {"c", "cpp"},
        re.compile(r"(?<!\w)strcat\("),
        "unbounded strcat()",
        "high",
        "vulnerability",
        ["CWE-120"],
    ),
    (
        "C-SPRINTF",
        {"c", "cpp"},
        re.compile(r"(?<!\w)sprintf\("),
        "unbounded sprintf()",
        "high",
        "vulnerability",
        ["CWE-120"],
    ),
    (
        "C-SCANF-S",
        {"c", "cpp"},
        re.compile(r"(?<!\w)scanf\(\s*\"[^\"]*%s"),
        "scanf with unbounded %s",
        "high",
        "vulnerability",
        ["CWE-120"],
    ),
    (
        "C-SYSTEM",
        {"c", "cpp"},
        re.compile(r"(?<!\w)system\("),
        "system() runs a shell",
        "high",
        "vulnerability",
        ["CWE-78"],
    ),
    (
        "C-PRINTF-FMT",
        {"c", "cpp"},
        re.compile(
            r"(?<!\w)(?:printf|syslog|fprintf\s*\(\s*\w+\s*,)\s*\(?\s*[A-Za-z_]\w*\s*\)"
        ),
        "non-literal format string",
        "high",
        "vulnerability",
        ["CWE-134"],
    ),
    (
        "C-MEMCPY",
        {"c", "cpp"},
        re.compile(r"(?<!\w)memcpy\("),
        "memcpy: verify destination bounds",
        "low",
        "vulnerability",
        ["CWE-119"],
    ),
    (
        "C-RAND",
        {"c", "cpp"},
        re.compile(r"(?<!\w)rand\(\)"),
        "rand() is not cryptographically secure",
        "low",
        "vulnerability",
        ["CWE-338"],
    ),
    (
        "JS-EVAL",
        {"javascript", "typescript"},
        re.compile(r"(?<![\w.])eval\("),
        "eval() on possibly untrusted input",
        "high",
        "vulnerability",
        ["CWE-95"],
    ),
    (
        "JS-NEW-FUNCTION",
        {"javascript", "typescript"},
        re.compile(r"new\s+Function\("),
        "dynamic code via new Function()",
        "medium",
        "vulnerability",
        ["CWE-95"],
    ),
    (
        "JS-INNERHTML",
        {"javascript", "typescript"},
        re.compile(r"\.innerHTML\s*="),
        "innerHTML assignment (XSS)",
        "medium",
        "vulnerability",
        ["CWE-79"],
    ),
    (
        "JS-DANGEROUS-HTML",
        {"javascript", "typescript"},
        re.compile(r"dangerouslySetInnerHTML"),
        "dangerouslySetInnerHTML (XSS)",
        "medium",
        "vulnerability",
        ["CWE-79"],
    ),
    (
        "JS-CHILD-EXEC",
        {"javascript", "typescript"},
        re.compile(r"\bexec(?:Sync)?\(\s*[`'\"].*\$\{"),
        "shell command built from template string",
        "high",
        "vulnerability",
        ["CWE-78"],
    ),
    (
        "GO-SHELL",
        {"go"},
        re.compile(r"exec\.Command\(\s*\"(?:sh|bash)\""),
        "shell invoked via exec.Command",
        "high",
        "vulnerability",
        ["CWE-78"],
    ),
    (
        "GO-TLS-SKIP",
        {"go"},
        re.compile(r"InsecureSkipVerify:\s*true"),
        "TLS verification disabled",
        "medium",
        "vulnerability",
        ["CWE-295"],
    ),
    (
        "GO-SQL-SPRINTF",
        {"go"},
        re.compile(r"fmt\.Sprintf\([^)]*(?:SELECT|INSERT|UPDATE|DELETE)\b"),
        "SQL built with Sprintf",
        "high",
        "vulnerability",
        ["CWE-89"],
    ),
    (
        "ANY-HARDCODED-SECRET",
        None,
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*['\"][^'\"]{6,}['\"]"
        ),
        "hard-coded credential",
        "high",
        "vulnerability",
        ["CWE-798"],
    ),
    (
        "ANY-AWS-KEY",
        None,
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "AWS access key in source",
        "critical",
        "vulnerability",
        ["CWE-798"],
    ),
    (
        "ANY-PRIVATE-KEY",
        None,
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        "private key in source",
        "critical",
        "vulnerability",
        ["CWE-798"],
    ),
    (
        "STYLE-TODO",
        None,
        re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b"),
        "unresolved TODO/FIXME marker",
        "info",
        "style",
        [],
    ),
    (
        "STYLE-LONG-LINE",
        None,
        re.compile(r"^.{141,}$"),
        "line longer than 140 characters",
        "info",
        "style",
        [],
    ),
    (
        "STYLE-DEBUG-PRINT",
        {"python"},
        re.compile(r"^\s*print\((?!.*#\s*noqa)"),
        "debug print left in code",
        "info",
        "style",
        [],
    ),
    (
        "STYLE-CONSOLE-LOG",
        {"javascript", "typescript"},
        re.compile(r"\bconsole\.log\("),
        "console.log left in code",
        "info",
        "style",
        [],
    ),
]


def _builtin(
    root: Path, files: list[ChangedFile], cfg: ScannerConfig
) -> list[ScannerFinding]:
    out: list[ScannerFinding] = []
    for f in files:
        if f.status in ("deleted", "binary"):
            continue
        p = root / f.path
        if not p.is_file():
            continue
        lines = split_lines(p.read_text(encoding="utf-8", errors="replace"))
        targets = (
            set(f.changed_lines())
            if cfg.only_changed_lines
            else set(range(1, len(lines) + 1))
        )
        for ln in sorted(targets):
            if ln - 1 >= len(lines):
                continue
            text = lines[ln - 1]
            if _is_comment(text, f.language):
                continue
            for rule, langs, pat, msg, sev, cat, cwe in BUILTIN_RULES:
                if langs is not None and f.language not in langs:
                    continue
                if pat.search(text):
                    out.append(
                        ScannerFinding(
                            scanner="builtin",
                            rule=rule,
                            message=msg,
                            path=f.path,
                            line=ln,
                            severity=sev,
                            category=cat,
                            cwe=list(cwe),
                        )
                    )
    return out


def _is_comment(line: str, lang: str | None) -> bool:
    s = line.lstrip()
    if lang in ("python", "shell", "ruby", "yaml", "toml"):
        return s.startswith("#")
    return s.startswith(("//", "*", "/*"))


# ----------------------------------------------------------------------- external tools


def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        errors="replace",
        check=False,
    )


def _rel(root: Path, path: str) -> str:
    p = Path(path)
    try:
        return (
            p.resolve().relative_to(root.resolve()).as_posix()
            if p.is_absolute()
            else p.as_posix()
        )
    except ValueError:
        return p.as_posix()


def _ruff(root: Path, paths: list[str], cfg: ScannerConfig) -> list[ScannerFinding]:
    proc = _run(
        [
            "ruff",
            "check",
            "--output-format",
            "json",
            "--exit-zero",
            "--no-cache",
            *paths,
        ],
        root,
        cfg.timeout_s,
    )
    out = []
    for item in json.loads(proc.stdout or "[]"):
        code = item.get("code") or ""
        cat = (
            "vulnerability"
            if code.startswith("S")
            else "bug"
            if code[:1] in ("F", "B") or code.startswith("PLE")
            else "style"
        )
        sev = "medium" if cat != "style" else "low"
        out.append(
            ScannerFinding(
                scanner="ruff",
                rule=code,
                message=item.get("message", ""),
                path=_rel(root, item.get("filename", "")),
                line=(item.get("location") or {}).get("row"),
                severity=sev,
                category=cat,
            )
        )
    return out


def _bandit(root: Path, paths: list[str], cfg: ScannerConfig) -> list[ScannerFinding]:
    proc = _run(["bandit", "-f", "json", "-q", *paths], root, cfg.timeout_s)
    data = json.loads(proc.stdout or "{}")
    out = []
    for r in data.get("results", []):
        cwe = r.get("issue_cwe") or {}
        out.append(
            ScannerFinding(
                scanner="bandit",
                rule=r.get("test_id", ""),
                message=r.get("issue_text", ""),
                path=_rel(root, r.get("filename", "")),
                line=r.get("line_number"),
                severity=str(r.get("issue_severity", "medium")).lower(),
                category="vulnerability",
                cwe=[f"CWE-{cwe['id']}"] if cwe.get("id") else [],
            )
        )
    return out


def _semgrep(root: Path, paths: list[str], cfg: ScannerConfig) -> list[ScannerFinding]:
    proc = _run(
        ["semgrep", "--config", cfg.semgrep_config, "--json", "--quiet", *paths],
        root,
        cfg.timeout_s,
    )
    data = json.loads(proc.stdout or "{}")
    out = []
    for r in data.get("results", []):
        extra = r.get("extra") or {}
        meta = extra.get("metadata") or {}
        cwes = meta.get("cwe") or []
        cwes = [cwes] if isinstance(cwes, str) else cwes
        cwe_ids = [
            m.group(0) for c in cwes for m in [re.search(r"CWE-\d+", str(c))] if m
        ]
        cat = (
            "vulnerability" if cwe_ids or "security" in r.get("check_id", "") else "bug"
        )
        out.append(
            ScannerFinding(
                scanner="semgrep",
                rule=r.get("check_id", ""),
                message=extra.get("message", ""),
                path=_rel(root, r.get("path", "")),
                line=(r.get("start") or {}).get("line"),
                severity=str(extra.get("severity", "medium")).lower(),
                category=cat,
                cwe=cwe_ids,
            )
        )
    return out


def _cppcheck(root: Path, paths: list[str], cfg: ScannerConfig) -> list[ScannerFinding]:
    tpl = "{file}\t{line}\t{severity}\t{id}\t{message}"
    proc = _run(
        [
            "cppcheck",
            "--enable=warning,style,performance",
            f"--template={tpl}",
            "--quiet",
            *paths,
        ],
        root,
        cfg.timeout_s,
    )
    out = []
    for line in proc.stderr.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        file, ln, sev, rule, msg = parts
        cat = "bug" if sev in ("error", "warning") else "style"
        out.append(
            ScannerFinding(
                scanner="cppcheck",
                rule=rule,
                message=msg,
                path=_rel(root, file),
                line=int(ln) if ln.isdigit() else None,
                severity="high"
                if sev == "error"
                else "medium"
                if sev == "warning"
                else "low",
                category=cat,
            )
        )
    return out


def _gosec(root: Path, paths: list[str], cfg: ScannerConfig) -> list[ScannerFinding]:
    proc = _run(["gosec", "-fmt", "json", "-quiet", "./..."], root, cfg.timeout_s)
    data = json.loads(proc.stdout or "{}")
    wanted = set(paths)
    out = []
    for issue in data.get("Issues", []):
        rel = _rel(root, issue.get("file", ""))
        if rel not in wanted:
            continue
        cwe = issue.get("cwe") or {}
        out.append(
            ScannerFinding(
                scanner="gosec",
                rule=issue.get("rule_id", ""),
                message=issue.get("details", ""),
                path=rel,
                line=int(str(issue.get("line", "0")).split("-")[0] or 0) or None,
                severity=str(issue.get("severity", "medium")).lower(),
                category="vulnerability",
                cwe=[f"CWE-{cwe['id']}"] if cwe.get("id") else [],
            )
        )
    return out


SCANNERS: dict[str, ScannerDef] = {
    "builtin": ScannerDef(
        "builtin", None, None, lambda root, paths, cfg: []
    ),  # handled specially
    "ruff": ScannerDef("ruff", {"python"}, "ruff", _ruff),
    "bandit": ScannerDef("bandit", {"python"}, "bandit", _bandit),
    "semgrep": ScannerDef("semgrep", None, "semgrep", _semgrep),
    "cppcheck": ScannerDef("cppcheck", {"c", "cpp"}, "cppcheck", _cppcheck),
    "gosec": ScannerDef("gosec", {"go"}, "gosec", _gosec),
}


def run_scanners(
    cfg: ScannerConfig, root: Path, files: list[ChangedFile]
) -> ScannerReport:
    """Run the enabled scanners on the changed files and collect their findings.

    Deleted and binary files are skipped, as are scanners for languages none of
    the files are in. A scanner whose binary is not installed is listed in
    ``scanners_missing``; an unknown name or a scanner that fails is noted in
    ``errors`` and the others still run.

    Args:
        cfg: Which scanners to run, their timeout, and whether to keep only
            findings on changed lines (a finding without a line is kept).
        root: The checkout to scan.
        files: The changed files.

    Returns:
        The findings, sorted by path, line, scanner and rule.
    """
    report = ScannerReport()
    live = [f for f in files if f.status not in ("deleted", "binary")]
    changed = {f.path: set(f.changed_lines()) for f in live}
    for name in cfg.enabled:
        sdef = SCANNERS.get(name)
        if sdef is None:
            report.errors.append(f"unknown scanner '{name}'")
            continue
        if name == "builtin":
            report.findings.extend(_builtin(root, live, cfg))
            report.scanners_run.append(name)
            continue
        paths = [
            f.path
            for f in live
            if sdef.languages is None or f.language in sdef.languages
        ]
        if not paths:
            continue
        if sdef.binary and not shutil.which(sdef.binary):
            report.scanners_missing.append(name)
            continue
        try:
            found = sdef.run(root, paths, cfg)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
            report.errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        if cfg.only_changed_lines:
            found = [
                f
                for f in found
                if f.line is None or f.line in changed.get(f.path, set())
            ]
        report.findings.extend(found)
        report.scanners_run.append(name)
    report.findings.sort(key=lambda f: (f.path, f.line or 0, f.scanner, f.rule))
    return report
