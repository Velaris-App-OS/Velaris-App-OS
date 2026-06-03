-- P42: HxNexus Polyglot Intelligence
-- bpm_concepts: knowledge base mapping Pega/Camunda/Appian/ServiceNow → Helix
-- generated_docs: cache for AI-generated business + developer guides

BEGIN;

CREATE TABLE bpm_concepts (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_tool     VARCHAR(50) NOT NULL,   -- pega | camunda | appian | servicenow
    source_concept  VARCHAR(255) NOT NULL,  -- e.g. "Assignment shape"
    helix_equiv     VARCHAR(255) NOT NULL,  -- e.g. "user_task step"
    helix_node_type VARCHAR(50),            -- e.g. "step" (graph node type)
    description     TEXT        NOT NULL,   -- plain-English mapping explanation
    example         TEXT,                   -- concrete before/after example
    confidence      VARCHAR(10) NOT NULL DEFAULT 'exact', -- exact|close|partial|manual
    notes           TEXT,                   -- caveats / differences
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ix_bpm_concepts_tool       ON bpm_concepts(source_tool);
CREATE INDEX ix_bpm_concepts_concept    ON bpm_concepts(source_concept);
CREATE INDEX ix_bpm_concepts_confidence ON bpm_concepts(confidence);

CREATE TABLE generated_docs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    doc_type        VARCHAR(50) NOT NULL UNIQUE,  -- business_guide | dev_guide
    content         TEXT        NOT NULL,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    node_count      INTEGER     NOT NULL DEFAULT 0  -- graph size at generation time
);

-- ─── Seed: Pega → Helix ───────────────────────────────────────────────────────

INSERT INTO bpm_concepts (source_tool, source_concept, helix_equiv, helix_node_type, description, example, confidence, notes) VALUES

-- Case structure
('pega', 'Case Type',         'case_type',         'case_type',     'The top-level container for a business process. Defines the lifecycle, stages, and data model.',          'InsuranceClaim → case_type: insurance_claim',           'exact',   NULL),
('pega', 'Stage',             'stage',             'stage',         'A named phase within a case lifecycle. Cases move from stage to stage as work progresses.',               'Intake stage → stage: intake',                          'exact',   NULL),
('pega', 'Assignment shape',  'user_task step',    'step',          'Work assigned to a human operator. The operator opens it, acts on it, and submits.',                     'ReviewAssignment → user_task step: review',             'exact',   NULL),
('pega', 'Approval shape',    'approval step',     'step',          'A yes/no decision point. Approver sees context and approves or rejects.',                                'ManagerApproval → approval step',                       'exact',   NULL),
('pega', 'Decision shape',    'routing rule',      NULL,            'Conditional branching based on case data. Routes to different stages or steps.',                         'CreditCheck decision → routing rule with conditions',   'close',   'Complex multi-branch decisions may need manual review'),
('pega', 'Data Transform',    'automated step',    'step',          'Automated field mapping and data manipulation. Runs without human input.',                               'MapClaimData → automated step',                         'close',   'Logic must be re-expressed as Helix field mappings'),
('pega', 'Subprocess',        'embedded stage',    'stage',         'A reusable child process embedded in a parent case.',                                                   'DocumentVerification subprocess → embedded stage',      'close',   'Helix uses HxFusion (P47) for full subprocess support'),
('pega', 'Spin-off case',     'child case',        'case_type',     'Creates a new case instance from within a running case.',                                               'SpinOffPayment → child case_type: payment',             'close',   NULL),

-- Forms and data
('pega', 'Section',           'form section',      'form',          'A named group of fields in a Pega form. Maps directly to a FormDefinition section.',                   'ClaimantSection → section in Claim Form',               'exact',   NULL),
('pega', 'Property',          'form field',        'field',         'A single data field. Maps to a field in a FormDefinition section.',                                    'claimAmount property → currency field: claim_amount',   'exact',   NULL),
('pega', 'Flow Action',       'form step',         'step',          'A button or action on a screen that captures input and advances the flow.',                            'SubmitClaim flow action → user_task step with form',    'exact',   NULL),
('pega', 'Harness',           'portal page',       NULL,            'The full-page UI container. Helix uses Portal.tsx for customer-facing and Studio for operators.',       'WorkHarness → Studio CaseManager module',               'close',   'UI layout is not converted — rebuilt in React'),
('pega', 'Clipboard',         'case data (JSONB)', NULL,            'In-memory data model for a running case. In Helix, case data lives in case_instances.data JSONB.',     'pyWorkPage.ClaimAmount → case.data.claim_amount',       'close',   NULL),
('pega', 'Data Page',         'API endpoint',      'endpoint',      'Cached data source. In Helix, data is fetched from API endpoints with caching at the HTTP layer.',     'D_ClaimList data page → GET /api/v1/cases endpoint',   'partial', 'Caching strategy differs'),

