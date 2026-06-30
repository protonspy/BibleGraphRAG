<!-- compiled from docs.langchain.com/oss/python/langgraph | 2026-06-30 | curated reference -->

# LangGraph (OSS Python) — Curated Reference

> Compiled from https://docs.langchain.com/oss/python/langgraph
> Scope: the primitives and patterns needed to build the **Deep Researcher over GraphRAG**.
> This is a curated reference (not a full wiki dump). Each section maps to how we'll use it.

## Format

Sections are delimited by `<<< SECTION: Title [slug] >>>` lines.
Grep for `^<<< SECTION:` to list all sections.

## Structure

├── 1 What & When
├── 2 Mental Model (state machine)
├── 3 State & Reducers
├── 4 Nodes
├── 5 Edges, Conditional Edges (routing)
├── 6 Command (update + goto)
├── 7 Send (map-reduce / fan-out)
├── 8 Subgraphs
├── 9 Workflow Patterns (routing, orchestrator-worker)
├── 10 Agents & ToolNode
├── 11 Persistence (checkpointer + store)
├── 12 Operational (retries, timeouts, cache, recursion, HITL)
└── 13 Bridge → Deep Researcher over GraphRAG

---

<<< SECTION: 1 What & When [1-what-and-when] >>>

# What & When

LangGraph is a **low-level orchestration framework/runtime** for stateful, long-running agents. Built by LangChain Inc.; usable **without** LangChain. It models a workflow as a **graph of nodes** over a **shared state**, executed in discrete "super-steps" (Pregel-style message passing).

What you get for free once you adopt it:
- **Durable execution** — every step is checkpointed; a run can resume from its last state after a crash or a human pause.
- **Persistence** — short-term (thread) memory via checkpointers; long-term (cross-thread) memory via a store.
- **Streaming** — token/step/event streaming out of the box.
- **Human-in-the-loop** — `interrupt()` pauses indefinitely and resumes with a `Command`.

Use it when: multi-step process with real state, need for pause/resume, failure recovery, parallel fan-out, and observability over distinct testable steps. Skip it for a single LLM call.

```bash
pip install -U langgraph
# or: uv add langgraph
```

---

<<< SECTION: 2 Mental Model (state machine) [2-mental-model] >>>

# Mental Model

Three concepts:

- **State** — shared memory all nodes read/write. *Store raw data, not formatted text.* Nodes format prompts from raw state when they need to. This keeps debugging clean and lets the graph evolve without breaking state.
- **Nodes** — Python functions `(state) -> state-update`. Receive current state, do work, return a **partial** dict of updates (never mutate state in place).
- **Edges** — routing between nodes. Either static (`add_edge`) or dynamic (`add_conditional_edges`, or a `Command` returned from the node).

Design checklist:
1. Map the workflow into discrete steps → each becomes a node.
2. Classify each step: LLM step / data step (retry+cache) / action step (side effects, no cache) / user-input step (`interrupt()`).
3. Design state: include data that persists across steps; compute derivable data on demand.
4. Build nodes; pick error strategy per step (retry / loop-back-with-error / `interrupt()` / bubble up).
5. Wire it; declare possible destinations with `Command[Literal["a","b"]]` type hints.

> Idempotency: "If execution stops and later resumes, the affected node runs again from the start." Avoid duplicate side effects; `interrupt()` must be the first statement in its node (code before it re-runs on resume).

---

<<< SECTION: 3 State & Reducers [3-state-and-reducers] >>>

# State & Reducers

