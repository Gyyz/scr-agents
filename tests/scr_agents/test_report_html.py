"""The detailed HTML report.

Written with every run, self-contained, safe with hostile repository text, served
behind the console token (and as a download that leaves out host paths).
"""

import re
import time

from starlette.testclient import TestClient

from scr_agents.config import Settings
from scr_agents.models import (
    Evidence,
    EvidenceReport,
    Finding,
    Location,
    ReviewRequest,
    ValidatedFinding,
    Verdict,
)
from scr_agents.web.server import create_app
from scr_agents.ws3_harness.report_html import render_html, report_filename

TOKEN = "html-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HOSTILE = '<script>alert(1)</script><img src=x onerror="fetch(`//evil`)">'


def _settings(tmp_path):
    return Settings.load(
        overrides={
            "llm": {"mode": "offline"},
            "output_dir": str(tmp_path / "out"),
            "projects_dir": str(tmp_path / "out" / "projects"),
            "scanners": {"enabled": ["builtin"]},
            "web": {"token": TOKEN, "allowed_hosts": ["testserver"]},
        }
    )


def _report() -> EvidenceReport:
    req = ReviewRequest(
        repo_path="/home/alice/secret-project/app-0123abcd",
        base_ref="HEAD~1",
        head_ref="HEAD",
        base="a" * 40,
        head="b" * 40,
        instructions=f"check {HOSTILE}",
    )
    bad = Finding(
        title=f"Injection {HOSTILE}",
        location=Location(path=f"src/{HOSTILE}.py", line=3),
        description=f"Calls `os.system(cmd)` with **user input**.\n\n{HOSTILE}",
        rationale="</pre></details>",
        cwe=["CWE-78", "not-a-cwe <b>"],
        code_quote=f"os.system({HOSTILE})",
        severity="critical",
        category="vulnerability",
        suggested_fix=(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
            "-os.system(cmd)\n+subprocess.run([cmd])\n"
        ),
    )
    evidence = [
        Evidence(
            kind="command",
            command=f"python3 poc.py {HOSTILE}",
            output=f"pwned {HOSTILE}",
            exit_code=0,
            ref="head",
        )
    ]
    return EvidenceReport(
        run_id="run-html1",
        request=req,
        findings=[
            ValidatedFinding(
                finding=bad,
                verdict=Verdict.CONFIRMED,
                validator_notes=f"Ran it. {HOSTILE}",
                evidence=evidence,
                evidence_strength="executed",
            ),
            ValidatedFinding(
                finding=Finding(
                    title="Long line",
                    location=Location(path="a.py", line=9),
                    severity="low",
                    category="style",
                ),
                verdict=Verdict.UNVERIFIED,
            ),
        ],
    )


def test_hostile_repository_text_is_escaped_and_nothing_else_can_run():
    """Hostile text is escaped and only the page's own nonce script can run."""
    r = _report()
    page = render_html(
        r,
        [
            {
                "finding_id": r.findings[0].finding.id,
                "verdict": "reject",
                "note": HOSTILE,
            }
        ],
    )
    assert (
        "<script>alert" not in page and "<img" not in page and 'onerror="' not in page
    )
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    nonce = re.search(r"script-src 'nonce-([^']+)'", page).group(1)
    scripts = re.findall(r"<script([^>]*)>", page)
    assert scripts == [f' nonce="{nonce}"'], "one script, carrying the policy's nonce"
    assert (
        "default-src 'none'" in page
        and '<meta name="referrer" content="no-referrer">' in page
    )
    assert not re.search(
        r"""(?:src|href)=["']?(?:https?:)?//(?!cwe\.mitre\.org/)""", page
    ), "no external loads"
    assert (
        'href="https://cwe.mitre.org/data/definitions/78.html"' in page
        and "not-a-cwe &lt;b&gt;" in page
    )
    assert (
        "<code>os.system(cmd)</code>" in page and "<b>user input</b>" in page
    )  # the little markdown models write
    assert (
        '<span class="add">+subprocess.run([cmd])</span>' in page
        and '<span class="del">-os.system(cmd)</span>' in page
    )
    assert "human: reject" in page


def test_a_copy_for_others_leaves_out_host_paths():
    """A shared copy hides host paths and names the repository without its path."""
    r = _report()
    r.findings[0].evidence[
        0
    ].output = (
        'Traceback:\n  File "/srv/out/sessions/s1/run-html1/work/head/poc.py", line 3'
    )
    assert "/home/alice" in render_html(r) and "/srv/out/sessions" in render_html(r)
    copy = render_html(
        r, shared=True, scrub={"/srv/out/sessions/s1/run-html1": "<run>"}
    )
    assert (
        "/home/alice" not in copy
        and "/srv/out" not in copy
        and "&lt;run&gt;/work/head/poc.py" in copy
    )
    assert "<code>app</code>" in copy, "the repository is named, not located"
    assert report_filename(r) == "scr-agents-app-run-html1.html"


def test_every_run_writes_it_and_the_console_serves_it_behind_the_token(tmp_path):
    """Each run writes ``report.html``; the console serves it only with the token."""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, token=TOKEN), headers=AUTH) as c:
        run_id = c.post("/api/demo", json={}).json()["run_id"]
        deadline = time.time() + 120
        while c.get(f"/api/runs/{run_id}").json()["status"] == "running":
            assert time.time() < deadline
            time.sleep(0.2)
        run_dir = c.app.state.manager.run_dir(run_id)
        assert (run_dir / "report.html").is_file()

        res = c.get(f"/api/runs/{run_id}/report.html")
        assert res.status_code == 200 and res.headers["content-type"].startswith(
            "text/html"
        )
        nonce = re.search(
            r"'nonce-([^']+)'", res.headers["content-security-policy"]
        ).group(1)
        assert (
            f'<script nonce="{nonce}">' in res.text
            and "frame-ancestors 'none'" in res.headers["content-security-policy"]
        )
        assert (
            res.headers["x-content-type-options"] == "nosniff"
            and res.headers["referrer-policy"] == "no-referrer"
        )
        report = EvidenceReport.model_validate_json(
            (run_dir / "report.json").read_text()
        )
        assert all(vf.finding.id in res.text for vf in report.findings)
        assert report.request.repo_path in res.text

        down = c.get(f"/api/runs/{run_id}/report.html?download=1")
        assert down.headers["content-disposition"].startswith(
            'attachment; filename="scr-agents-'
        )
        assert report.request.repo_path not in down.text
        assert (
            str(tmp_path) not in down.text and str(tmp_path.resolve()) not in down.text
        ), "no host paths in a copy"
        assert all(vf.finding.id in down.text for vf in report.findings)

        fid = report.findings[0].finding.id
        assert (
            c.post(
                f"/api/runs/{run_id}/verdict",
                json={"finding": fid, "verdict": "reject", "note": "test file"},
            ).status_code
            == 200
        )
        assert "human: reject" in c.get(f"/api/runs/{run_id}/report.html").text, (
            "rendered fresh, with later human verdicts"
        )

        assert (
            TestClient(c.app).get(f"/api/runs/{run_id}/report.html").status_code == 401
        )
        assert c.get("/api/runs/run-missing/report.html").status_code == 404
        assert c.get("/api/runs/..%2F..%2Fx/report.html").status_code in (400, 404)
