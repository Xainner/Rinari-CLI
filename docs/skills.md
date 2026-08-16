# Rinari CLI — Skills Catalog

> Catálogo maestro de skills para un agente CLI generalista.  
> Una **skill** no es una tool atómica: es una capacidad compuesta que combina razonamiento, procedimientos, tools, validación y criterios de éxito.

---

## 1. Core Agent Skills

- Goal interpretation
- Intent classification
- Requirement extraction
- Constraint detection
- Ambiguity resolution
- Task decomposition
- Task prioritization
- Dependency detection
- Planning
- Replanning
- Execution monitoring
- Progress tracking
- Completion verification
- Failure recovery
- Risk assessment
- Tool selection
- Tool discovery
- Tool composition
- Budget-aware execution
- Context-aware execution
- Cancellation handling
- Human escalation

---

## 2. Planning & Orchestration

- Create execution plans
- Build task graphs
- Identify parallelizable work
- Identify sequential dependencies
- Split work between subagents
- Merge subagent findings
- Track blockers
- Retry failed steps
- Select fallback approaches
- Stop unproductive loops
- Resume interrupted workflows
- Create checkpoints
- Roll back unsafe changes
- Verify workflow completion
- Optimize plan for latency
- Optimize plan for cost
- Optimize plan for reliability

---

## 3. Tool Use

- Discover relevant tools
- Inspect tool schemas
- Choose appropriate tool
- Fill structured arguments
- Validate tool arguments
- Interpret tool outputs
- Handle partial results
- Handle retryable errors
- Handle non-retryable errors
- Avoid duplicate side effects
- Use idempotency keys
- Compose multiple tools
- Prefer deterministic tools over free-form reasoning
- Use escape-hatch tools safely
- Detect when a tool is insufficient
- Load domain-specific tools on demand

---

## 4. Filesystem Skills

- Explore directory trees
- Find relevant files
- Read large files selectively
- Search text across a repository
- Search regex patterns
- Compare files
- Apply minimal patches
- Refactor files safely
- Create files
- Rename and move files
- Delete files safely
- Preserve file permissions
- Detect generated files
- Detect binary files
- Create backups/checkpoints
- Validate filesystem changes
- Watch file changes

---

## 5. Shell & System Skills

- Execute shell commands
- Interpret exit codes
- Capture stdout/stderr
- Run long-lived processes
- Manage interactive processes
- Use PTY sessions
- Detect installed binaries
- Inspect environment variables
- Inspect system resources
- Diagnose process failures
- Kill stuck processes
- Chain shell commands safely
- Quote shell arguments correctly
- Avoid shell injection
- Use platform-specific commands
- Detect OS/platform differences

---

## 6. Repository Understanding

- Map repository structure
- Detect languages
- Detect frameworks
- Detect package managers
- Detect build systems
- Detect test frameworks
- Detect CI configuration
- Detect deployment configuration
- Detect coding conventions
- Detect architectural boundaries
- Find entry points
- Find configuration files
- Identify generated code
- Identify public APIs
- Identify internal modules
- Build repository mental model
- Summarize repository architecture

---

## 7. Coding

- Implement features
- Fix bugs
- Refactor code
- Write idiomatic code
- Match project conventions
- Preserve backward compatibility
- Add type annotations
- Improve error handling
- Improve logging
- Reduce duplication
- Improve modularity
- Implement interfaces
- Add configuration
- Add CLI commands
- Add API endpoints
- Add database integrations
- Add background jobs
- Add caching
- Add validation
- Add feature flags

---

## 8. Code Navigation

- Find symbol definitions
- Find symbol references
- Trace call graphs
- Trace data flow
- Inspect type information
- Inspect diagnostics
- Locate implementations
- Locate interfaces
- Locate tests
- Locate related configuration
- Locate dependency usage
- Navigate monorepos
- Navigate generated sources

---

## 9. Debugging

- Reproduce bugs
- Minimize reproduction cases
- Inspect logs
- Inspect stack traces
- Form debugging hypotheses
- Test hypotheses
- Bisect failures
- Trace runtime behavior
- Diagnose race conditions
- Diagnose deadlocks
- Diagnose memory issues
- Diagnose performance regressions
- Diagnose environment issues
- Diagnose dependency conflicts
- Diagnose configuration problems
- Diagnose network failures
- Diagnose authentication failures
- Diagnose serialization errors
- Diagnose database failures
- Verify the fix

---

## 10. Testing