-- Access and security
('pega', 'Access Group',      'access_group',      'access_group',  'Groups operators and defines what they can see and do. Direct equivalent in Helix.',                  'ClaimsAdjusters → access_group: claims_adjusters',     'exact',   NULL),
('pega', 'Operator',          'operator / user',   NULL,            'A person who uses the Pega application. Maps to Helix operator with access group memberships.',       'adjuster@example.com → operator in Claims Adjusters',  'exact',   NULL),
('pega', 'Worklist',          'Work Center queue', NULL,            'Personal queue of assignments for an operator. Helix equivalent is the Work Center module.',          'Worklist → /work-center',                               'exact',   NULL),
('pega', 'Work Queue',        'queue',             NULL,            'Shared queue that multiple operators can pull from. Helix queues are defined per access group.',       'ClaimsQueue → queue in Claims Adjusters access group',  'exact',   NULL),
('pega', 'Role',              'access_role',       NULL,            'Named set of privileges. Roles are assigned to access groups in both Pega and Helix.',               'AdjusterRole → access_role with claim_update privilege', 'exact',  NULL),

-- SLA and escalation
('pega', 'Service Level',     'sla_rule',          NULL,            'Defines goal, deadline, and passed-deadline (breach) timers. Direct equivalent in Helix.',            'ClaimSLA → sla_rule with 24h deadline',                 'exact',   NULL),
('pega', 'Urgency',           'priority',          NULL,            'Numeric urgency in Pega maps to priority levels in Helix (low/medium/high/critical).',               'Urgency 50 → priority: high',                           'close',   'Pega uses 0-100 scale; Helix uses named tiers'),

-- Integration and rules
('pega', 'Connector',         'HxConnect connector', 'connector',   'Integration with an external system. Maps to a HxConnect ConnectorProtocol implementation.',         'SalesforceConnector → HxConnect Salesforce adapter',   'close',   'Connector logic must be re-implemented in HxConnect'),
('pega', 'Correspondence',    'email template',    NULL,            'Outbound email template. Maps to Helix email templates (P25).',                                       'ClaimConfirmation → email template: claim_confirmation', 'exact',  NULL),
('pega', 'Decision Table',    'routing rule',      NULL,            'Tabular IF/THEN logic. Converted to Helix conditional routing rules.',                               'PremiumCalc table → routing rule with conditions',      'close',   'Complex tables may need manual conversion'),
('pega', 'Ruleset',           'case_type version', NULL,            'A versioned container of rules. Maps to case_type version + migration file.',                        'InsuranceRuleset 01-01-01 → case_type v1.0 + migration', 'close',  NULL),
('pega', 'Portal',            'portal',            NULL,            'Customer-facing or staff-facing UI portal. Direct equivalent in Helix.',                             'CustomerPortal → portal: customer',                     'exact',   NULL);

-- ─── Seed: Camunda → Helix ───────────────────────────────────────────────────

INSERT INTO bpm_concepts (source_tool, source_concept, helix_equiv, helix_node_type, description, example, confidence, notes) VALUES

