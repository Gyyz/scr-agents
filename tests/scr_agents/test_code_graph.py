from pathlib import Path

from scr_agents.inputs.git import changed_files
from scr_agents.ws2_analysis.code_graph import CodeGraph


def test_python_and_c_symbols_and_blast_radius(demo_repo):
    """The graph finds Python and C symbols, their callers and a change's impact."""
    repo, base, head = demo_repo
    g = CodeGraph.build(Path(repo))
    names = {s.name for s in g.symbols["app/utils.py"]}
    assert {"run", "load_settings", "last_n"} <= names
    assert {s.name for s in g.symbols["native/greet.c"]} == {"greet", "main"}
    callers = {c.path for c in g.callers("last_n")}
    assert "tests/test_utils.py" in callers
    report = g.blast_radius(changed_files(repo, base, head))
    assert any(s.name == "run" for s in report.changed_symbols)
    assert "tests/test_utils.py" in report.impacted_files
    q = g.query("greet")
    assert q["definitions"][0]["path"] == "native/greet.c"
    assert any(c["name"] == "main" for c in q["callers"])
