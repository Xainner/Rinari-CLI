-- 0061: plan de conectores C4. A project-scope MCP server can belong to one
-- project folder (the canonical root it was accepted in), so a server that a
-- folder's .mcp.json brought never runs in another project. Older project
-- rows keep an empty root and stay visible in every trusted project.
-- mcp_project_offers keeps the owner's answer about each server a trusted
-- folder brings (.mcp.json, .vscode/mcp.json…), so the question is asked
-- once per server. The fingerprint covers the non-secret shape of the
-- definition (transport, command, URL without its query values, env and
-- header names): a changed definition is offered again. No secret is kept.
-- NOTE: no semicolons inside comments, the runner splits on them.
ALTER TABLE mcp_servers ADD COLUMN project_root TEXT NOT NULL DEFAULT '';

CREATE TABLE mcp_project_offers (
    project_root TEXT NOT NULL,
    server_key TEXT NOT NULL,
    source TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    server_name TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL,
    PRIMARY KEY (project_root, server_key)
);
