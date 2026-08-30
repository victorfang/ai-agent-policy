#!/usr/bin/env python3
"""Tiny LangGraph + OpenAI agent with OPA gating a fake P&L tool.

Author: Victor Fang, 2026

Flow:
  user → LLM (may request get_pnl) → OPA (CEO/CFO only) → fake P&L or deny → LLM answer

Usage:
  export OPENAI_API_KEY=sk-...
  pip install -r requirements.txt

  python3 pnl_langgraph_agent.py --role CFO "What was Q1 operating income?"
  python3 pnl_langgraph_agent.py --role engineer "Show me the P&L"
  python3 pnl_langgraph_agent.py --quiet ...   # silence DEBUG dumps
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

ROOT = Path(__file__).resolve().parent
POLICY = ROOT / "policy" / "pnl_tool.rego"

# On by default — dump states / prompts / tool / OPA at every step.
DEBUG = True


def dbg(title: str, payload: Any = None) -> None:
    if not DEBUG:
        return
    bar = "=" * 72
    print(f"\n{bar}")
    print(f"[DEBUG] {title}")
    print(bar)
    if payload is None:
        return
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(payload)


def message_to_dict(msg: BaseMessage) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": msg.__class__.__name__,
        "content": getattr(msg, "content", None),
    }
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        data["tool_calls"] = tool_calls
    if isinstance(msg, ToolMessage):
        data["tool_call_id"] = msg.tool_call_id
    return data


def dump_state(label: str, state: AgentState | dict) -> None:
    dbg(
        f"STATE @ {label}",
        {
            "role": state.get("role"),
            "message_count": len(state.get("messages") or []),
            "messages": [message_to_dict(m) for m in state.get("messages") or []],
        },
    )


def openai_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY_VF")
    if not key:
        raise SystemExit(
            "Set OPENAI_API_KEY (or OPENAI_API_KEY_VF) before running this demo."
        )
    return key


# ---------------------------------------------------------------------------
# Fake finance API — stand-in for a real P&L / cash service.
# The LLM can only reach this via the LangGraph "tools" node, and only after OPA.
# ---------------------------------------------------------------------------
FAKE_PNL = {
    "period": "2026-Q1",
    "currency": "USD",
    "revenue": 128_400_000,
    "cogs": 51_200_000,
    "gross_profit": 77_200_000,
    "opex": 41_500_000,
    "operating_income": 35_700_000,
    "net_income": 28_100_000,
    "cash_balance": 92_300_000,  # snapshot for the period (demo only)
}


@tool
def get_pnl(period: str = "2026-Q1") -> str:
    """Fetch company financials for a period: P&L lines and cash balance."""
    # LangChain @tool: docstring becomes the tool description the LLM sees.
    return json.dumps({**FAKE_PNL, "period": period})


def opa_allows(*, role: str, tool_name: str) -> bool:
    """Policy Enforcement Point (PEP): ask OPA before the tool runs.

    Builds an input document, shells out to `opa eval` against pnl_tool.rego,
    and returns True only if data.pnl.allow is true (CEO/CFO + get_pnl).
    """
    # Facts for Rego: input.user.role and input.tool  (see policy/pnl_tool.rego)
    payload = {"user": {"role": role}, "tool": tool_name}
    dbg("OPA input", payload)
    dbg("OPA policy file", str(POLICY))
    dbg("OPA query", "data.pnl.allow")

    # opa eval wants a file path for --input
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = fh.name
    try:
        cmd = [
            "opa",
            "eval",
            "--format",
            "raw",  # print bare true/false
            "--data",
            str(POLICY),
            "--input",
            path,
            "data.pnl.allow",  # package pnl → rule allow
        ]
        dbg("OPA command", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    finally:
        Path(path).unlink(missing_ok=True)

    if result.returncode != 0:
        dbg("OPA stderr", result.stderr.strip())
        raise RuntimeError(result.stderr.strip() or "opa eval failed")

    allowed = result.stdout.strip().lower() == "true"
    dbg("OPA raw stdout", result.stdout.strip())
    dbg("OPA decision", "ALLOW" if allowed else "DENY")
    return allowed


class AgentState(TypedDict):
    # add_messages: new messages are appended, not replaced, when a node returns.
    messages: Annotated[list, add_messages]
    role: str  # carried for clarity; OPA uses the role closed over in build_graph


def build_graph(role: str):
    """Wire the LangGraph: START → agent ⇄ tools → agent → END.

    Graph shape:
        START
          ↓
        agent  --(tool_calls?)-->  tools  --(always)-->  agent
          ↓ (no tool_calls)
         END

    The LLM never calls get_pnl directly; the tools node is the PEP.
    """
    # Bind tools so the model can emit structured tool_calls for get_pnl.
    llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0,
        api_key=openai_api_key(),
    ).bind_tools([get_pnl])

    def agent(state: AgentState) -> dict:
        """LLM node: read chat history, return either text or a tool_call."""
        dump_state("agent (enter)", state)
        dbg(
            "PROMPT sent to LLM (full message list)",
            [message_to_dict(m) for m in state["messages"]],
        )
        reply = llm.invoke(state["messages"])
        dbg("LLM response", message_to_dict(reply))
        # Returning {"messages": [...]} merges into state via add_messages.
        return {"messages": [reply]}

    def tools(state: AgentState) -> dict:
        """Tool node + OPA gate: enforce policy, then call fake API or deny."""
        dump_state("tools (enter)", state)
        last = state["messages"][-1]
        # Router only sends us here when the last AIMessage has tool_calls.
        assert isinstance(last, AIMessage) and last.tool_calls

        out: list[ToolMessage] = []
        for call in last.tool_calls:
            name = call["name"]
            args = call.get("args") or {}
            dbg("Tool call requested by LLM", {"name": name, "args": args, "id": call.get("id")})

            # --- PDP check (OPA) before any side effect / data access ---
            allowed = opa_allows(role=role, tool_name=name)
            print(f"[OPA] role={role!r} tool={name!r} -> {'ALLOW' if allowed else 'DENY'}")

            if not allowed:
                # Still return a ToolMessage so the LLM can explain the denial.
                # We do NOT call get_pnl — fail closed.
                msg = ToolMessage(
                    content=(
                        f"DENIED by OPA: role {role!r} cannot call {name!r}. "
                        "Only CEO or CFO may access P&L data."
                    ),
                    tool_call_id=call["id"],
                )
                dbg("Tool result (denied)", message_to_dict(msg))
                out.append(msg)
                continue

            # Allowed → hit the fake P&L API, then feed JSON back to the LLM.
            raw = get_pnl.invoke(args)
            dbg("Fake P&L API result", raw)
            msg = ToolMessage(content=raw, tool_call_id=call["id"])
            dbg("Tool result (allowed)", message_to_dict(msg))
            out.append(msg)

        return {"messages": out}

    def route(state: AgentState) -> Literal["tools", "end"]:
        """After agent: go to tools if the model requested a tool, else finish."""
        last = state["messages"][-1]
        has_tools = isinstance(last, AIMessage) and bool(last.tool_calls)
        nxt: Literal["tools", "end"] = "tools" if has_tools else "end"
        dbg(
            "ROUTER after agent",
            {
                "last_message_type": last.__class__.__name__,
                "has_tool_calls": has_tools,
                "next_node": nxt,
            },
        )
        return nxt

    # Assemble the graph: nodes + edges (including the conditional router).
    g = StateGraph(AgentState)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", "end": END})
    g.add_edge("tools", "agent")  # tool results go back to the LLM for a final answer
    return g.compile()


def run(role: str, question: str) -> str:
    """One-shot invoke: build initial state, run the graph, return final text."""
    # System prompt steers the model to use get_pnl for finance questions.
    # Authz is NOT here — OPA still decides in the tools node.
    system = SystemMessage(
        content=(
            "You are a finance assistant. For questions about revenue, profit, "
            "income, P&L, cash, or cash balance, always call the get_pnl tool "
            "and answer from its JSON. Do not invent numbers. "
            f"The current user role is {role}."
        )
    )
    initial: AgentState = {
        "messages": [system, HumanMessage(content=question)],
        "role": role,
    }
    dbg("INITIAL STATE (before graph)", None)
    dump_state("graph start", initial)
    dbg("System prompt text", system.content)
    dbg("User question", question)

    # Blocks until the graph hits END (possibly after agent → tools → agent).
    result = build_graph(role).invoke(initial)
    dump_state("graph end (final)", result)

    final = result["messages"][-1]
    return getattr(final, "content", str(final))


def main() -> int:
    global DEBUG
    parser = argparse.ArgumentParser(description="OPA-gated P&L LangGraph demo")
    parser.add_argument("--role", default="CFO", help="User role (CEO, CFO, engineer, …)")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Disable DEBUG dumps (DEBUG is on by default)",
    )
    parser.add_argument(
        "question",
        nargs="?",
        default="What was operating income in Q1 2026?",
    )
    args = parser.parse_args()
    if args.quiet:
        DEBUG = False

    print(f"role={args.role}")
    print(f"Q: {args.question}")
    print(f"DEBUG={'on' if DEBUG else 'off'}")
    print("---")
    answer = run(args.role, args.question)
    print("\n" + "=" * 72)
    print("[FINAL ANSWER]")
    print("=" * 72)
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