- Identify appropriate test level
- Write unit tests
- Write integration tests
- Write end-to-end tests
- Write regression tests
- Write snapshot tests
- Write property-based tests
- Write contract tests
- Generate fixtures
- Generate mocks
- Test error paths
- Test edge cases
- Test concurrency
- Test migrations
- Measure coverage
- Interpret coverage gaps
- Run targeted tests
- Run full test suites
- Diagnose flaky tests
- Repair flaky tests

---

## 11. Code Review

- Review correctness
- Review maintainability
- Review readability
- Review architecture
- Review API design
- Review security
- Review performance
- Review tests
- Review error handling
- Review observability
- Review backward compatibility
- Review migrations
- Review dependency changes
- Review concurrency
- Review edge cases
- Identify hidden side effects
- Rank findings by severity
- Suggest concrete fixes

---

## 12. Refactoring

- Extract functions
- Extract modules
- Extract classes
- Rename symbols
- Remove dead code
- Simplify control flow
- Reduce coupling
- Increase cohesion
- Separate concerns
- Replace duplication
- Introduce interfaces
- Improve dependency boundaries
- Refactor without changing behavior
- Validate refactor with tests
- Perform staged refactors

---

## 13. Git Skills

- Inspect repository status
- Inspect diffs
- Inspect history
- Identify relevant commits
- Create branches
- Create commits
- Split commits
- Amend commits
- Revert commits
- Cherry-pick commits
- Resolve merge conflicts
- Rebase branches
- Manage stashes
- Manage worktrees
- Prepare clean patches
- Preserve user changes
- Avoid destructive git operations
- Explain git history

---

## 14. GitHub / GitLab Collaboration

- Triage issues
- Summarize issues
- Reproduce issue reports
- Link issues to code
- Prepare pull requests
- Review pull requests
- Address review comments
- Resolve review threads
- Summarize PR changes
- Inspect CI checks
- Diagnose CI failures
- Rerun failed CI
- Prepare release notes
- Manage releases
- Analyze repository activity
- Identify maintainers
- Identify related issues/PRs

---

## 15. Dependency Management

- Detect outdated dependencies
- Add dependencies
- Remove dependencies
- Upgrade dependencies
- Pin dependency versions
- Resolve dependency conflicts
- Interpret lockfiles
- Minimize dependency footprint
- Audit vulnerabilities
- Replace deprecated libraries
- Plan major version upgrades
- Verify compatibility
- Test dependency migrations

---

## 16. API Engineering

- Design REST APIs
- Design GraphQL APIs
- Design RPC APIs
- Define request schemas
- Define response schemas
- Design pagination
- Design filtering
- Design sorting
- Design error responses
- Version APIs
- Maintain backward compatibility
- Generate API clients
- Consume external APIs
- Handle retries
- Handle timeouts
- Handle rate limits
- Implement idempotency
- Implement webhooks
- Verify signatures
- Debug API integrations

---

## 17. OpenAPI Skills

- Read OpenAPI specs
- Validate OpenAPI specs
- Find API operations
- Generate tool schemas
- Generate API clients
- Generate server stubs
- Detect schema inconsistencies
- Detect undocumented behavior
- Call operations safely
- Infer authentication requirements
- Compose APIs into agent tools

---

## 18. MCP Skills

- Discover MCP servers
- Inspect MCP capabilities
- Load MCP tools
- Read MCP resources
- Use MCP prompts
- Call MCP tools
- Handle MCP errors
- Select between multiple MCP servers
- Expose local capabilities through MCP
- Build MCP adapters

---

## 19. Web Research

- Form search queries
- Search broadly
- Search precisely
- Search recent information
- Search primary sources
- Search technical documentation
- Search academic sources
- Search news
- Compare multiple sources
- Detect stale information
- Assess source authority
- Detect contradictory sources
- Extract relevant evidence
- Preserve citations
- Distinguish facts from inference
- Summarize research findings
- Identify unresolved uncertainty

---

## 20. Browser Automation

- Navigate websites
- Handle SPAs
- Interact with forms
- Upload files
- Download files
- Handle pagination
- Handle modal dialogs
- Handle tabs
- Inspect DOM
- Inspect accessibility tree
- Inspect browser console
- Inspect network requests
- Extract structured data
- Handle authenticated sessions
- Handle OAuth flows
- Detect browser automation failure
- Fall back to alternate interfaces

---

## 21. Data Extraction

- Extract text from HTML
- Extract tables from HTML
- Extract metadata
- Extract structured JSON
- Parse embedded JSON
- Extract PDF text
- Extract PDF tables
- Extract spreadsheet data
- Extract image text
- Normalize extracted data
- Deduplicate records
- Validate extracted fields
- Preserve provenance

---

## 22. Data Transformation

