#!/usr/bin/env python3
"""
Provision the Tilicho Credit Assist agent on CX Agent Studio using **CXAS SCRAPI**
(`cxas-scrapi`, Google's official Python toolkit) — built entirely through the SCRAPI SDK.

Builds the app `tilicho-credit-scrapi` so provenance is 100% SCRAPI:
  App → OpenAPI toolset → file-search (RAG) tool → guardrail → 3 agents → root agent.

Requires Python >=3.10 (use .venv-scrapi). Config, tool schema, tool ids and a thin REST
helper come from agent/ces_common.py; instructions from agent/agent_spec.py — single sources
of truth for the agent's content.

Usage:
    .venv-scrapi/bin/python agent/provision_scrapi.py provision
    .venv-scrapi/bin/python agent/provision_scrapi.py chat "My loan is TL-1001, phone 4417, EMI?"
    .venv-scrapi/bin/python agent/provision_scrapi.py info
"""
import os
import sys
import time

from google.api_core import exceptions as gax
from cxas_scrapi import Apps, Agents, Tools, Guardrails, Sessions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
import ces_common as cc  # noqa: E402  (config + tool schema + REST helper)

PROJECT = cc.PROJECT_ID
LOCATION = cc.LOCATION                      # "us"
APP_ID = os.environ.get("SCRAPI_APP_ID", "tilicho-credit-scrapi")
APP_NAME = f"projects/{PROJECT}/locations/{LOCATION}/apps/{APP_ID}"
TOOLSET_ID, RAG_TOOL_ID, GUARDRAIL_ID = "servicing", "policy-rag", "compliance"
TOOLSET_NAME = f"{APP_NAME}/toolsets/{TOOLSET_ID}"
RAG_TOOL_NAME = f"{APP_NAME}/tools/{RAG_TOOL_ID}"
GUARDRAIL_NAME = f"{APP_NAME}/guardrails/{GUARDRAIL_ID}"
# Agent instructions — single source of truth (agent/agent_spec.py).
import agent_spec  # noqa: E402

AGENTS = {  # agent_id -> (display, instruction)
    "credit-assist": ("Credit Assist", agent_spec.FULL),
    "credit-assist-lean": ("Credit Assist Lean", agent_spec.LEAN),
    "assist": ("Agent Assist", agent_spec.ASSIST),
}
RAG_CORPUS = cc.ENV.get("RAG_CORPUS")


def _ok(fn, what):
    try:
        fn(); print(f"  created {what}")
    except gax.AlreadyExists:
        print(f"  exists  {what}")
    except Exception as e:
        print(f"  ERROR {what}: {str(e)[:200]}")