('camunda', 'Process Definition',  'case_type',         'case_type',  'The top-level BPMN process. Maps to a Helix case_type with stages derived from swim lanes or logical groups.', 'LoanProcess.bpmn → case_type: loan_application',      'close',   'BPMN flow is more rigid than Helix stages; restructuring required'),
('camunda', 'UserTask',            'user_task step',    'step',       'Human task requiring operator input. Direct equivalent.',                                                        'ReviewTask → user_task step: review',                 'exact',   NULL),
('camunda', 'ServiceTask',         'automated step',    'step',       'Automated task calling a service. Maps to Helix automated step or HxConnect connector call.',                   'PaymentServiceTask → automated step + connector',     'close',   'Java delegate logic must be re-implemented'),
('camunda', 'StartEvent',          'case creation',     NULL,         'Marks the beginning of a process. Maps to case instantiation in Helix.',                                        'StartEvent → case created via portal or API',         'exact',   NULL),
('camunda', 'EndEvent',            'case resolution',   NULL,         'Marks process completion. Maps to case status = resolved/closed.',                                               'EndEvent → case.status = resolved',                   'exact',   NULL),
('camunda', 'ExclusiveGateway',    'routing rule',      NULL,         'XOR decision — one path taken. Maps to conditional routing in Helix.',                                          'ApprovalGateway → routing rule: if approved → next',  'close',   NULL),
('camunda', 'ParallelGateway',     'parallel stages',   NULL,         'AND split/join — multiple paths run simultaneously. Helix handles with parallel stages.',                       'ParallelGateway → two stages running concurrently',   'partial', 'Full parallel execution needs HxFusion (P47)'),
('camunda', 'CallActivity',        'child case',        'case_type',  'Calls a reusable sub-process. Maps to spawning a child case in Helix.',                                         'DocumentVerification call → child case_type',         'close',   NULL),
('camunda', 'BoundaryEvent (timer)', 'sla_rule',        NULL,         'Timer boundary on a task maps to a Helix SLA rule with escalation.',                                            'TimerBoundary 48h → sla_rule deadline 48h',           'close',   NULL),
('camunda', 'MessageEvent',        'notification',      NULL,         'Send/receive message maps to Helix email (P25) or push notification (P27).',                                    'MessageEnd → email notification to customer',         'close',   NULL),
('camunda', 'ScriptTask',          'automated step',    'step',       'Inline script execution. Logic must be re-expressed as Helix automated step.',                                   'GroovyScript → automated step',                       'partial', 'Script logic requires manual migration'),
('camunda', 'BusinessRuleTask',    'routing rule',      NULL,         'DMN decision table evaluation. Maps to Helix routing rules.',                                                    'DMNTask → routing rule with conditions',              'close',   NULL),
('camunda', 'Subprocess',          'embedded stage',    'stage',      'Embedded sub-process. Maps to a sub-stage group in Helix.',                                                     'InlineSubprocess → embedded stage group',             'close',   'Full subprocess support in HxFusion (P47)'),
('camunda', 'SequenceFlow',        'stage transition',  NULL,         'Arrow connecting elements. Maps to stage/step ordering in Helix definition_json.',                              'SequenceFlow A→B → stage order + step order',         'exact',   NULL);

-- ─── Seed: Appian → Helix ───────────────────────────────────────────────────

INSERT INTO bpm_concepts (source_tool, source_concept, helix_equiv, helix_node_type, description, example, confidence, notes) VALUES

('appian', 'Process Model',        'case_type',         'case_type',  'Top-level Appian process. Maps to a Helix case_type.',                                                          'LoanApprovalProcess → case_type: loan_approval',      'close',   NULL),
('appian', 'User Input Task',      'user_task step',    'step',       'Human task in Appian. Direct equivalent to Helix user_task step.',                                              'ReviewApplicationTask → user_task step',              'exact',   NULL),
('appian', 'Automated Task',       'automated step',    'step',       'System-executed task. Maps to Helix automated step.',                                                            'SendEmailTask → automated step',                      'exact',   NULL),
('appian', 'Record Type',          'case_type',         'case_type',  'Appian Record = a data entity with a lifecycle. Maps to case_type + case_instances.',                          'CustomerRecord → case_type + case_instances table',   'close',   'Appian Record views map to Helix case detail panels'),
('appian', 'Interface',            'form',              'form',       'Appian UI form definition. Maps to Helix FormDefinition JSON.',                                                  'LoanApplicationInterface → FormDefinition: loan_app',  'close',   'Appian SAIL expressions need re-expression'),
('appian', 'Expression Rule',      'routing rule',      NULL,         'Reusable expression/formula. Maps to Helix routing rules or field calculated values.',                          'EligibilityRule → routing rule condition',            'close',   NULL),
('appian', 'Decision',             'routing rule',      NULL,         'If/else decision node. Maps to Helix stage conditional routing.',                                               'ApprovalDecision → routing rule: if approved → done', 'close',   NULL),
('appian', 'Constant',             'configuration',     NULL,         'Named constant value. Store in Helix case_type properties or tenant settings.',                                 'MAX_LOAN_AMOUNT → case_type property',                'close',   NULL),
('appian', 'Site',                 'portal',            NULL,         'Appian customer-facing site. Maps to Helix portal.',                                                             'CustomerSite → portal: customer',                     'exact',   NULL),
('appian', 'Group',                'access_group',      'access_group', 'Appian group for access control. Direct equivalent.',                                                         'LoanOfficersGroup → access_group: loan_officers',     'exact',   NULL),
('appian', 'Role',                 'access_role',       NULL,         'Named privilege set. Maps to Helix access_role.',                                                                'ApproverRole → access_role: approver',                'exact',   NULL),
('appian', 'Integration Object',   'HxConnect connector', 'connector', 'External system integration. Maps to HxConnect ConnectorProtocol.',                                           'SalesforceIntegration → HxConnect connector',         'close',   'Integration logic must be re-implemented'),
('appian', 'Report',               'Analytics view',    NULL,         'Appian report/grid. Maps to Helix Analytics module or HxGraph query.',                                          'ClaimsReport → Analytics module + custom query',      'close',   NULL),
('appian', 'Timer Event',          'sla_rule',          NULL,         'Time-based trigger. Maps to SLA rule with action on deadline.',                                                 'EscalationTimer 48h → sla_rule + escalation',         'close',   NULL);