- Clean datasets
- Normalize fields
- Map schemas
- Merge datasets
- Join datasets
- Filter records
- Sort records
- Group records
- Aggregate values
- Pivot data
- Unpivot data
- Deduplicate data
- Impute missing values
- Convert units
- Convert timezones
- Convert encodings
- Validate transformed output

---

## 23. SQL & Database Skills

- Explore database schema
- Read table definitions
- Write SELECT queries
- Write joins
- Write aggregations
- Write window functions
- Explain query plans
- Optimize queries
- Write inserts
- Write updates
- Write deletes
- Use transactions
- Write migrations
- Review migrations
- Detect unsafe migrations
- Backfill data
- Verify data integrity
- Diagnose slow queries
- Diagnose locking
- Diagnose connection failures

---

## 24. NoSQL Skills

- Model document schemas
- Write MongoDB queries
- Write aggregation pipelines
- Model Redis keys
- Use Redis caching patterns
- Query vector stores
- Design vector metadata
- Tune semantic retrieval
- Manage TTL policies
- Diagnose consistency issues

---

## 25. Data Science

- Explore datasets
- Compute descriptive statistics
- Detect outliers
- Analyze correlations
- Build simple models
- Evaluate models
- Compare experiments
- Perform hypothesis tests
- Calculate confidence intervals
- Analyze distributions
- Create derived features
- Detect data leakage
- Analyze cohort behavior
- Analyze funnels
- Analyze retention
- Analyze time series

---

## 26. Mathematical Reasoning

- Arithmetic
- Algebra
- Symbolic manipulation
- Calculus
- Probability
- Statistics
- Linear algebra
- Optimization
- Constraint solving
- Unit conversion
- Numerical verification
- Dimensional analysis

---

## 27. Security Engineering

- Threat modeling
- Detect insecure code
- Detect injection risks
- Detect auth flaws
- Detect authorization flaws
- Detect secret leakage
- Detect vulnerable dependencies
- Review cryptographic usage
- Review session handling
- Review CORS
- Review CSP
- Review SSRF risks
- Review file upload security
- Review deserialization risks
- Review command execution risks
- Review sandbox boundaries
- Review infrastructure security
- Review IAM permissions
- Recommend least privilege

---

## 28. Secrets Management

- Identify secret requirements
- Request secret scopes
- Inject secrets without exposing plaintext
- Detect accidental secret logging
- Rotate credentials
- Revoke credentials
- Handle expired credentials
- Select appropriate credential source
- Enforce least privilege

---

## 29. DevOps

- Understand CI/CD pipelines
- Build CI workflows
- Debug CI failures
- Optimize CI runtime
- Configure caching
- Configure artifacts
- Configure environments
- Configure secrets
- Build release pipelines
- Build deployment pipelines
- Implement rollback strategy
- Diagnose deployment failures
- Compare environments
- Validate production readiness

---

## 30. Docker

- Write Dockerfiles
- Optimize Dockerfiles
- Build images
- Run containers
- Debug containers
- Inspect container logs
- Reduce image size
- Configure volumes
- Configure networks
- Configure multi-stage builds
- Detect container security issues
- Diagnose container startup failures

---

## 31. Kubernetes

- Read manifests
- Write manifests
- Apply manifests
- Inspect deployments
- Inspect pods
- Inspect services
- Inspect ingress
- Inspect config maps
- Inspect secrets
- Inspect events
- Read logs
- Execute into pods
- Debug crash loops
- Debug networking
- Debug scheduling
- Debug resource limits
- Manage rollouts
- Scale workloads
- Review cluster safety

---

## 32. Infrastructure as Code

- Read Terraform/OpenTofu
- Write Terraform/OpenTofu
- Validate IaC
- Run plans
- Review plans
- Detect destructive changes
- Refactor modules
- Import existing resources
- Manage state
- Diagnose drift
- Review provider upgrades
- Review IAM changes
- Plan infrastructure migrations

---

## 33. Cloud Engineering

- Inspect cloud resources
- Inspect logs
- Inspect identity
- Inspect storage
- Inspect compute
- Inspect serverless functions
- Diagnose permission issues
- Diagnose network issues
- Diagnose deployment failures
- Estimate cloud cost
- Detect unused resources
- Recommend architecture changes
- Validate cloud configurations

---

## 34. Observability

- Read logs
- Query logs
- Read metrics
- Analyze traces
- Correlate logs and traces
- Detect anomalies
- Identify error spikes
- Identify latency regressions
- Identify resource saturation
- Diagnose distributed failures
- Define dashboards
- Define alerts
- Validate instrumentation
- Add structured logging
- Add tracing
- Add metrics

