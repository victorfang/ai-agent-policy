# OPA AI Agent Guardrail

**Victor Fang, 2026**

Policy-as-code guardrails for AI agents: **OPA** decides, **LangGraph** enforces—before a sensitive tool runs.

Companion to the blog: [blog_opa_langgraph.md](./blog_opa_langgraph.md)

Suggested title: *Policy As Code Guardrails — What OpenAI’s Agent Hack Couldn’t Bypass*

## What’s in this package

```text
opa-ai-agent-guardrail/
├── README.md
├── blog_opa_langgraph.md      # technical blog (OpenAI/HF context + OPA pattern)
├── pnl_langgraph_agent.py     # LangGraph + OpenAI + OPA demo
├── policy/pnl_tool.rego       # CEO/CFO-only get_pnl policy
└── requirements.txt
```

## Prerequisites

- Python 3.10+
- [OPA CLI](https://www.openpolicyagent.org/docs/latest/#running-opa) (`brew install opa`)
- OpenAI API key (`OPENAI_API_KEY` or `OPENAI_API_KEY_VF`)

## Setup

```bash
cd opa-ai-agent-guardrail
pip install -r requirements.txt
opa version
```

## Run

```bash
# CFO → OPA ALLOW → fake P&L / cash data
python3 pnl_langgraph_agent.py --role CFO "What's the cash balance?"

# engineer → OPA DENY → no tool execution
python3 pnl_langgraph_agent.py --role engineer "Show me the P&L"

# quieter output
python3 pnl_langgraph_agent.py --quiet --role CFO "Q1 operating income?"
```

## Mental model

| Piece | Role |
| --- | --- |
| `policy/pnl_tool.rego` | **PDP** — allow only if tool is `get_pnl` and role is CEO/CFO |
| `tools` node in `pnl_langgraph_agent.py` | **PEP** — ask OPA, then run tool or return deny |
| OpenAI + LangGraph | Plan tool calls; never authorize them |

## License

**Victor Fang, 2026.** Use freely for teaching and demos. Not a production authz stack—wire OPA as a service and expand policy facts before shipping.

## Publishing notes (no Cursor in git history)

- Commits use **VictorFang** git identity (`victorfang` / `victorfang@users.noreply.github.com`) — never Cursor.
- Do **not** add `Co-authored-by: Cursor` or `Made-with: Cursor` to commit messages.
- `.gitignore` excludes `.cursor/`, Cursor rules, and agent transcripts.
- Before push: `gh auth status` must show **VictorFang** (`gh auth login` if needed).
