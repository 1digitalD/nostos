# Lightweight OSS agent substrate options

**Status:** decision research, not an implementation plan  
**Verified:** 2026-09-06 (America/Vancouver)  
**Sources:** official documentation and official repositories only

## Executive recommendation

Nostos does **not** need a full agent platform today. Its lack of a chat interface is an advantage: entry points can remain deterministic (manual action, listing refresh, scheduled batch, or a narrowly typed domain event), while the model is used only inside a bounded job.

No reviewed open-source framework covers the whole desired substrate well: versioned skills/prompts/context, direct and MCP tools, bounded model loops, durable scheduling and events, arbitrary workflows, cancellation and idempotency, evaluations, observability, and application integration. Products that come closest either focus on the model loop (PydanticAI, OpenAI Agents SDK), the agent state machine (LangGraph, Burr), or durable job orchestration (Temporal, Hatchet, Prefect, Dagster). Combining two overlapping agent-loop frameworks would create more complexity than value.

The recommended starting composition is:

1. **PydanticAI** for the typed, bounded agent loop, model abstraction, structured output, direct Python tools, MCP tools, usage limits, message history, and code-first evaluations.
2. **A very small application-owned substrate** for versioned skill manifests, prompt/context assembly, permission policy, run records, input/context hashes, idempotency keys, cancellation flags, and deterministic trigger adapters.
3. **SQLite first** for local run/event persistence. Keep the run-store and dispatcher interfaces replaceable.
4. **Existing Nostos scheduling/events first.** A trigger should only submit a typed run; it should not contain agent logic.
5. Add a durable orchestrator only after a measured need:
   - **Hatchet** for distributed background jobs, event triggers, retries, concurrency controls, cancellation, and compact DAGs.
   - **Prefect** instead when the work is primarily observable enrichment/data pipelines; PydanticAI has an official Prefect durable-execution integration.
   - **Temporal** when restart-safe, long-running or human-paused workflows become a core reliability requirement and justify a stronger programming and operational model.

Do not add LangGraph or Burr alongside PydanticAI initially. If explicit graph/state-machine behavior becomes the central abstraction, evaluate **replacing** the PydanticAI loop with LangGraph or Burr for that workload.

## The smallest useful substrate

The first reusable package should expose only these contracts:

- `SkillSpec`: stable ID, semantic version, instructions/prompt version, input/output schemas, requested capabilities, evaluator set, and context policy.
- `ToolSpec`: typed direct-call or MCP adapter, side-effect class, timeout, retry safety, and permission requirement.
- `RunSpec`: skill/version, input, context snapshot/digest, tool grants, model configuration, deadline, step/token/cost limits, idempotency key, and trigger metadata.
- `RunEvent` and `RunResult`: append-only progress, tool attempts, usage, evidence/provenance, terminal status, and structured output.
- `Runner`: start, inspect, cancel, and resume/retry a run.
- `TriggerAdapter`: manual, schedule, or domain event into the same `RunSpec` submission path.
- `Evaluator`: deterministic assertions first, model-judged checks only where necessary, with an offline regression dataset.

This is application-agnostic. Nostos contributes skills, domain tools/evaluators, permissions, and presentation. Its existing MCP server can be one tool transport; same-process Python calls should remain available because they are simpler, faster, and easier to authorize locally.

The initial runner should be deliberately modest: one process, one agent job at a time, bounded retries, cooperative cancellation between model/tool steps, and recovery of queued jobs after restart. It should not claim exactly-once execution of arbitrary external side effects; tool implementations need idempotency keys or application-level reconciliation.

## Capability comparison

Legend: **Strong** means the capability is a primary, documented OSS concern; **Partial** means useful primitives exist but application work remains; **No** means it is outside the framework's purpose.