---

## 35. Performance Engineering

- Benchmark code
- Profile CPU
- Profile memory
- Profile I/O
- Profile database queries
- Detect N+1 queries
- Detect unnecessary allocations
- Detect excessive network calls
- Detect cache misses
- Optimize hot paths
- Compare before/after performance
- Prevent performance regressions

---

## 36. Reliability Engineering

- Detect failure modes
- Add retries
- Add backoff
- Add circuit breakers
- Add timeouts
- Add rate limiting
- Add graceful degradation
- Add idempotency
- Add health checks
- Add readiness checks
- Add liveness checks
- Design recovery procedures
- Validate rollback paths
- Perform failure analysis

---

## 37. Documentation

- Read technical documentation
- Generate README files
- Generate API docs
- Generate architecture docs
- Generate setup guides
- Generate troubleshooting guides
- Generate runbooks
- Generate changelogs
- Generate migration guides
- Generate contribution guides
- Keep docs synchronized with code
- Validate code snippets in docs

---

## 38. Technical Writing

- Explain complex systems
- Summarize code
- Summarize architecture
- Explain tradeoffs
- Write concise CLI help
- Write error messages
- Write release notes
- Write technical proposals
- Write ADRs
- Write RFCs
- Write design documents

---

## 39. CLI Design

- Design command hierarchies
- Design flags
- Design interactive prompts
- Design help text
- Design exit codes
- Design machine-readable output
- Design human-readable output
- Support JSON output
- Support quiet mode
- Support verbose/debug mode
- Support progress indicators
- Support shell completion
- Support config files
- Support environment variables
- Maintain backward compatibility

---

## 40. Agent CLI UX

- Show current goal
- Show current step
- Show active tool
- Show permission requests
- Show side effects
- Show progress
- Stream tool output
- Allow interruption
- Allow cancellation
- Allow plan edits
- Allow approval once
- Allow approval for session
- Allow persistent permission rules
- Allow undo
- Show artifacts
- Show citations
- Show execution summary

---

## 41. Permissions & Safety

- Classify action risk
- Detect destructive actions
- Detect external side effects
- Detect irreversible operations
- Check filesystem scope
- Check network scope
- Check secret scope
- Check cloud scope
- Ask for approval
- Explain approval request
- Apply least privilege
- Deny out-of-scope actions
- Enforce sandbox boundaries
- Require confirmation for critical actions

---

## 42. Transaction & Rollback Skills

- Identify transaction boundaries
- Create checkpoints before mutation
- Use database transactions
- Use git checkpoints
- Snapshot filesystem state
- Preview destructive changes
- Roll back failed operations
- Verify rollback success
- Commit successful transaction
- Clean obsolete checkpoints

---

## 43. Memory Management

- Decide what should be remembered
- Decide what should not be remembered
- Store durable user preferences
- Store project facts
- Store task history
- Search memory
- Resolve conflicting memories
- Expire stale memories
- Update changed facts
- Track memory provenance
- Separate session and persistent memory

---

## 44. Context Engineering

- Select relevant context
- Drop irrelevant context
- Summarize old context
- Pin critical context
- Retrieve prior decisions
- Retrieve related artifacts
- Retrieve code selectively
- Prevent context overflow
- Preserve important constraints
- Track unresolved questions
- Track task state outside model context

---

## 45. Subagent Delegation

- Decide when delegation helps
- Define bounded subagent tasks
- Provide minimal sufficient context
- Restrict subagent tools
- Restrict subagent budgets
- Run subagents in parallel
- Collect results
- Detect contradictory subagent results
- Merge findings
- Request follow-up from subagent
- Cancel unnecessary subagents

---

## 46. Research Agent Skills

- Define research questions
- Build search strategy
- Search multiple source classes
- Prioritize primary sources
- Verify claims
- Compare conflicting evidence
- Track citations
- Distinguish current vs historical facts
- Identify missing evidence
- Produce evidence-backed synthesis
- Avoid unsupported conclusions

---

## 47. Document Skills

- Read PDFs
- Read DOCX
- Read spreadsheets
- Read presentations
- Create PDFs
- Create DOCX
- Create spreadsheets
- Create presentations
- Edit documents
- Convert formats
- Extract tables
- Extract images
- Render document previews
- Preserve formatting
- Validate generated files

---

## 48. Spreadsheet Skills

- Read workbooks
- Write cells
- Write formulas
- Create tables
- Clean spreadsheet data
- Format sheets
- Create charts
- Build dashboards
- Create pivot tables
- Validate formulas
- Analyze workbook structure
- Detect broken references
- Export CSV
- Export PDF