-- ─── Seed: ServiceNow → Helix ───────────────────────────────────────────────

INSERT INTO bpm_concepts (source_tool, source_concept, helix_equiv, helix_node_type, description, example, confidence, notes) VALUES

('servicenow', 'Workflow',           'case_type stages',  NULL,         'ServiceNow workflow = sequence of activities. Maps to Helix case_type stage sequence.',                  'IncidentWorkflow → case_type: incident stages',       'close',   NULL),
('servicenow', 'Activity',           'step',              'step',       'Single unit of work in a workflow. Maps to a Helix step.',                                                  'ApprovalActivity → approval step',                    'close',   NULL),
('servicenow', 'Approval',           'approval step',     'step',       'Approval request to a user or group. Direct equivalent.',                                                   'ManagerApproval → approval step',                     'exact',   NULL),
('servicenow', 'Catalog Item',       'portal case_type',  'case_type',  'Service Catalog item = customer-submittable request. Maps to case_type with portal enabled.',              'LaptopRequest → portal-enabled case_type',            'exact',   NULL),
('servicenow', 'Flow Designer Flow', 'case_type stages',  NULL,         'Visual flow builder. Maps to Helix stage + step definition.',                                               'OnboardingFlow → case_type: onboarding',              'close',   NULL),
('servicenow', 'Assignment Group',   'access_group',      'access_group', 'Group that work is assigned to. Direct equivalent to Helix access_group.',                              'IT_Support → access_group: it_support',               'exact',   NULL),
('servicenow', 'SLA Definition',     'sla_rule',          NULL,         'Response and resolution SLA timers. Direct equivalent.',                                                    'P1_SLA 4h response → sla_rule deadline 4h',           'exact',   NULL),
('servicenow', 'Business Rule',      'automated step',    'step',       'Script triggered on record events. Maps to automated step with logic.',                                     'AssignmentRule → automated step on case create',      'partial', 'Script logic requires manual migration'),
('servicenow', 'UI Action',          'form step action',  'step',       'Button on a form. Maps to a form step action (approve/reject/submit).',                                     'ResolveButton → form step action: resolve',           'close',   NULL),
('servicenow', 'Transform Map',      'automated step',    'step',       'Data transformation script. Maps to automated step.',                                                       'ImportTransform → automated step: data_map',          'partial', 'Transform logic requires manual migration'),
('servicenow', 'Scheduled Job',      'sla_rule escalation', NULL,       'Time-based recurring action. Maps to SLA escalation rule.',                                                 'DailyEscalation → sla_rule escalation action',        'close',   NULL),
('servicenow', 'Knowledge Article',  'Knowledge Center',  NULL,         'Knowledge base article. Maps to HxGraph concept node + Knowledge Center entry.',                           'HowToArticle → knowledge_center entry / concept node', 'close',  NULL);

COMMIT;
