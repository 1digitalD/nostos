# Nostos — project reference

Nostos is the planned rebuild of `apartment-hunt` as a configurable, publishable
rental-search tool. These documents are the durable record of what it is, why it is
shaped this way, and how to build it.

They are written to be **copied wholesale into the new `nostos` repository** when it
is created. Nothing here depends on living in `apartment-hunt`.

Status: **design complete, implementation not started.** Name and licence confirmed 2026-08-25.

---

## Reading order

| Doc | Read it when |
|---|---|
| [01-vision.md](01-vision.md) | You need to know what this is for and who it serves |
| [02-architecture.md](02-architecture.md) | You are about to write or review any module |
| [03-data-model.md](03-data-model.md) | You touch a listing, a field, or the store |
| [04-config.md](04-config.md) | You touch a citypack, a profile, or anything user-facing |
| [05-stack.md](05-stack.md) | You are adding a dependency |
| [06-roadmap.md](06-roadmap.md) | You are deciding whether something is in scope now |
| [07-porting.md](07-porting.md) | You are moving code across from `apartment-hunt` |
| [08-decisions.md](08-decisions.md) | You want to reopen a decision, or wonder why something is odd |
| [09-r1-tasks.md](09-r1-tasks.md) | You are implementing release one |

---

## Agent brief — start here in a fresh session

If you are an agent picking this project up cold, read in this order and stop:

1. **This file.**
2. **`02-architecture.md`** — the four principles at the top are binding.
3. **`03-data-model.md`** — the record is the keystone; almost every bug will be a
   provenance or precedence bug.
4. **The task you were given** from `09-r1-tasks.md`.

Do **not** read the whole doc set before starting. Each task in `09-r1-tasks.md`
names exactly which sections it needs.

### Non-negotiables

These are the rules that most often get broken by someone working fast. Violating any
of them is a defect regardless of whether tests pass:

- **The pipeline is deterministic.** No model call in the scheduled run path.
  Models operate at the edges only — perception in (photos, fallback extraction),
  authoring out (drafting adapters and citypacks for human review).
- **Every field carries provenance.** Never store a bare value. See `03-data-model.md`.
- **Absence is typed.** `NOT_STATED` and `NOT_APPLICABLE` are different facts. Never
  collapse them to `None`.
- **The citypack owns the neighbourhood vocabulary.** `area_key` is a validated string,
  never an enum. This is the specific bug that made the old codebase single-city.
- **All capability lives in the library.** The CLI, MCP server, UI and scheduler are
  callers. None may do anything the library cannot.
- **Nothing paid runs without consent.** Estimate, ask, cap. The free provider is the
  default for every paid capability.
- **Listing facts are replaceable; user state is not.** Archival never touches a
  listing someone shortlisted, contacted or viewed.
- **No credentials in config files.** OS keychain, referenced by name.

### Working rules

- Port tests before the code they cover. The ported suite is the specification.
- Parsers are pure functions over recorded fixtures. A source test that needs the
  network is a broken test.
- Keep changes scoped to the task. Do not refactor adjacent code opportunistically.
- If a decision in `08-decisions.md` looks wrong, say so — do not silently work around it.

---

## Provenance of these documents

Produced from a design session on 2026-08-25 covering: an adversarial review of the
existing `apartment-hunt` codebase at `1137c9c`, a target architecture, a features-first
roadmap, a YC-style product diagnostic (via the `office-hours` framework from
[garrytan/gstack](https://github.com/garrytan/gstack)), and a full solution design.

Findings cite files and line numbers as read at that commit.