State schema can be a `TypedDict` (recommended), a `dataclass` (when you need defaults), or a Pydantic `BaseModel` (runtime validation on input; slower, and graph output isn't a model instance).

```python
from typing_extensions import TypedDict, Annotated
from langchain.messages import AnyMessage

class State(TypedDict):
    messages: list[AnyMessage]
    extra_field: int
```

A node returns updates; by default each key is **overwritten**:

```python
from langchain.messages import AIMessage

def node(state: State):
    return {"messages": state["messages"] + [AIMessage("Hello!")], "extra_field": 10}
```

## Reducers

A **reducer** controls how a node's update merges into the existing value for a key. Attach with `Annotated[type, reducer]`. Each key has its own reducer.

```python
import operator
from typing_extensions import Annotated

class State(TypedDict):
    # appends instead of overwriting — accumulate findings/learnings here
    learnings: Annotated[list[str], operator.add]
    visited: Annotated[list[str], operator.add]
```

Built-in `add_messages` (append + dedupe-by-id + format coercion) and the `MessagesState` convenience base:

```python
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages

class State(MessagesState):           # gives `messages: Annotated[list, add_messages]`
    documents: list[str]
```

### Custom reducer (override-or-append pattern)

This is exactly the `override_reducer` open_deep_research uses — append by default, or replace the whole list when passed `{"type": "override", "value": [...]}`:

```python
def override_reducer(current, new):
    if isinstance(new, dict) and new.get("type") == "override":
        return new.get("value", new)
    return operator.add(current, new)
```

### Bypassing a reducer (Overwrite)

```python
from langgraph.types import Overwrite

def replace(state: State):
    return {"messages": Overwrite(["replacement"])}   # or {"messages": {"__overwrite__": [...]}}
```

Only **one** node may overwrite a given key per super-step during parallel execution.

## Input / output / private schemas

Public input/output schemas can differ from the internal working state:

```python
class InputState(TypedDict):  question: str
class OutputState(TypedDict): answer: str
class OverallState(InputState, OutputState): pass

builder = StateGraph(OverallState, input_schema=InputState, output_schema=OutputState)
```

A node can also emit a **private** key (typed by its return) that only the next node reads — not exposed on the public schema. Useful for passing raw GraphRAG payloads between two nodes without polluting state.

---

<<< SECTION: 4 Nodes [4-nodes] >>>

# Nodes

Nodes are sync or async functions taking `state` and optionally `config` / `runtime`:

```python
from langgraph.runtime import Runtime

class Context(TypedDict):  graphrag_root: str   # non-state config

def my_node(state: State, runtime: Runtime[Context]):
    root = runtime.context["graphrag_root"]
    ...
    return {"learnings": [...]}
```

Add nodes via `builder.add_node(...)`. Async nodes use `async def` + `await`; invoke with `graph.ainvoke(...)`.

`runtime.context` carries per-run config (api bases, paths, budgets) without putting it in state. `runtime.store` exposes long-term memory. `runtime.execution_info` exposes `node_attempt`, `thread_id`, etc. (handy for fallbacks on retry).

---

<<< SECTION: 5 Edges, Conditional Edges (routing) [5-edges] >>>

# Edges & Conditional Edges

```python
from langgraph.graph import StateGraph, START, END

builder = StateGraph(State)
builder.add_node(step_1); builder.add_node(step_2)
builder.add_edge(START, "step_1")          # entry
builder.add_edge("step_1", "step_2")       # static
builder.add_edge("step_2", END)
graph = builder.compile()
# shorthand: StateGraph(State).add_sequence([step_1, step_2, step_3])
```

**Conditional edges** = a router function returning the next node name(s):

```python
from typing import Literal

def route_decision(state: State) -> Literal["llm_call_1", "llm_call_2", "llm_call_3"]:
    return {"story":"llm_call_1","joke":"llm_call_2","poem":"llm_call_3"}[state["decision"]]

builder.add_conditional_edges("router_node", route_decision,
    {"llm_call_1":"llm_call_1","llm_call_2":"llm_call_2","llm_call_3":"llm_call_3"})
```

A router may return a **list** of destinations (fan to several) and may return `END` to terminate (loop control):

```python
def route(state: State) -> Literal["b", END]:
    return "b" if len(state["aggregate"]) < 7 else END
builder.add_conditional_edges("a", route)
builder.add_edge("b", "a")    # loop a→b→a until END
```

> This is the backbone of **query classification → GraphRAG mode**: a node classifies, the conditional edge dispatches to the matching search node.

---

<<< SECTION: 6 Command (update + goto) [6-command] >>>

# Command — combine state update + control flow

Return a `Command` from a node to **both** update state and decide the next node. Type-hint destinations so the graph can validate/visualize.

```python
from langgraph.types import Command
from typing import Literal

def classify(state: State) -> Command[Literal["search_global", "search_local"]]:
    mode = router_llm.invoke(state["query"]).mode      # "global" | "local" | ...
    goto = "search_global" if mode == "global" else "search_local"
    return Command(update={"mode": mode}, goto=goto)
```

Navigate to a node in the **parent** graph from a subgraph:

```python
return Command(update={"foo": "bar"}, goto="other_node", graph=Command.PARENT)
```

`Command` also works inside tools (must include a `ToolMessage` in `messages`) and carries `resume=` for `interrupt()` continuation.

---

<<< SECTION: 7 Send (map-reduce / fan-out) [7-send] >>>

# Send — dynamic fan-out (map-reduce)

`Send(node_name, state)` dispatches a node **once per item**, each with its own input state — the count is decided at runtime. Combine with an accumulating reducer to gather results. This is how we run **N classified queries against GraphRAG in parallel**.

```python
from langgraph.types import Send
import operator

class OverallState(TypedDict):
    subqueries: list[dict]                          # [{query, mode, goal}, ...]
    learnings: Annotated[list[str], operator.add]   # gathered from all branches

def fan_out_queries(state: OverallState):
    return [Send("graphrag_search", {"subquery": q}) for q in state["subqueries"]]

builder.add_conditional_edges("generate_queries", fan_out_queries, ["graphrag_search"])
```

Each `graphrag_search` invocation returns `{"learnings": [...]}`; the reducer concatenates across all branches in the same super-step. The downstream node runs after all sends complete (use `defer=True` on the aggregator if branch lengths are uneven).

---

<<< SECTION: 8 Subgraphs [8-subgraphs] >>>

# Subgraphs

A subgraph is a compiled graph used as a node — for multi-agent decomposition or reuse.

**Shared state keys** → add the compiled subgraph directly:

```python
subgraph = subgraph_builder.compile()
builder.add_node("research_unit", subgraph)   # parent & subgraph share schema keys
```

**Different schemas** → wrap in a node that maps parent→sub→parent:

```python
def call_subgraph(state: State):
    out = subgraph.invoke({"bar": state["foo"]})
    return {"foo": out["bar"]}
```

Persistence modes via `.compile(checkpointer=...)` on the subgraph:

| Mode | Setting | Behavior |
|---|---|---|
| Per-invocation | `None` (default) | fresh each call; inherits parent checkpointer for interrupts within one call |
| Per-thread | `checkpointer=True` | state accumulates across calls on the same thread |
| Stateless | `checkpointer=False` | no checkpointing |

Inspect: `graph.get_state(config, subgraphs=True).tasks[0].state`.

> open_deep_research uses this for `supervisor_subgraph` → `researcher_subgraph`. We may or may not need this layer (see §13).

---

<<< SECTION: 9 Workflow Patterns (routing, orchestrator-worker) [9-patterns] >>>

# Workflow Patterns

LangGraph's own docs ship these patterns. Two matter directly for us.

## Routing (classify → dispatch)

Classify the input with **structured output**, then conditional-edge to the right handler. *This is the user's "cada query → classificação (global/local/drift/basic)" idea, verbatim.*

```python
from pydantic import BaseModel, Field
from typing_extensions import Literal

class Route(BaseModel):
    step: Literal["global", "local", "drift", "basic"] = Field(
        description="Which GraphRAG search mode best answers this query")

router = llm.with_structured_output(Route)

def llm_call_router(state: State):
    decision = router.invoke([
        SystemMessage(content="Classify the query into a GraphRAG search mode."),
        HumanMessage(content=state["input"])])
    return {"decision": decision.step}

def route_decision(state: State):
    return {"global":"search_global","local":"search_local",
            "drift":"search_drift","basic":"search_basic"}[state["decision"]]

builder.add_conditional_edges("llm_call_router", route_decision, {...})
```

## Orchestrator–Worker (plan → Send workers → synthesize)

Orchestrator plans subtasks it can't know upfront; `Send` spawns a worker per subtask; a synthesizer merges. This is the shape of a **single research round** (plan queries → search each → synthesize).

```python
class State(TypedDict):
    topic: str
    sections: list                                  # plan
    completed: Annotated[list, operator.add]        # worker outputs
    final: str

def orchestrator(state): return {"sections": planner.invoke(...).sections}
def worker(state):       return {"completed": [llm.invoke(...).content]}
def synthesizer(state):  return {"final": "\n\n---\n\n".join(state["completed"])}
def assign(state):       return [Send("worker", {"section": s}) for s in state["sections"]]

b = StateGraph(State)
b.add_node("orchestrator", orchestrator); b.add_node("worker", worker)
b.add_node("synthesizer", synthesizer)
b.add_edge(START, "orchestrator")
b.add_conditional_edges("orchestrator", assign, ["worker"])
b.add_edge("worker", "synthesizer"); b.add_edge("synthesizer", END)
```

Other patterns: **prompt chaining** (sequential gates), **parallelization** (fixed fan-out → aggregate), **evaluator-optimizer** (generate ⇄ critique loop until criteria met).

---

<<< SECTION: 10 Agents & ToolNode [10-agents] >>>

# Agents & ToolNode

For the autonomous (ReAct) style, `ToolNode` executes tool calls (parallel exec, error handling, state injection):

```python
from langgraph.prebuilt import ToolNode
from langchain.tools import tool, ToolRuntime

@tool
def graphrag_query(query: str, mode: str) -> str:
    """Answer a query against the Bible knowledge graph in the given mode."""
    return run_graphrag(query, mode)

builder.add_node("tools", ToolNode([graphrag_query]))
```

Tools can read graph state / runtime context via an injected `ToolRuntime[Context, State]` argument (e.g. pull `thread_id`, user, or the graphrag root from context). A tool may also return a `Command` to update state directly.

> Decision point (see §13): expose GraphRAG as a **tool** the LLM calls (ReAct), or as an **explicit node** the graph routes to. Explicit nodes give tighter cost control; tools give the model autonomy.

---

<<< SECTION: 11 Persistence (checkpointer + store) [11-persistence] >>>

# Persistence

Two layers:
- **Checkpointer** — persists a thread's state as checkpoints (short-term, thread-scoped). Enables resume, time-travel, HITL, fault tolerance.
- **Store** — application data outside graph state (long-term, cross-thread): user prefs, cached facts.

```python
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

graph = builder.compile(checkpointer=InMemorySaver(), store=InMemoryStore())
graph.invoke({"messages": [{"role":"user","content":"..."}]},
             {"configurable": {"thread_id": "thread-1"}})
```

Production checkpointers: `SqliteSaver` (dev/file), `PostgresSaver` / `AsyncPostgresSaver`. (A Mongo-backed saver is also viable — relevant given the MVP's MongoDB store.)

Introspection / time-travel: `graph.get_state(config)`, `graph.get_state_history(config)`, `graph.update_state(config, values)`, replay from a past `checkpoint_id`.

Keep `thread_id` < 255 chars on Postgres; prune old checkpoints to bound growth.

---

<<< SECTION: 12 Operational (retries, timeouts, cache, recursion, HITL) [12-operational] >>>

# Operational Controls

```python
from langgraph.types import RetryPolicy, CachePolicy
from langgraph.checkpoint.memory import InMemoryCache

builder.add_node("search", search_fn, retry_policy=RetryPolicy(max_attempts=3))
builder.add_node("model", call_model, timeout=120.0)                  # async nodes only
builder.add_node("expensive", fn, cache_policy=CachePolicy(ttl=120))  # needs compile(cache=...)
graph = builder.compile(cache=InMemoryCache())
```

- **Retry**: retries most exceptions except `ValueError`/`TypeError`/etc.; for HTTP, only 5xx. Custom: `RetryPolicy(retry_on=ConnectionError)`.
- **Recursion limit**: default 1000 super-steps; `graph.invoke(x, {"recursion_limit": 25})`; catch `GraphRecursionError`. `RemainingSteps` in state lets a router exit gracefully before the limit.
- **HITL**: `interrupt(payload)` pauses (needs a checkpointer); resume with `Command(resume=...)`. Must be the first statement in its node.
- **Error handler** (langgraph ≥ 1.2): `add_node(..., error_handler=fn)` runs after retries exhaust → compensation/saga routing.

Visualize: `graph.get_graph().draw_mermaid()` / `draw_mermaid_png()`.

---

<<< SECTION: 13 Bridge → Deep Researcher over GraphRAG [13-bridge] >>>

# Bridge → Deep Researcher over GraphRAG

How the three references combine into our target system.

| Source | What we take |
|---|---|
| **dzhng/deep-research** | the recursive `breadth × depth` loop; accumulate `learnings[]` + `sources[]`; `generateSerpQueries` (query + researchGoal); `processResult` → learnings + follow-up questions; breadth halves each level |
| **open_deep_research** | optional supervisor/researcher/compress layer; clarify step; per-result compression; structured outputs; budget caps (`max_concurrent`, `max_iterations`) |
| **LangGraph** | `StateGraph` + accumulating reducers (§3); **routing** (§5/§9) for query classification; **Send** (§7) for parallel GraphRAG calls; checkpointer (§11) for resumable runs; recursion/budget limits (§12) |

**The swap that makes it "ours":** replace web search (Tavily/Firecrawl) with **GraphRAG queries**. Each generated sub-query is first **classified** into a search mode and routed to the matching GraphRAG call:

```
question
  → [clarify?]                      (odr / dzhng generateFeedback)
  → write_brief
  → generate_queries                → [{query, goal}, ...]     (dzhng generateSerpQueries)
  → classify_query  (per query)     → global | local | drift | basic
       └─ reuse the existing LLM method-router
  → Send fan-out → graphrag_search  → run the right GraphRAG mode, in parallel  (§7)
  → process_results                 → learnings[] + follow_up_questions[]       (dzhng)
  → depth-- ? loop (next queries = follow_ups, breadth/2) : compress
  → final_report
```

Open design decisions (for the TL):
1. **Engine shape** — recursive depth/breadth loop (dzhng-style, simpler/cheaper) vs supervisor+researcher subgraphs (odr-style, heavier parallel decomposition).
2. **Classification placement** — fold mode into `generate_queries` (one structured call emits `{query, mode, goal}`) vs a dedicated `classify` node (more accurate, +1 LLM call). Either way, reuse the existing LLM method-router.
3. **GraphRAG as node vs tool** — explicit routed node (tight cost control) vs ReAct tool the model calls (more autonomy). §10.
4. **Cost governance** — global search is the expensive mode; the classifier doubles as a **cost governor** by steering toward local/basic when sufficient. Add `max_depth`, `max_breadth`, `max_concurrent`, and a per-run global-search budget.
5. **LangGraph adoption** — add `langgraph` as a dep (gets persistence/streaming/HITL/checkpointing) vs a thin native recursive loop. A Mongo checkpointer could align with the MVP store.
