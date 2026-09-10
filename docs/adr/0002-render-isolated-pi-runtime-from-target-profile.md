# Render an isolated Pi runtime from the TargetProfile

The first real Target is Pi 0.84.1 with `ark-agent-plan/glm-5.3-flash:max`. Mounting the host Pi home would introduce mutable provider configuration, unrelated credentials, sessions, skills, and extensions.

ACO will instead add a narrow Pi adapter at Harbor's existing agent seam. It renders frozen non-sensitive provider/model configuration, resolves `ark-agent-plan-main` from `ARK_AGENT_PLAN_API_KEY`, injects only that credential, disables undeclared skills and extensions, and records the rendered CLI selection. The host `~/.pi/agent` is never mounted; Harbor retains container execution and ACO retains termination, sealing, and scoring.
