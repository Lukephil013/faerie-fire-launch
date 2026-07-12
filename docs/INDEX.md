# Documentation Index

Use the narrowest source that answers the question:

| Document | Status | Purpose |
|---|---|---|
| `AGENTS.md` | Current, authoritative | Agent routing, runtime paths, invariants, verification |
| `CLAUDE.md` | Current, authoritative | Claude Code bootstrap pointing to the shared agent sources |
| `docs/HANDOFF.md` | Current, generated | Latest Git state and exact startup command for the next agent |
| `docs/LIFECYCLE.md` | Current | End-to-end cultivation lifecycle and consent gates |
| `docs/GOALS_CURIOSITIES.md` | Current | Practical model for Soul/Roots/Branches/Leaves, Investigations, GoalAI reviews, and proposals |
| `docs/GOALS_INVESTIGATIONS_WALKTHROUGH.md` | Current | Step-by-step examples for using Goals, Investigations, Review Node, proposals, and Leaves |
| `docs/UPWARD_SPIRAL_PLAN.md` | Current | Phase ledger, product contracts, acceptance criteria, and user-feedback checkpoints for the evolving person-model loop |
| `docs/UPWARD_SPIRAL_IMPLEMENTATION.md` | Current | Implemented upward-spiral architecture, lifecycle diagrams, authority map, cadence, controls, and limitations |
| `README.md` | Current | Installation and everyday operation |
| `FEATURES.md` | Current | Detailed implemented behavior and configuration |
| `config.example.toml` | Current | Supported user configuration examples |
| `bats/README.md` | Current | Windows launcher guide |
| `living_computer_design.md` | Historical | Original architecture and product intent |
| `devlog/` | Historical | Chronological implementation record |
| `Faerie_Fire_Overview.*` | Snapshot | Shareable overview, not an implementation reference |
| `Faerie_Fire_Dossier.pdf` | Snapshot | Long-form project artifact, not an implementation reference |

For code context, run `python tools/project_context.py <area>`. Available areas:
`capture`, `triage`, `companion`, `filing`, `review`, `storage`, and `diagnostics`.

For privacy-safe prompt size estimates, run:

```powershell
python tools/project_context.py triage --tokens
```