---

## 49. Presentation Skills

- Build slide narratives
- Create slide outlines
- Create slides
- Edit slides
- Create diagrams
- Create charts
- Preserve template styling
- Improve slide density
- Improve visual hierarchy
- Validate slide consistency
- Export presentation
- Render previews

---

## 50. Image Skills

- Inspect images
- Describe images
- Extract text
- Detect objects
- Compare images
- Crop images
- Resize images
- Convert formats
- Compress images
- Generate images
- Edit images
- Remove objects
- Replace backgrounds
- Upscale images

---

## 51. Audio Skills

- Transcribe audio
- Identify speakers
- Summarize audio
- Translate speech
- Generate speech
- Convert audio formats
- Trim audio
- Merge audio
- Extract audio from video

---

## 52. Video Skills

- Inspect video metadata
- Extract frames
- Transcribe video
- Summarize video
- Find relevant timestamps
- Trim video
- Merge video
- Convert video
- Generate video
- Analyze visual scenes

---

## 53. Computer Use Skills

- Understand screen state
- Navigate native applications
- Use mouse safely
- Use keyboard shortcuts
- Use accessibility tree
- Interact with dialogs
- Manage windows
- Use clipboard
- Recover from UI changes
- Avoid coordinate-based interaction when a structured interface exists

---

## 54. Communication Skills

- Search messages
- Read threads
- Summarize conversations
- Draft replies
- Send replies
- Follow up
- Detect unanswered messages
- Extract action items
- Extract decisions
- Extract deadlines
- Preserve communication tone
- Avoid accidental sends
- Confirm high-impact external communication

---

## 55. Email Skills

- Search inbox
- Read email threads
- Categorize emails
- Detect urgent emails
- Summarize inbox
- Draft email
- Reply to email
- Forward email
- Manage labels
- Archive email
- Extract attachments
- Schedule follow-up
- Detect phishing indicators

---

## 56. Calendar Skills

- Search events
- Check availability
- Find scheduling conflicts
- Create events
- Update events
- Cancel events
- Respond to invitations
- Compare calendars
- Suggest meeting times
- Respect timezones
- Detect travel conflicts
- Generate meeting agendas
- Generate meeting follow-ups

---

## 57. Project Management Skills

- Create tasks
- Update tasks
- Prioritize tasks
- Detect blocked tasks
- Detect overdue tasks
- Link tasks to code changes
- Summarize project status
- Identify risks
- Identify missing owners
- Build milestone plans
- Create sprint summaries
- Update Jira/Linear/Asana
- Generate status reports

---

## 58. Product Skills

- Analyze user requirements
- Write product requirements
- Break features into stories
- Define acceptance criteria
- Prioritize features
- Analyze user feedback
- Identify product risks
- Analyze funnels
- Analyze retention
- Analyze experiments
- Compare roadmap tradeoffs

---

## 59. Design Skills

- Review UI structure
- Review UX flows
- Detect accessibility issues
- Review information hierarchy
- Suggest interaction improvements
- Translate designs into implementation
- Inspect design tokens
- Maintain component consistency
- Review responsive behavior

---

## 60. Accessibility Skills

- Inspect semantic HTML
- Inspect ARIA usage
- Inspect keyboard navigation
- Inspect focus behavior
- Inspect color-independent semantics
- Inspect form labels
- Inspect alternative text
- Inspect heading structure
- Test screen-reader compatibility
- Recommend accessibility fixes

---

## 61. Localization & Internationalization

- Detect hard-coded strings
- Extract translation keys
- Translate UI strings
- Review locale formats
- Handle pluralization
- Handle date formats
- Handle currency formats
- Handle RTL layouts
- Validate encoding
- Detect localization regressions

---

## 62. Configuration Management

- Discover config files
- Parse configuration
- Merge layered configuration
- Validate configuration
- Detect missing settings
- Detect conflicting settings
- Manage environment overrides
- Manage secrets references
- Generate default configuration
- Migrate configuration formats

---

## 63. Release Engineering

- Determine release version
- Generate changelog
- Validate release branch
- Run release checks
- Build artifacts
- Sign artifacts
- Publish packages
- Create GitHub/GitLab release
- Tag release
- Publish release notes
- Verify deployment
- Roll back release

---

## 64. Package Publishing

- Validate package metadata
- Build package
- Run package tests
- Check package contents
- Publish npm package
- Publish Python package
- Publish Rust crate
- Publish container image
- Verify published package
- Handle failed publication

---

## 65. CI/CD Skills