| Option | Agent loop / typed output | Direct / MCP tools | Graph or workflow | Durable runs, retries, cancellation | Schedule / events | Evals / observability | Fit for Nostos |
|---|---|---|---|---|---|---|---|
| **PydanticAI** | Strong | Strong / Strong | Partial; optional typed graph | Partial alone; official durable backends | No native trigger service | Strong code-first evals; OpenTelemetry/Logfire integration | **Best starting loop** |
| **LangGraph** | Strong stateful graph | Strong / available via LangChain ecosystem | Strong cyclic graph | Strong checkpoint/durable graph semantics | Partial; deployment layer or external scheduler | Partial OSS tracing hooks; broader LangSmith features are separate | Use when explicit graph state is central |
| **Temporal** | No | No | Strong durable workflows | **Strongest** | Strong schedules/signals/updates | Operational visibility, not AI evals | Reliability escalation, not first dependency |
| **Prefect** | No | No | Strong Python flows/tasks | Strong workflow retries/state/cancellation | Strong schedules/events/automations | Strong operational UI/telemetry, not AI evals | Good for enrichment/data pipelines |
| **Dagster** | No | No | Strong asset/job orchestration | Strong for data jobs | Strong schedules/sensors | Strong data observability, not AI evals | Too data-platform-shaped for the first slice |
| **DSPy** | Partial modules/ReAct | Partial | No durable workflow engine | No | No | **Strong optimization/evaluation focus** | Optional offline prompt/program optimization later |
| **OpenAI Agents SDK** | Strong | Strong / Strong | Partial handoffs/agents-as-tools | Partial resumable state; no trigger service | No | Strong tracing; evals are a separate concern | Good if deliberately OpenAI-first |
| **Apache Burr** | Strong explicit state machine | Partial/application-defined | Strong state transitions | Strong persistence/checkpointing; not a full distributed scheduler | No native general trigger service | Strong lifecycle tracking/UI | Attractive lighter state-machine alternative |
| **Hatchet** | No built-in model loop | Application-defined | Strong durable tasks/workflows | Strong | Strong event/cron triggers | Strong operational observability, not AI evals | Best later middleweight job orchestrator |

## Primary candidates

### PydanticAI — recommended agent layer

PydanticAI's `Agent` packages developer instructions, function tools/toolsets, structured output types, dependencies, model settings, and reusable capabilities. It supports bounded programmatic runs and iteration over the underlying graph ([agent documentation](https://pydantic.dev/docs/ai/core-concepts/agent/)). Function tools can be ordinary Python functions, while toolsets collect direct or third-party tools ([tools documentation](https://pydantic.dev/docs/ai/tools-toolsets/tools/)). Its MCP client supports local stdio, Streamable HTTP, and in-process servers; an in-process MCP server avoids a network round trip ([MCP client documentation](https://pydantic.dev/docs/ai/mcp/client/)).

PydanticAI now documents a stable durable-execution backend interface and official integrations with Temporal, DBOS, Prefect, Restate, and AWS Lambda durable functions. The stated purpose is preserving progress through transient failures, application errors, and restarts, including long-running and human-in-the-loop work ([durable execution overview](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)). This makes it possible to start light without closing the durability seam.