def provision():
    print(f"SCRAPI build · project {PROJECT} · location {LOCATION} · app {APP_ID}")

    # 1) App (display name must be unique per project; creation is async)
    apps = Apps(PROJECT, LOCATION)
    _ok(lambda: apps.create_app(APP_ID, "Tilicho Credit Assist SCRAPI",
                                description="Lending CX agent (built with CXAS SCRAPI)."),
        f"app {APP_ID}")
    for _ in range(30):  # wait for the app to be ready before creating children
        try:
            apps.get_app(APP_NAME); break
        except Exception:
            time.sleep(3)

    tools = Tools(APP_NAME)
    # 2) OpenAPI toolset -> Cloud Run backend (Secret Manager auth)
    _ok(lambda: tools.create_tool(
        TOOLSET_ID, "Servicing API", tool_type="open_api_toolset",
        payload={
            "open_api_schema": cc.load_openapi_schema(),
            "url": cc.BACKEND_URL,
            "api_authentication": {"api_key_config": {
                "key_name": "X-API-Key", "request_location": "HEADER",
                "api_key_secret_version": cc.SECRET_VERSION}},
        }), f"toolset {TOOLSET_ID}")
    # keep the toolset's OpenAPI schema current on re-runs (added phone_last4 param).
    # SCRAPI's UpdateToolset isn't accepted by the API, so use the raw REST PATCH.
    try:
        s, _ = cc.api("PATCH", f"{TOOLSET_NAME}?updateMask=openApiToolset",
            {"openApiToolset": {
                "openApiSchema": cc.load_openapi_schema(), "url": cc.BACKEND_URL,
                "apiAuthentication": {"apiKeyConfig": {
                    "keyName": "X-API-Key", "requestLocation": "HEADER",
                    "apiKeySecretVersion": cc.SECRET_VERSION}}}})
        print(f"  toolset schema PATCH: {s}")
    except Exception as e:
        print(f"  toolset update ERR: {str(e)[:160]}")

    # 3) File-search (RAG) tool -> Vertex AI RAG Engine corpus
    if RAG_CORPUS:
        _ok(lambda: tools.create_tool(
            RAG_TOOL_ID, "policy_kb", tool_type="file_search_tool",
            description="Grounded answers from Tilicho loan policies, FAQ, fair-practice & grievance docs.",
            payload={"name": "policy_kb",
                     "description": "Grounded answers from Tilicho loan policies, FAQ, fair-practice & grievance docs.",
                     "file_corpus": RAG_CORPUS, "corpus_type": "USER_OWNED"}),
            f"rag tool {RAG_TOOL_ID}")

    # 4) Compliance guardrail (LLM policy + safe generative answer)
    guards = Guardrails(APP_NAME)
    _ok(lambda: guards.create_guardrail(
        GUARDRAIL_ID, "No advice / no false promises",
        payload={"llm_policy": {
            "policy_scope": "AGENT_RESPONSE",
            "prompt": ("Flag the response ONLY if it (a) gives legal, tax, or investment "
                       "advice or recommendations, or (b) promises/guarantees a loan "
                       "approval, fee waiver, rate reduction, or a specific outcome. Do "
                       "NOT flag factual statements of standard policy, fees, rates, EMIs, "
                       "due dates, payoff amounts, SLAs, or processes."),
            "fail_open": True}},
        action={"generative_answer": {"prompt": (
            "Politely say you can't give legal, tax, or investment advice or guarantee "
            "outcomes, and offer to explain the loan's terms or connect a human.")}}),
        f"guardrail {GUARDRAIL_ID}")
    # 4b) Input-side guardrail: built-in prompt-injection / jailbreak screen (output guardrail
    #     above is response-only). Demo posture: fail-open; prod flips fail-closed (docs/SECURITY.md).
    _ok(lambda: guards.create_guardrail(
        "prompt-security", "Prompt-injection / jailbreak screen",
        payload={"llm_prompt_security": {"default_settings": {}, "fail_open": True}},
        action={"generative_answer": {"prompt": (
            "Politely refuse: say you can only help with Tilicho Credit loan servicing "
            "and can't act on that request.")}}),
        "guardrail prompt-security")

    # 5) Agents (instruction only first; model unset -> inherits a valid 'us' model)
    ag = Agents(APP_NAME)
    for aid, (disp, instr) in AGENTS.items():
        _ok(lambda aid=aid, disp=disp, instr=instr: ag.create_agent(
            display_name=disp, agent_id=aid, agent_type="llm", model=None, instruction=instr),
            f"agent {aid}")

    # 6) Wire tools/guardrails onto each agent (PATCH). Retry transient 400.
    toolset_ref = [{"toolset": TOOLSET_NAME, "tool_ids": cc.TOOL_IDS}]
    rag_ref = [RAG_TOOL_NAME] if RAG_CORPUS else []
    wiring = {
        "credit-assist": {"toolsets": toolset_ref, "tools": rag_ref,
                          "guardrails": [GUARDRAIL_NAME, f"{APP_NAME}/guardrails/prompt-security"]},
        "credit-assist-lean": {"toolsets": toolset_ref, "tools": rag_ref},
        "assist": {"tools": rag_ref},
    }
    for aid, kw in wiring.items():
        name = f"{APP_NAME}/agents/{aid}"
        kw = {**kw, "instruction": AGENTS[aid][1]}  # keep instruction current on re-runs
        for attempt in range(5):
            try:
                ag.update_agent(name, **kw); print(f"  wired   agent {aid}"); break
            except Exception as e:
                if attempt < 4:
                    time.sleep(8); continue
                print(f"  ERROR wiring {aid}: {str(e)[:200]}")

    # 7) Root agent
    try:
        apps.update_app(APP_NAME, root_agent=f"{APP_NAME}/agents/credit-assist")
        print("  set root agent: credit-assist")
    except Exception as e:
        print(f"  ERROR root agent: {str(e)[:200]}")
    print("Done. (Examples omitted — optional; keeps the build 100% SCRAPI.)")
    info()


def chat(text):
    sess = Sessions(APP_NAME)
    sid = sess.create_session_id()
    for _ in range(8):
        try:
            res = sess.run(sid, text=text)
        except Exception as e:
            print("err", str(e)[:160]); time.sleep(12); continue
        g = sess.get_structured_response(res)
        reply = (g.get("agent_text") or "").strip()
        # the SDK sometimes doubles the streamed text; collapse an exact repeat
        half = len(reply) // 2
        if half and reply[:half].strip() == reply[half:].strip():
            reply = reply[:half].strip()
        tools = [t.get("action") for t in g.get("tool_calls", [])]
        print(reply or g)
        if tools:
            print("   tools:", tools)
        return


def info():
    apps = Apps(PROJECT, LOCATION)
    try:
        app = apps.get_app(APP_NAME)
        print(f"\napp   {APP_NAME}\nroot  {getattr(app,'root_agent','?')}")
    except Exception as e:
        print("info err", str(e)[:160])
    try:
        print("agents:", list(Agents(APP_NAME).get_agents_map().keys()))
        print("tools :", list(Tools(APP_NAME).get_tools_map().keys()))
    except Exception as e:
        print("list err", str(e)[:160])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "provision":
        provision()
    elif cmd == "chat":
        chat(" ".join(sys.argv[2:]) or "hello")
    else:
        info()