- Understand workflow files
- Create workflows
- Edit workflows
- Optimize workflows
- Add caching
- Add matrix builds
- Add artifacts
- Add environment gates
- Add approvals
- Diagnose failed jobs
- Diagnose flaky CI
- Diagnose timeout failures
- Verify fixes locally
- Rerun checks

---

## 66. Migration Skills

- Plan schema migrations
- Plan framework migrations
- Plan language migrations
- Plan dependency migrations
- Plan cloud migrations
- Plan API migrations
- Build migration steps
- Build rollback steps
- Identify compatibility risks
- Validate migrated data
- Perform staged migration
- Verify post-migration behavior

---

## 67. Architecture Skills

- Analyze architecture
- Identify components
- Identify boundaries
- Identify dependencies
- Identify coupling
- Identify bottlenecks
- Identify failure domains
- Design modular architectures
- Design event-driven systems
- Design distributed systems
- Design service boundaries
- Design data flows
- Design caching strategy
- Design deployment topology
- Evaluate tradeoffs
- Create architecture diagrams
- Write ADRs

---

## 68. Distributed Systems Skills

- Reason about consistency
- Reason about availability
- Reason about partitions
- Design retries
- Design idempotency
- Design queues
- Design event processing
- Design deduplication
- Design leader election
- Design locks
- Design leases
- Design sagas
- Diagnose distributed races
- Diagnose message duplication
- Diagnose out-of-order events

---

## 69. Networking Skills

- Diagnose DNS
- Diagnose TCP connectivity
- Diagnose TLS
- Diagnose HTTP
- Diagnose proxy issues
- Diagnose firewall issues
- Diagnose routing
- Diagnose ports
- Analyze latency
- Inspect network connections
- Validate certificates

---

## 70. Authentication & Authorization

- Implement authentication
- Implement OAuth
- Implement OIDC
- Implement API keys
- Implement sessions
- Implement JWT
- Implement refresh tokens
- Implement RBAC
- Implement ABAC
- Review authorization boundaries
- Diagnose login issues
- Diagnose token expiration
- Diagnose permission failures

---

## 71. Cryptography Skills

- Select secure primitives
- Hash data
- Verify hashes
- Encrypt data
- Decrypt data
- Sign data
- Verify signatures
- Generate secure random values
- Handle key material safely
- Review cryptographic misuse

---

## 72. Cache Skills

- Identify cacheable data
- Design cache keys
- Configure TTL
- Implement invalidation
- Prevent cache stampede
- Diagnose stale cache
- Diagnose low hit rate
- Compare cache strategies
- Measure cache effectiveness

---

## 73. Queue & Event Skills

- Publish messages
- Consume messages
- Retry messages
- Handle dead-letter queues
- Enforce idempotency
- Handle ordering
- Handle duplicates
- Design event schemas
- Version events
- Trace event flows
- Diagnose stuck consumers
- Diagnose backlog

---

## 74. Scheduling Skills

- Create one-time tasks
- Create recurring tasks
- Interpret cron expressions
- Handle timezones
- Handle retries
- Handle missed runs
- Handle overlapping runs
- Pause schedules
- Resume schedules
- Cancel schedules
- Track schedule history

---

## 75. Monitoring & Conditional Watch Skills

- Define conditions
- Poll external state
- Compare previous state
- Detect meaningful changes
- Suppress duplicate notifications
- Respect rate limits
- Notify only when condition is met
- Stop monitoring when resolved
- Track watch history

---

## 76. Financial Data Skills

- Retrieve market data
- Retrieve exchange rates
- Compare historical prices
- Calculate returns
- Calculate volatility
- Summarize market movement
- Track price thresholds
- Preserve timestamped provenance

---

## 77. Weather Skills

- Retrieve current weather
- Retrieve forecast
- Interpret alerts
- Compare forecast windows
- Track weather conditions
- Convert units
- Respect location/timezone context

---

## 78. Geospatial Skills

- Geocode locations
- Reverse geocode coordinates
- Calculate distances
- Build routes
- Find nearby places
- Compare route options
- Handle timezones by location
- Normalize addresses

---

## 79. Knowledge Management

- Search knowledge bases
- Retrieve relevant documents
- Summarize documents
- Link related knowledge
- Detect duplicate knowledge
- Detect stale knowledge
- Track source provenance
- Update knowledge indexes
- Build semantic indexes

---

## 80. Prompt Engineering

- Analyze system prompts
- Write tool descriptions
- Write task prompts
- Reduce ambiguity
- Improve structured output reliability
- Design few-shot examples
- Design prompt templates
- Detect conflicting instructions
- Minimize prompt injection surface
- Test prompt variations
- Benchmark prompts

---

## 81. Model Routing