Pydantic Evals is code-first and supports datasets/cases, deterministic and model-based evaluators, serialized reports, retry strategies, and span-based evaluation over OpenTelemetry traces ([Pydantic Evals](https://pydantic.dev/docs/ai/evals/evals/)). The optional `pydantic-graph` package offers typed graphs and finite-state machines, but its own documentation warns that graphs add setup and are unnecessary for many jobs ([graph documentation](https://pydantic.dev/docs/ai/graph/graph/)).

**Gaps and risks:** it is not a scheduler or distributed queue; it does not supply the application-level skill registry, prompt release process, authorization policy, or durable run product. Its API has evolved rapidly, so pin versions and protect the application behind a small adapter. Logfire is optional and separate; do not make a hosted observability service mandatory for local Nostos.

**Status/license:** Python, MIT, active and not archived. The official repository reported release `v2.40.0` on 2026-09-05 ([repository](https://github.com/pydantic/pydantic-ai), [release](https://github.com/pydantic/pydantic-ai/releases/tag/v2.40.0), [license](https://github.com/pydantic/pydantic-ai/blob/main/LICENSE)).

### Hatchet — recommended later middleweight orchestrator

Hatchet is a server-backed orchestration engine for background tasks, event-driven work, and durable workflows, with Python support. Its official material covers workflows/tasks, retries, timeouts, concurrency controls, cancellation, cron and event triggers, and DAG-style composition ([documentation](https://docs.hatchet.run/), [repository](https://github.com/hatchet-dev/hatchet)). That maps closely to the substrate's eventual trigger and run-lifecycle needs without pretending to be the model loop.

**Gaps and risks:** Hatchet requires operating or buying a separate orchestration control plane and persistence layer. It does not own prompt/context versioning or AI evaluation. PydanticAI did not list Hatchet among its official durable backends at verification time, so integration would wrap a PydanticAI run as a Hatchet task rather than use a maintained first-party adapter. The project is younger and releases rapidly; pin both server and SDK versions and prove cancellation, retries, and upgrade behavior in a small deployment before committing.

**Status/license:** Go server with Python SDK support, MIT, active and not archived. The official repository reported release `v0.105.16` on 2026-08-31 ([release](https://github.com/hatchet-dev/hatchet/releases/tag/v0.105.16), [license](https://github.com/hatchet-dev/hatchet/blob/main/LICENSE)).

### Temporal — strongest durability, currently too heavy

Temporal provides durable workflow execution through replayable workflow code and separately retried activities. Its official documentation covers Python workflows/activities, retry and timeout policies, cancellation, signals/updates, child workflows, schedules, and visibility ([Python developer guide](https://docs.temporal.io/develop/python), [schedules](https://docs.temporal.io/schedule)). It is the strongest reviewed option for workflows that must survive worker/process failures or pause for long periods.

PydanticAI maintains an official Temporal integration ([PydanticAI Temporal integration](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/)).

**Gaps and risks:** Temporal is not an agent, prompt, tool, or eval framework. Workflow code must obey Temporal's deterministic execution constraints, with network/model/tool work isolated in activities. Self-hosting adds a server and persistence operational burden; Cloud adds cost and vendor dependence. This is justified for consequential, long-running, cross-service workflows—not for a local deterministic listing-enrichment trigger.

**Status/license:** the Temporal server repository is Go, MIT, active and not archived; the Python SDK is separately maintained. The server reported release `v1.31.2` on 2026-07-08 ([server repository](https://github.com/temporalio/temporal), [release](https://github.com/temporalio/temporal/releases/tag/v1.31.2), [server license](https://github.com/temporalio/temporal/blob/main/LICENSE), [Python SDK](https://github.com/temporalio/sdk-python)). Hosted-service terms are separate from the OSS license.

### Prefect — best when this becomes a data/enrichment pipeline

Prefect turns Python functions into observable flows and tasks and documents retries, caching, timeouts, concurrency, deployments, schedules, events, automations, and cancellation ([flows](https://docs.prefect.io/v3/concepts/flows), [tasks](https://docs.prefect.io/v3/concepts/tasks), [schedules](https://docs.prefect.io/v3/automate/add-schedules), [events and automations](https://docs.prefect.io/v3/automate/events)). PydanticAI maintains an official Prefect durable-execution integration ([integration](https://pydantic.dev/docs/ai/capabilities/durable_execution/prefect/)).

**Gaps and risks:** Prefect orchestrates Python work; it does not define agent tools, skills, prompt/context releases, or AI quality. A server/cloud deployment and worker model would be premature for one local periodic job. Its strengths become relevant when Nostos has multiple observable enrichment/backfill pipelines and operators need reruns and scheduling UI.

**Status/license:** Python, Apache-2.0, active and not archived. The official repository reported release `3.8.5` on 2026-09-03 ([repository](https://github.com/PrefectHQ/prefect), [release](https://github.com/PrefectHQ/prefect/releases/tag/3.8.5), [license](https://github.com/PrefectHQ/prefect/blob/main/LICENSE)). Prefect Cloud terms and features are separate from the OSS repository.

### LangGraph — strongest agent graph, but overlapping

LangGraph is a low-level framework for long-running, stateful agents. Its official OSS documentation emphasizes graph state, persistence/checkpoints, durable execution, interrupts, and human-in-the-loop control ([overview](https://docs.langchain.com/oss/python/langgraph/overview), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)). It is a better fit than PydanticAI when explicit nodes, transitions, cycles, checkpointed thread state, and pause/resume behavior are the application's central model.

**Gaps and risks:** it still needs an external trigger/job service for deterministic schedules and distributed run operations. Prompt/skill release management and domain evals remain application concerns. The broader LangSmith observability/evaluation/deployment offering is distinct from the MIT LangGraph library, so OSS and hosted capabilities must not be conflated. LangGraph also overlaps substantially with PydanticAI's agent loop and optional graph; adopting both at the core would create two run-state models.

**Status/license:** Python, MIT, active and not archived. The monorepo's latest GitHub release label was SDK-oriented (`sdk==0.4.4`, 2026-08-27), so package-specific versioning should be checked when pinning ([repository](https://github.com/langchain-ai/langgraph), [releases](https://github.com/langchain-ai/langgraph/releases), [license](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)).

### Apache Burr — compelling lightweight state-machine alternative

Burr models applications as actions and explicit state transitions, with persistence, tracking, lifecycle hooks, streaming, and a local UI. Its project describes running decision-making applications on one's own infrastructure ([official site](https://burr.apache.org/), [repository](https://github.com/apache/burr)). It is conceptually clean for deterministic hosts around selectively agentic steps and is lighter than adopting a broad ecosystem.

**Gaps and risks:** Burr is a state-machine/application framework, not a distributed scheduler or complete durable job service. Direct/MCP tool conventions, prompt/skill packaging, permissions, idempotent external effects, and evaluation need to be supplied. It is currently an Apache incubating project, which is a governance/maturity caveat even though activity is healthy.

**Status/license:** Python, Apache-2.0, active and not archived; release `v0.43.0-incubating` was published 2026-08-29 ([release](https://github.com/apache/burr/releases/tag/v0.43.0-incubating), [license](https://github.com/apache/burr/blob/main/LICENSE)).

## Concise treatment of the other required options

### OpenAI Agents SDK

The SDK provides agents, function tools, agents-as-tools/handoffs, guardrails, sessions, human-in-the-loop support, MCP, tracing, and run-state serialization/resumption ([official docs](https://openai.github.io/openai-agents-python/), [running agents](https://openai.github.io/openai-agents-python/running_agents/), [MCP](https://openai.github.io/openai-agents-python/mcp/), [tracing](https://openai.github.io/openai-agents-python/tracing/)). It is a credible alternative to PydanticAI for an intentionally OpenAI-first substrate. It is not a scheduler or general durable workflow service, and provider portability is not its primary design center. Python, MIT, active; release `v0.22.0` on 2026-08-19 ([repository](https://github.com/openai/openai-agents-python), [release](https://github.com/openai/openai-agents-python/releases/tag/v0.22.0), [license](https://github.com/openai/openai-agents-python/blob/main/LICENSE)).

### DSPy

DSPy is strongest as a framework for declarative LM modules and optimizing them against metrics and examples, including tool-using patterns such as ReAct ([official docs](https://dspy.ai/), [optimizers](https://dspy.ai/learn/optimization/optimizers/), [evaluation](https://dspy.ai/learn/evaluation/metrics/)). It is not a persistent job runner, schedule/event service, or durability layer. Introduce it later only if a real evaluation set shows that systematic prompt/program optimization pays for its additional abstraction. Python, MIT, active; release `3.3.1` on 2026-08-21 ([repository](https://github.com/stanfordnlp/dspy), [release](https://github.com/stanfordnlp/dspy/releases/tag/3.3.1), [license](https://github.com/stanfordnlp/dspy/blob/main/LICENSE)).

### Dagster

Dagster is a mature data orchestrator built around assets, jobs, schedules, sensors, partitions, backfills, retries, and operational observability ([official docs](https://docs.dagster.io/), [schedules](https://docs.dagster.io/guides/automate/schedules), [sensors](https://docs.dagster.io/guides/automate/sensors)). It is excellent when the reusable product is a governed data platform; it supplies no agent loop, MCP layer, prompt/context registry, or AI eval machinery. For the stated lightweight substrate, its asset-centric model and deployment footprint are unnecessary. Python, Apache-2.0, active; release `1.13.21` on 2026-09-03 ([repository](https://github.com/dagster-io/dagster), [release](https://github.com/dagster-io/dagster/releases/tag/1.13.21), [license](https://github.com/dagster-io/dagster/blob/master/LICENSE)). Dagster+ and other hosted/commercial terms are separate.

## Answer to the framing question

The proposed framing is correct with two adjustments:

1. **Skills are versioned application artifacts, not the security boundary.** A skill may request tools and context; a host policy grants them. Treat prompt, skill, model, tool schema, evaluator, and context-builder versions as part of every run's provenance.
2. **DAG orchestration is optional.** A bounded agent loop is inherently cyclic. Deterministic preprocessing/postprocessing can remain ordinary code; a DAG or durable workflow is warranted only when there are independently retryable steps, parallel branches, cross-process waits, or operator checkpoints.

There is no need to build a chat system, generalized workflow editor, plugin marketplace, shared vector memory, or multi-agent supervisor. Deterministic triggers should enqueue the same typed run used by a manual request. Context should be assembled fresh from explicit sources; durable memory should be a separate opt-in capability with ownership, correction, and retention rules.

## Adoption gates

Adopt PydanticAI after a small spike proves:

- one versioned skill runs through direct Python tools and the existing Nostos MCP surface;
- step/token/time limits terminate correctly;
- structured output and evidence references are validated;
- cancellation works between model and tool boundaries;
- a repeated event with the same idempotency key does not publish twice;
- the same regression dataset runs with a fake model and one real configured provider;
- disabling AI leaves normal Nostos behavior intact.

Do not add an orchestrator until one of these is true:

- jobs must survive process/host restarts while in progress;
- multiple workers or applications submit concurrently;
- delayed retries or pauses exceed the local process lifetime;
- operators need centralized queue visibility and cancellation;
- a workflow has independently retryable or parallel stages.

At that point, timebox a Hatchet and Prefect comparison using the same runner contract. Choose Temporal only if replay-based durability and long-lived coordination are requirements rather than aspirations.

## Bottom line

Build the lightweight substrate, but **do not build all of its eventual components now**. PydanticAI plus small application-owned contracts covers the current agentic need and leaves clean seams for MCP, evaluations, and later durable execution. Keep triggers deterministic and thin. Add Hatchet, Prefect, or Temporal only when operational evidence makes persistence and orchestration a separate problem worth owning.
