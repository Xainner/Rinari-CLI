> **Archivado el 2026-10-12.** Análisis puntual de septiembre, ya resuelto; no es una regla ni una tarea pendiente. Índice: [README.md](README.md). Su contenido sigue vigente y está en [tools.md](../tools.md#alcance-de-lectura-en-plan-y-review).

# PLAN/REVIEW read scope

PLAN and REVIEW always use an immutable execution profile: filesystem writes and
shell/process execution are denied. The persisted permission selection separately
controls ordinary filesystem reads: full-access allows external reads, workspace
requests approval outside the session root, and read-only limits reads to the root.
Sensitive credential reads retain their explicit approval gate; engine-private
storage remains inaccessible. An external read approval extends the sandbox only
for that tool invocation.

The protocol advertises plan_read_scope_v1. Session summaries expose
read_permission_profile separately from effective_permission_profile. Desktop
clients must not present the execution profile as the selected read scope.
