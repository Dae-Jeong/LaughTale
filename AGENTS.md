# Laughtale Agent Entry

1. Follow the machine-wide rules in `/Users/marin/AGENTS.md` first.
2. Use [`README.md`](README.md) as the project context for product, architecture, and infrastructure work.
3. Future ideas in the context below are reference only. Do not scaffold or install them until the admin asks.
4. If work drifts from the stated purpose, give a brief reminder. The admin's explicit current decision takes precedence.
5. Before implementing a significant feature or architecture decision, follow [`.agents/skills/spec-driven-development/SKILL.md`](.agents/skills/spec-driven-development/SKILL.md). Respect that Skill's declared exceptions for trivial, self-contained changes.
6. Follow the document lifecycle in [`docs/README.md`](docs/README.md): [Obsidian Operations](../Obsidian/Operations/laughtale/index.md) owns idea and stable task documents; repo `.ideas/` and `tasks/` paths are compatibility facades, while `docs/` owns approved project rules and implemented, verified project truth.
7. Project override for spec-driven work: combine specification, plan, task checklist, and diagrams in one named `Operations/Laughtale/tasks/<work>.md`; the repo `tasks/<work>.md` path is its compatibility facade. Do not create root `SPEC-*`, generic `tasks/plan.md`, or generic `tasks/todo.md` unless the admin explicitly requests separate artifacts.
8. Use [`.agents/skills/grilling/SKILL.md`](.agents/skills/grilling/SKILL.md) only when the admin explicitly asks to grill or stress-test a consequential decision. Run it before approving the task specification, and record only settled decisions in the existing canonical `Operations/Laughtale/tasks/<work>.md`; do not create a separate grill session, `CONTEXT.md`, or ADR tree.
9. Use [`.agents/skills/project-status/SKILL.md`](.agents/skills/project-status/SKILL.md) only when the admin explicitly asks to see project status, ideas, or tasks, including a direct `$project-status` invocation. Do not run it during unrelated work or as a routine progress update.
10. Start design, implementation, and review at [the document routing table](docs/README.md#작업별-읽기). Backend rules come from the pinned `external/backend-template` submodule; read only the relevant owner documents and applicable local supplements. Do not edit the dependency or silently substitute another checkout or remote main.
11. The project's [TDD applicability override](docs/README.md#tdd-적용-상태) takes precedence over downstream implementation instructions in installed skills. Consult it before following a skill's implementation phase.

## Context to remember

For PM coordination or cross-role work, read [the orchestration binding](docs/orchestration.md).
It routes to the shared model; product code and runtime rules remain in this repo, while idea and stable task document ownership is in `Operations/Laughtale/`.

- Grow one product gradually; do not front-load infrastructure or empty services.
- Treat each backend service as an independent project that may use FastAPI, Spring Boot, or another suitable stack.
- Infrastructure learning interests include Kubernetes scaling, high-traffic reliability, database replication, Elasticsearch, and Kafka.
- Capacity planning should connect measured throughput and demand assumptions to monthly infrastructure cost scenarios.
- These interests are reference context, not current implementation requirements.