- Select model by task
- Select model by latency
- Select model by cost
- Select model by modality
- Select model by context window
- Fall back between models
- Route coding tasks
- Route research tasks
- Route vision tasks
- Route lightweight tasks
- Compare model outputs
- Estimate token usage
- Estimate cost

---

## 82. Evaluation Skills

- Create eval datasets
- Create golden outputs
- Create deterministic assertions
- Create model-based judges
- Evaluate tool use
- Evaluate task completion
- Evaluate factuality
- Evaluate latency
- Evaluate cost
- Compare agent trajectories
- Detect regressions
- Analyze failure cases
- Generate eval reports

---

## 83. Observability of Agents

- Trace model turns
- Trace tool calls
- Trace subagents
- Measure token usage
- Measure tool latency
- Measure total latency
- Measure cost
- Inspect retries
- Inspect failures
- Inspect approvals
- Inspect context usage
- Inspect artifacts
- Export sessions
- Replay sessions

---

## 84. Agent Failure Recovery

- Detect stalled execution
- Detect repeated actions
- Detect repeated errors
- Detect context confusion
- Detect invalid assumptions
- Retry safely
- Switch tools
- Switch approach
- Reduce task scope
- Ask user only when necessary
- Restore checkpoint
- Cancel failing subagent
- Produce partial result when completion is impossible

---

## 85. Cost Management

- Estimate cost before execution
- Select cheaper tools
- Select cheaper models
- Reuse cached results
- Avoid redundant searches
- Avoid redundant model calls
- Limit subagent fan-out
- Compress context
- Track spend
- Stop at budget threshold

---

## 86. Latency Optimization

- Parallelize independent work
- Batch tool calls
- Cache repeated results
- Use deterministic local tools
- Avoid unnecessary model calls
- Use smaller models where sufficient
- Stream outputs
- Reuse open sessions
- Minimize context size

---

## 87. Provenance & Citations

- Track data source
- Track file line ranges
- Track URL source
- Track query source
- Track tool call source
- Track timestamp
- Attach provenance to artifacts
- Verify provenance
- Produce citations
- Explain evidence chain

---

## 88. Structured Output Skills

- Produce valid JSON
- Produce JSON matching schema
- Produce YAML
- Produce XML
- Produce CSV
- Produce Markdown
- Produce typed tool arguments
- Validate structured outputs
- Repair malformed outputs
- Preserve deterministic formatting

---

## 89. Error Handling

- Classify errors
- Distinguish retryable errors
- Distinguish permission errors
- Distinguish authentication errors
- Distinguish validation errors
- Distinguish conflicts
- Distinguish partial failures
- Avoid repeating side effects
- Provide recovery options
- Escalate critical failures
- Preserve error context

---

## 90. Idempotency Skills

- Detect mutating operations
- Generate idempotency keys
- Reuse idempotency keys on retries
- Detect duplicate requests
- Reconcile uncertain outcomes
- Prevent duplicate sends
- Prevent duplicate creates
- Prevent duplicate payments
- Prevent duplicate deployments

---

## 91. Human-in-the-Loop Skills

- Know when approval is required
- Explain intended action
- Explain side effects
- Explain risk
- Show exact command/action
- Request one-time approval
- Request session approval
- Respect denied actions
- Offer safer alternatives
- Resume after approval
- Preserve user control

---

## 92. Plugin Development

- Discover plugin API
- Scaffold plugins
- Define tools
- Define schemas
- Define permissions
- Define risk metadata
- Define lifecycle hooks
- Test plugins
- Package plugins
- Install plugins
- Upgrade plugins
- Disable plugins
- Debug plugin failures

---

## 93. Tool Development

- Define atomic tools
- Define deterministic outputs
- Define input schemas
- Define output schemas
- Define error contracts
- Define side effects
- Define permission scopes
- Define idempotency behavior
- Define retry policy
- Define timeout policy
- Write tool tests
- Write tool documentation
- Benchmark tools

---

## 94. Tool Composition Skills

- Detect repeated tool chains
- Compose reusable operations
- Define composite inputs
- Define composite outputs
- Propagate errors
- Propagate provenance
- Propagate permissions
- Preserve idempotency
- Cache composite results
- Register temporary composed tools

---

## 95. Artifact Management Skills

- Create artifacts
- Read artifacts selectively
- Search large artifacts
- Slice large artifacts
- Convert artifacts
- Render artifacts
- Export artifacts
- Attach artifacts to sessions
- Avoid loading entire large outputs into context
- Preserve artifact provenance

---

## 96. Sandbox Skills

