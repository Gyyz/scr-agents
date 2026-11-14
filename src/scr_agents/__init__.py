"""scr_agents: a multi-agent code review system.

Workstreams (see docs/superpowers/specs/2026-10-08-code-review-agent-design.md):
  WS1 review agent + subagents + validator      -> scr_agents.ws1_review
  WS2 code graph + static analysis              -> scr_agents.ws2_analysis
  WS3 PoC harness (test bench, shell, evidence) -> scr_agents.ws3_harness
  WS4 guardrails + supply chain                 -> scr_agents.ws4_guardrails
  WS5 knowledge + observability + human review  -> scr_agents.ws5_knowledge
"""

__version__ = "0.1.0"
