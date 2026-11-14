from scr_agents.ws5_knowledge.knowledge_base import KnowledgeBase


def test_lookup_by_id_and_keywords():
    """Entries are found by id in any case and by keywords across CWE and MISRA."""
    kb = KnowledgeBase()
    assert kb.get("cwe-120").title.startswith("Classic Buffer")
    ids = [e.id for e in kb.lookup("strcpy buffer overflow", limit=3)]
    assert "CWE-120" in ids
    assert kb.lookup("null pointer dereference", 1)[0].id in ("CWE-476", "EXP34-C")
    assert any(e.standard == "MISRA" for e in kb.lookup("malloc dynamic allocation", 5))