- Decide when isolation is required
- Create sandbox
- Limit filesystem access
- Limit network access
- Limit secrets
- Limit CPU
- Limit memory
- Limit execution time
- Inspect sandbox changes
- Reset sandbox
- Destroy sandbox

---

## 97. Privacy Skills

- Minimize sensitive data exposure
- Detect personal data
- Avoid unnecessary retention
- Avoid exposing secrets to models
- Redact sensitive logs
- Scope data access
- Respect user deletion
- Apply data minimization
- Track sensitive-data provenance

---

## 98. Compliance Skills

- Detect policy-sensitive actions
- Apply approval policies
- Maintain audit logs
- Record data access
- Record state mutations
- Retain required provenance
- Enforce access control
- Export audit trails
- Respect retention rules

---

## 99. Final Verification Skills

Before declaring success, the agent should be able to:

- Re-read modified files
- Run relevant tests
- Run type checks
- Run lint checks
- Validate generated schemas
- Inspect git diff
- Verify external side effects
- Verify deployment status
- Verify database state
- Verify created artifacts
- Check unresolved errors
- Check task acceptance criteria
- Check user constraints
- Report remaining uncertainty

---

## 100. Recommended Skill Layers

### Layer 1 — Universal Core
Always available:

- intent understanding
- planning
- filesystem navigation
- shell execution
- search
- context management
- tool discovery
- error handling
- verification
- permissions
- artifact handling

### Layer 2 — Engineering
Load for coding tasks:

- repository understanding
- coding
- debugging
- testing
- git
- code review
- dependencies
- CI/CD
- containers
- databases

### Layer 3 — Research & Web
Load for external knowledge tasks:

- web research
- browser automation
- extraction
- citations
- source verification

### Layer 4 — Infrastructure
Load when needed:

- cloud
- Kubernetes
- Terraform/OpenTofu
- networking
- security
- observability

### Layer 5 — Productivity
Load for account-integrated work:

- email
- calendar
- docs
- sheets
- project management
- communication

### Layer 6 — Multimodal
Load for media tasks:

- image
- audio
- video
- PDF
- presentations
- computer use

### Layer 7 — Extensible Domain Skills
Loaded dynamically:

- MCP skills
- OpenAPI-derived skills
- plugin-defined skills
- project-specific skills
- organization-specific skills
- user-created skills

---

## 101. Skill Definition Format

Recommended skill contract:

```yaml
name: fix-ci-failure
description: Diagnose and repair a failing CI pipeline.

triggers:
  - failing CI
  - GitHub Actions failure
  - pipeline error

required_tools:
  - git.status
  - git.diff
  - code.test
  - github.checks
  - github.actions_logs

optional_tools:
  - web.search
  - package.audit

permissions:
  - filesystem:write
  - process:execute
  - github:read

risk: medium

procedure:
  - inspect repository state
  - inspect failed checks
  - retrieve logs
  - reproduce locally
  - identify root cause
  - implement minimal fix
  - run targeted tests
  - run validation
  - inspect final diff

success_criteria:
  - root cause identified
  - relevant tests pass
  - no unrelated changes introduced

failure_policy:
  - preserve logs
  - avoid destructive reset
  - return partial diagnosis if reproduction is impossible
```

---

## 102. Skill Runtime Metadata

Each skill should ideally support:

```ts
type SkillDefinition = {
  name: string
  description: string

  triggers?: string[]
  requiredTools: string[]
  optionalTools?: string[]

  requiredPermissions?: string[]
  risk?: "low" | "medium" | "high" | "critical"

  maxToolCalls?: number
  maxModelCalls?: number
  maxCost?: number

  procedure?: SkillStep[]
  successCriteria?: string[]
  failurePolicy?: string[]

  canDelegate?: boolean
  canRunInParallel?: boolean

  version: string
}
```

---

## 103. Skill Execution Lifecycle

```text
detect skill
    ↓
load skill definition
    ↓
check required tools
    ↓
load missing tools
    ↓
check permissions
    ↓
build task plan
    ↓
create checkpoint if needed
    ↓
execute
    ↓
observe
    ↓
recover / retry
    ↓
validate success criteria
    ↓
commit state
    ↓
report result + provenance
```

---

## 104. Skills vs Tools

Use this distinction consistently:

```text
TOOL
Atomic executable capability.

Example:
git.diff
fs.patch
browser.click
db.query


SKILL
Reusable procedure that combines tools and judgment.

Example:
review-pull-request
fix-ci-failure
migrate-database
research-topic
deploy-application
debug-production-incident
```

A healthy architecture has many tools, but the model should solve common workflows primarily through **skills** rather than repeatedly rediscovering procedures from scratch.
