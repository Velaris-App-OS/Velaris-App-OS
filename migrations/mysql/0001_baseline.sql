-- ============================================================================
-- Velaris — MySQL 8 / MariaDB 10.6+ consolidated baseline (DB SDK Phase 1).
--
-- Generated from the SQLAlchemy ORM metadata (case_service.db.models) compiled
-- to the MySQL dialect — the same DDL `Base.metadata.create_all` emits. This is
-- the MySQL equivalent of the Postgres `migrations/postgresql/*.sql` track: ONE baseline
-- instead of the 80+ incremental PG files (Velaris ships fresh on MySQL; there
-- is no in-place PG→MySQL upgrade path — that is HxDBMigrate's job).
--
-- Recommended: create the database as utf8mb4
--   (CREATE DATABASE velaris CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;)
-- The indexed-identifier columns are bounded for InnoDB's 3072-byte key limit at
-- utf8mb4's 4 bytes/char, so this schema is safe on any charset.
--
-- DO NOT EDIT BY HAND. Regenerate via scripts/gen_mysql_baseline.py after a model
-- change, and hand-review the diff.
-- ============================================================================

SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE access_roles (
	id CHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	description TEXT NOT NULL, 
	privileges JSON NOT NULL, 
	tenant_id VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT access_roles_name_tenant_uq UNIQUE (name, tenant_id)
);

CREATE INDEX idx_access_roles_tenant ON access_roles (tenant_id);

CREATE TABLE app_packages (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	version VARCHAR(50) NOT NULL, 
	description TEXT, 
	bundle JSON NOT NULL, 
	manifest JSON NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_app_packages_name_version UNIQUE (name, version)
);

CREATE INDEX ix_app_packages_created ON app_packages (created_at);

CREATE INDEX ix_app_packages_status ON app_packages (status);

CREATE TABLE audit_anchors (
	id CHAR(36) NOT NULL, 
	tip_sequence INTEGER NOT NULL, 
	tip_hash VARCHAR(64) NOT NULL, 
	tsa_url VARCHAR(512) NOT NULL, 
	tsr_der BLOB NOT NULL, 
	anchored_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_audit_anchors_tip_seq ON audit_anchors (tip_sequence);

CREATE INDEX ix_audit_anchors_anchored_at ON audit_anchors (anchored_at);

CREATE TABLE auth_devices (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	device_name VARCHAR(255) NOT NULL, 
	user_agent_hash VARCHAR(64) NOT NULL, 
	first_ip VARCHAR(64), 
	last_ip VARCHAR(64), 
	created_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	revoked_at DATETIME, 
	revoked_by VARCHAR(255), 
	PRIMARY KEY (id)
);

CREATE INDEX ix_auth_devices_user ON auth_devices (user_id);

CREATE TABLE bpm_concepts (
	id CHAR(36) NOT NULL, 
	source_tool VARCHAR(50) NOT NULL, 
	source_concept VARCHAR(255) NOT NULL, 
	helix_equiv VARCHAR(255) NOT NULL, 
	helix_node_type VARCHAR(50), 
	description TEXT NOT NULL, 
	example TEXT, 
	confidence VARCHAR(10) NOT NULL, 
	notes TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_bpm_concepts_concept ON bpm_concepts (source_concept);

CREATE INDEX ix_bpm_concepts_confidence ON bpm_concepts (confidence);

CREATE INDEX ix_bpm_concepts_tool ON bpm_concepts (source_tool);

CREATE TABLE branch_audit_events (
	id CHAR(36) NOT NULL, 
	branch_id CHAR(36) NOT NULL, 
	event_type VARCHAR(60) NOT NULL, 
	actor_id TEXT, 
	actor_name TEXT, 
	metadata JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_bae_branch ON branch_audit_events (branch_id);

CREATE INDEX ix_bae_created ON branch_audit_events (created_at);

CREATE TABLE business_calendars (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	timezone VARCHAR(100) NOT NULL, 
	work_days JSON NOT NULL, 
	work_start_hour INTEGER NOT NULL, 
	work_end_hour INTEGER NOT NULL, 
	holidays JSON NOT NULL, 
	description TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE case_audit_log_chain (
	id CHAR(36) NOT NULL, 
	sequence INTEGER NOT NULL, 
	audit_log_id CHAR(36) NOT NULL, 
	prev_hash VARCHAR(64) NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	sealed_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (sequence)
);

CREATE INDEX idx_audit_chain_audit_id ON case_audit_log_chain (audit_log_id);

CREATE INDEX idx_audit_chain_seq ON case_audit_log_chain (sequence);

CREATE INDEX ix_case_audit_log_chain_sealed_at ON case_audit_log_chain (sealed_at);

CREATE TABLE case_time_entries (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	case_id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	`role` VARCHAR(100), 
	source VARCHAR(20) NOT NULL, 
	started_at DATETIME, 
	ended_at DATETIME, 
	duration_seconds INTEGER NOT NULL, 
	billable BOOL NOT NULL, 
	note TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_time_entries_case ON case_time_entries (case_id);

CREATE INDEX ix_time_entries_tenant ON case_time_entries (tenant_id);

CREATE INDEX ix_time_entries_user ON case_time_entries (user_id);

CREATE TABLE case_type_migrations (
	id CHAR(36) NOT NULL, 
	case_type_id CHAR(36) NOT NULL, 
	run_id CHAR(36), 
	source_platform VARCHAR(100) NOT NULL, 
	source_filename VARCHAR(500) NOT NULL, 
	imported_by_user_id TEXT NOT NULL, 
	imported_by_email TEXT NOT NULL, 
	imported_at DATETIME NOT NULL, 
	stages_count INTEGER NOT NULL, 
	steps_count INTEGER NOT NULL, 
	forms_count INTEGER NOT NULL, 
	rules_count INTEGER NOT NULL, 
	slas_count INTEGER NOT NULL, 
	notes TEXT NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_case_type_migrations_case_type_id ON case_type_migrations (case_type_id);

CREATE TABLE checkout_service_tokens (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	label VARCHAR(255) NOT NULL, 
	token_hash VARCHAR(255) NOT NULL, 
	token_prefix VARCHAR(24) NOT NULL, 
	scope VARCHAR(50) NOT NULL, 
	last_used_at DATETIME, 
	revoked_at DATETIME, 
	suspended BOOL NOT NULL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_checkout_tokens_prefix UNIQUE (token_prefix)
);

CREATE INDEX ix_checkout_tokens_tenant ON checkout_service_tokens (tenant_id);

CREATE TABLE checkout_webhook_integrations (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	platform VARCHAR(50) NOT NULL, 
	label VARCHAR(255) NOT NULL, 
	hmac_secret_enc TEXT, 
	field_map JSON NOT NULL, 
	enabled BOOL NOT NULL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_checkout_integrations_tenant ON checkout_webhook_integrations (tenant_id);

CREATE TABLE compliance_reports (
	id CHAR(36) NOT NULL, 
	framework VARCHAR(32) NOT NULL, 
	period_start DATETIME NOT NULL, 
	period_end DATETIME NOT NULL, 
	generated_by VARCHAR(255), 
	generated_at DATETIME NOT NULL, 
	summary JSON NOT NULL, 
	storage_key_json VARCHAR(1024), 
	storage_key_pdf VARCHAR(1024), 
	chain_verified BOOL NOT NULL, 
	cadence VARCHAR(16) NOT NULL, 
	tenant_id VARCHAR(64), 
	PRIMARY KEY (id)
);

CREATE INDEX idx_compliance_framework ON compliance_reports (framework);

CREATE INDEX idx_compliance_generated_at ON compliance_reports (generated_at);

CREATE TABLE component_commits (
	id CHAR(36) NOT NULL, 
	component_type VARCHAR(64) NOT NULL, 
	component_id VARCHAR(255) NOT NULL, 
	component_name TEXT NOT NULL, 
	commit_message TEXT NOT NULL, 
	committed_by VARCHAR(255) NOT NULL, 
	diff_snapshot JSON, 
	story_matches JSON, 
	committed_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_cc_committed ON component_commits (committed_at);

CREATE INDEX ix_cc_component ON component_commits (component_type, component_id);

CREATE INDEX ix_cc_user ON component_commits (committed_by);

CREATE TABLE connector_registry (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	connector_type VARCHAR(100) NOT NULL, 
	description TEXT, 
	config_schema JSON NOT NULL, 
	config JSON NOT NULL, 
	credentials JSON NOT NULL, 
	tenant_id VARCHAR(255), 
	enabled BOOL NOT NULL, 
	last_tested_at DATETIME, 
	last_test_ok BOOL, 
	credential_expires_at DATETIME, 
	credentials_updated_at DATETIME, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_connector_name_tenant UNIQUE (name, tenant_id)
);

CREATE INDEX ix_connector_enabled ON connector_registry (enabled);

CREATE INDEX ix_connector_tenant ON connector_registry (tenant_id);

CREATE INDEX ix_connector_type ON connector_registry (connector_type);

CREATE TABLE data_lineage_events (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	kind VARCHAR(64) NOT NULL, 
	field_path VARCHAR(512), 
	before_value JSON, 
	after_value JSON, 
	actor_id VARCHAR(255), 
	source VARCHAR(64) NOT NULL, 
	at DATETIME NOT NULL, 
	tenant_id VARCHAR(64), 
	PRIMARY KEY (id)
);

CREATE INDEX idx_lineage_at ON data_lineage_events (at);

CREATE INDEX idx_lineage_case ON data_lineage_events (case_id);

CREATE INDEX idx_lineage_kind ON data_lineage_events (kind);

CREATE TABLE data_models (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	version VARCHAR(50) NOT NULL, 
	definition_json JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name, version)
);

CREATE TABLE db_manager_query_log (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	query_text TEXT NOT NULL, 
	query_hash VARCHAR(64) NOT NULL, 
	duration_ms INTEGER, 
	rows_affected INTEGER, 
	status VARCHAR(20) NOT NULL, 
	error_detail TEXT, 
	ran_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_dbmgr_log_hash ON db_manager_query_log (query_hash);

CREATE INDEX ix_dbmgr_log_ran_at ON db_manager_query_log (ran_at);

CREATE INDEX ix_dbmgr_log_tenant ON db_manager_query_log (tenant_id);

CREATE INDEX ix_dbmgr_log_user ON db_manager_query_log (user_id);

CREATE TABLE dml_before_image (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	operation VARCHAR(10) NOT NULL, 
	table_hint VARCHAR(255), 
	original_sql TEXT NOT NULL, 
	old_rows JSON, 
	new_rows JSON, 
	row_count INTEGER, 
	capture_method VARCHAR(30) NOT NULL, 
	captured_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_dml_before_captured ON dml_before_image (captured_at);

CREATE INDEX ix_dml_before_tenant ON dml_before_image (tenant_id);

CREATE INDEX ix_dml_before_user ON dml_before_image (user_id);

CREATE TABLE email_accounts (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	address VARCHAR(320) NOT NULL, 
	smtp_host VARCHAR(255) NOT NULL, 
	smtp_port INTEGER NOT NULL, 
	smtp_username VARCHAR(255), 
	smtp_password VARCHAR(1024), 
	smtp_use_tls BOOL NOT NULL, 
	imap_host VARCHAR(255), 
	imap_port INTEGER NOT NULL, 
	imap_username VARCHAR(255), 
	imap_password VARCHAR(1024), 
	imap_use_ssl BOOL NOT NULL, 
	imap_folder VARCHAR(255) NOT NULL, 
	poll_interval_seconds INTEGER NOT NULL, 
	is_active BOOL NOT NULL, 
	is_default_outbound BOOL NOT NULL, 
	tenant_id VARCHAR(64), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_email_accounts_active ON email_accounts (is_active);

CREATE INDEX idx_email_accounts_tenant ON email_accounts (tenant_id);

CREATE TABLE email_messages (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36), 
	direction VARCHAR(16) NOT NULL, 
	account_id CHAR(36), 
	message_id VARCHAR(998), 
	in_reply_to VARCHAR(998), 
	`references` JSON NOT NULL, 
	from_address VARCHAR(320) NOT NULL, 
	to_addresses JSON NOT NULL, 
	cc_addresses JSON NOT NULL, 
	subject TEXT NOT NULL, 
	body_text TEXT NOT NULL, 
	body_html TEXT, 
	raw_headers JSON NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	error_message TEXT, 
	is_read BOOL NOT NULL, 
	sent_at DATETIME, 
	received_at DATETIME, 
	created_at DATETIME NOT NULL, 
	tenant_id VARCHAR(64), 
	PRIMARY KEY (id)
);

CREATE INDEX idx_email_messages_case ON email_messages (case_id);

CREATE INDEX idx_email_messages_direction ON email_messages (direction);

CREATE INDEX idx_email_messages_msgid ON email_messages (message_id(255));

CREATE INDEX idx_email_messages_received ON email_messages (received_at);

CREATE INDEX idx_email_messages_status ON email_messages (status);

CREATE INDEX idx_email_messages_unread ON email_messages (is_read);

CREATE TABLE environment_registry (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	label TEXT NOT NULL, 
	url TEXT, 
	order_index INTEGER NOT NULL, 
	current_package_id CHAR(36), 
	current_version TEXT, 
	status VARCHAR(50) NOT NULL, 
	last_deployed_at DATETIME, 
	api_token_enc JSON, 
	connection_verified_at DATETIME, 
	delivery_method VARCHAR(20) NOT NULL, 
	webhook_url TEXT, 
	webhook_secret TEXT, 
	import_api_key TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_env_name_tenant ON environment_registry (tenant_id, name);

CREATE INDEX ix_env_tenant ON environment_registry (tenant_id);

CREATE TABLE gdpr_requests (
	id CHAR(36) NOT NULL, 
	subject_id VARCHAR(255) NOT NULL, 
	request_type VARCHAR(50) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	requested_by VARCHAR(255), 
	reason TEXT, 
	result_file TEXT, 
	completed_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE generated_docs (
	id CHAR(36) NOT NULL, 
	doc_type VARCHAR(50) NOT NULL, 
	content TEXT NOT NULL, 
	generated_at DATETIME NOT NULL, 
	node_count INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (doc_type)
);

CREATE TABLE graph_nodes (
	id CHAR(36) NOT NULL, 
	node_type VARCHAR(50) NOT NULL, 
	name VARCHAR(500) NOT NULL, 
	label VARCHAR(500) NOT NULL, 
	source VARCHAR(20) NOT NULL, 
	properties JSON NOT NULL, 
	summary TEXT, 
	embedding JSON, 
	community_id INTEGER, 
	tenant_id VARCHAR(255), 
	last_synced_at DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_graph_nodes_community ON graph_nodes (community_id);

CREATE INDEX ix_graph_nodes_name ON graph_nodes (name);

CREATE INDEX ix_graph_nodes_source ON graph_nodes (source);

CREATE INDEX ix_graph_nodes_tenant ON graph_nodes (tenant_id);

CREATE INDEX ix_graph_nodes_type ON graph_nodes (node_type);

CREATE TABLE health_check_results (
	id CHAR(36) NOT NULL, 
	component VARCHAR(64) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	latency_ms FLOAT, 
	detail JSON NOT NULL, 
	checked_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_health_check_results_checked_at ON health_check_results (checked_at);

CREATE INDEX ix_health_check_results_component ON health_check_results (component);

CREATE INDEX ix_health_check_results_status ON health_check_results (status);

CREATE TABLE helix_settings (
	`key` VARCHAR(255) NOT NULL, 
	value TEXT NOT NULL, 
	updated_at DATETIME NOT NULL, 
	updated_by TEXT, 
	PRIMARY KEY (`key`)
);

CREATE TABLE helix_users (
	id CHAR(36) NOT NULL, 
	username VARCHAR(255) NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	display_name TEXT, 
	password_hash TEXT, 
	roles JSON NOT NULL, 
	is_superadmin BOOL NOT NULL, 
	is_active BOOL NOT NULL, 
	failed_attempts INTEGER NOT NULL, 
	locked_until DATETIME, 
	password_change_required BOOL NOT NULL, 
	mfa_enabled BOOL NOT NULL, 
	mfa_secret_enc JSON, 
	sso_provider VARCHAR(255), 
	sso_subject VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	last_login_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (username), 
	UNIQUE (email)
);

CREATE UNIQUE INDEX ix_helix_users_email ON helix_users (email);

CREATE INDEX ix_helix_users_sso ON helix_users (sso_provider, sso_subject);

CREATE UNIQUE INDEX ix_helix_users_username ON helix_users (username);

CREATE TABLE hxdbmigrate_analyses (
	id CHAR(36) NOT NULL, 
	source_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	status VARCHAR(32) NOT NULL, 
	table_count INTEGER, 
	quality_score INTEGER, 
	pii_count INTEGER, 
	report JSON NOT NULL, 
	error TEXT, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_hxdbmig_an_created ON hxdbmigrate_analyses (created_at);

CREATE INDEX ix_hxdbmig_an_source ON hxdbmigrate_analyses (source_id);

CREATE INDEX ix_hxdbmig_an_tenant ON hxdbmigrate_analyses (tenant_id);

CREATE TABLE hxdbmigrate_migration_runs (
	id CHAR(36) NOT NULL, 
	source_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	table_name VARCHAR(255) NOT NULL, 
	case_type_id CHAR(36), 
	kind VARCHAR(16) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	pii_mode VARCHAR(16) NOT NULL, 
	dry_run BOOL NOT NULL, 
	rows_read INTEGER, 
	rows_migrated INTEGER, 
	rows_updated INTEGER, 
	excluded_columns JSON NOT NULL, 
	error TEXT, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_hxdbmig_run_created ON hxdbmigrate_migration_runs (created_at);

CREATE INDEX ix_hxdbmig_run_source ON hxdbmigrate_migration_runs (source_id);

CREATE INDEX ix_hxdbmig_run_tenant ON hxdbmigrate_migration_runs (tenant_id);

CREATE TABLE hxdbmigrate_row_links (
	id CHAR(36) NOT NULL, 
	source_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	table_name VARCHAR(255) NOT NULL, 
	source_pk VARCHAR(512) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	case_type_id CHAR(36), 
	row_checksum VARCHAR(64), 
	last_synced_at DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_hxdbmig_link_row UNIQUE (source_id, table_name, source_pk)
);

CREATE INDEX ix_hxdbmig_link_case ON hxdbmigrate_row_links (case_id);

CREATE INDEX ix_hxdbmig_link_source ON hxdbmigrate_row_links (source_id);

CREATE INDEX ix_hxdbmig_link_tenant ON hxdbmigrate_row_links (tenant_id);

CREATE TABLE hxdbmigrate_sources (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	source_type VARCHAR(32) NOT NULL, 
	host VARCHAR(255) NOT NULL, 
	port INTEGER NOT NULL, 
	`database` VARCHAR(255) NOT NULL, 
	username VARCHAR(255) NOT NULL, 
	ssl_mode VARCHAR(16) NOT NULL, 
	credentials JSON NOT NULL, 
	tenant_id VARCHAR(255), 
	status VARCHAR(16) NOT NULL, 
	cutover_at DATETIME, 
	rollback_window_hours INTEGER NOT NULL, 
	last_connected_at DATETIME, 
	last_connect_ok BOOL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_hxdbmig_src_name_tenant UNIQUE (name, tenant_id)
);

CREATE INDEX ix_hxdbmig_src_tenant ON hxdbmigrate_sources (tenant_id);

CREATE TABLE hxdocs_spaces (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name TEXT NOT NULL, 
	slug VARCHAR(200) NOT NULL, 
	description TEXT, 
	is_public BOOL NOT NULL, 
	created_by TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_hxds_tenant ON hxdocs_spaces (tenant_id);

CREATE TABLE hxevolve_baselines (
	case_type_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	metrics JSON NOT NULL, 
	merged_at_baseline INTEGER NOT NULL, 
	checked_through INTEGER NOT NULL, 
	frozen BOOL NOT NULL, 
	frozen_reason TEXT, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	rebaselined_at DATETIME, 
	PRIMARY KEY (case_type_id)
);

CREATE TABLE hxevolve_config (
	case_type_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	min_improvement FLOAT NOT NULL, 
	max_auto_ratio_rise FLOAT NOT NULL, 
	min_coverage FLOAT NOT NULL, 
	min_determinate INTEGER NOT NULL, 
	scan_frequency_hours INTEGER NOT NULL, 
	scan_enabled BOOL NOT NULL, 
	drift_check_every_n_changes INTEGER NOT NULL, 
	updated_by VARCHAR(255), 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (case_type_id)
);

CREATE INDEX ix_hxevolve_cfg_enabled ON hxevolve_config (scan_enabled);

CREATE INDEX ix_hxevolve_cfg_tenant ON hxevolve_config (tenant_id);

CREATE TABLE hxevolve_insights (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	case_type_id CHAR(36) NOT NULL, 
	`signal` JSON NOT NULL, 
	proposal JSON NOT NULL, 
	proposal_kind VARCHAR(32), 
	evidence JSON, 
	evidence_kind VARCHAR(16), 
	replay_run_id CHAR(36), 
	rationale TEXT, 
	status VARCHAR(32) NOT NULL, 
	branch_id CHAR(36), 
	staged_rule_id CHAR(36), 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_hxevolve_ins_created ON hxevolve_insights (created_at);

CREATE INDEX ix_hxevolve_ins_ct ON hxevolve_insights (case_type_id);

CREATE INDEX ix_hxevolve_ins_status ON hxevolve_insights (status);

CREATE INDEX ix_hxevolve_ins_tenant ON hxevolve_insights (tenant_id);

CREATE TABLE hxguard_tuples (
	id CHAR(36) NOT NULL, 
	object_type VARCHAR(30) NOT NULL, 
	object_id CHAR(36) NOT NULL, 
	relation VARCHAR(30) NOT NULL, 
	subject_type VARCHAR(30) NOT NULL, 
	subject_id VARCHAR(255) NOT NULL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_hxguard_tuple UNIQUE (object_type, object_id, relation, subject_type, subject_id)
);

CREATE INDEX ix_hxg_tuples_object ON hxguard_tuples (object_type, object_id);

CREATE INDEX ix_hxg_tuples_subject ON hxguard_tuples (subject_type, subject_id);

CREATE TABLE hxnexus_conversations (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36), 
	tenant_id VARCHAR(64), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_hxnexus_conv_case ON hxnexus_conversations (case_id);

CREATE INDEX idx_hxnexus_conv_user ON hxnexus_conversations (user_id);

CREATE TABLE hxnexus_document_chunks (
	id CHAR(36) NOT NULL, 
	document_id CHAR(36), 
	case_id CHAR(36), 
	chunk_index INTEGER NOT NULL, 
	chunk_text TEXT NOT NULL, 
	embedding JSON NOT NULL, 
	tenant_id VARCHAR(64), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_hxnexus_chunk_case ON hxnexus_document_chunks (case_id);

CREATE INDEX idx_hxnexus_chunk_document ON hxnexus_document_chunks (document_id);

CREATE INDEX idx_hxnexus_chunk_tenant ON hxnexus_document_chunks (tenant_id);

CREATE TABLE hxtest_results (
	id CHAR(36) NOT NULL, 
	run_id CHAR(36) NOT NULL, 
	test_id VARCHAR(200) NOT NULL, 
	test_name VARCHAR(300), 
	status VARCHAR(20) NOT NULL, 
	duration_ms INTEGER NOT NULL, 
	error_detail TEXT, 
	step_results JSON NOT NULL, 
	ran_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE hxtest_runs (
	id CHAR(36) NOT NULL, 
	suite_id CHAR(36), 
	suite_name VARCHAR(200), 
	triggered_by VARCHAR(255), 
	tenant_id VARCHAR(255), 
	status VARCHAR(20) NOT NULL, 
	total INTEGER NOT NULL, 
	passed INTEGER NOT NULL, 
	failed INTEGER NOT NULL, 
	skipped INTEGER NOT NULL, 
	app_package_id CHAR(36), 
	ephemeral_tenant_id VARCHAR(255), 
	started_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE hxtest_suites (
	id CHAR(36) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	suite_type VARCHAR(30) NOT NULL, 
	source VARCHAR(20) NOT NULL, 
	case_type_id CHAR(36), 
	definition JSON NOT NULL, 
	version VARCHAR(40) NOT NULL, 
	ai_stale BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE import_jobs (
	id CHAR(36) NOT NULL, 
	tool VARCHAR(50) NOT NULL, 
	filename VARCHAR(500) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	pass1_result JSON NOT NULL, 
	pass2_result JSON NOT NULL, 
	pass3_result JSON NOT NULL, 
	pass4_result JSON NOT NULL, 
	report JSON NOT NULL, 
	error TEXT, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_import_jobs_created ON import_jobs (created_at);

CREATE INDEX ix_import_jobs_status ON import_jobs (status);

CREATE INDEX ix_import_jobs_tool ON import_jobs (tool);

CREATE TABLE marketplace_access_rules (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	access_group_id VARCHAR(255) NOT NULL, 
	rule_type VARCHAR(32) NOT NULL, 
	allowed_package_ids TEXT NOT NULL, 
	blocked_package_ids TEXT NOT NULL, 
	updated_by VARCHAR(255) NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mar_tenant_group UNIQUE (tenant_id, access_group_id)
);

CREATE INDEX ix_mar_tenant ON marketplace_access_rules (tenant_id);

CREATE TABLE marketplace_blacklist (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	type VARCHAR(32) NOT NULL, 
	value VARCHAR(1024) NOT NULL, 
	reason TEXT NOT NULL, 
	blacklisted_by VARCHAR(255) NOT NULL, 
	notify_velaris BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_mbl_tenant ON marketplace_blacklist (tenant_id);

CREATE INDEX ix_mbl_type_value ON marketplace_blacklist (type, value(500));

CREATE TABLE marketplace_installs (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	package_id VARCHAR(255) NOT NULL, 
	package_version VARCHAR(64) NOT NULL, 
	package_type VARCHAR(64) NOT NULL, 
	licence_key_enc TEXT, 
	licence_expires VARCHAR(32), 
	approved_by VARCHAR(255) NOT NULL, 
	workspace_id CHAR(36), 
	installed_at DATETIME NOT NULL, 
	revoked_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mi_tenant_package UNIQUE (tenant_id, package_id)
);

CREATE INDEX ix_mi_tenant ON marketplace_installs (tenant_id);

CREATE TABLE marketplace_release_requests (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	package_id VARCHAR(255) NOT NULL, 
	package_version VARCHAR(64) NOT NULL, 
	requested_by VARCHAR(255) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	included_in_deploy_id VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	deployed_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mrr_tenant_package_status UNIQUE (tenant_id, package_id, status)
);

CREATE INDEX ix_mrr_tenant ON marketplace_release_requests (tenant_id);

CREATE TABLE marketplace_sandbox_datasets (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	description TEXT, 
	data_json TEXT NOT NULL, 
	created_by VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE marketplace_sources (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	url VARCHAR(1024) CHARACTER SET ascii NOT NULL, 
	tier VARCHAR(32) NOT NULL, 
	token_enc TEXT, 
	poll_interval_hours INTEGER NOT NULL, 
	enabled BOOL NOT NULL, 
	last_polled_at DATETIME, 
	last_error TEXT, 
	package_count INTEGER NOT NULL, 
	added_by VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mks_url UNIQUE (url)
);

CREATE INDEX ix_mks_tier ON marketplace_sources (tier);

CREATE TABLE marketplace_updates (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	package_id VARCHAR(255) NOT NULL, 
	installed_version VARCHAR(64) NOT NULL, 
	available_version VARCHAR(64) NOT NULL, 
	release_notes TEXT, 
	new_outbound_domains TEXT NOT NULL, 
	fast_track BOOL NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	detected_at DATETIME NOT NULL, 
	approved_at DATETIME, 
	approved_by VARCHAR(255), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mku_tenant_package UNIQUE (tenant_id, package_id)
);

CREATE INDEX ix_mku_tenant ON marketplace_updates (tenant_id);

CREATE TABLE marketplace_workspaces (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	dataset_id CHAR(36), 
	container_id VARCHAR(255), 
	created_by VARCHAR(255) NOT NULL, 
	reviewed_by VARCHAR(255), 
	review_note TEXT, 
	created_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	submitted_at DATETIME, 
	reviewed_at DATETIME, 
	conformance_status VARCHAR(30) NOT NULL, 
	conformance_run_id CHAR(36), 
	conformance_checked_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_mw_status ON marketplace_workspaces (status);

CREATE INDEX ix_mw_tenant ON marketplace_workspaces (tenant_id);

CREATE INDEX ix_mw_user ON marketplace_workspaces (created_by);

CREATE TABLE mcp_action_proposals (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	tenant_id VARCHAR(255), 
	tool_name VARCHAR(100) NOT NULL, 
	arguments_json JSON NOT NULL, 
	case_id CHAR(36), 
	summary TEXT, 
	status VARCHAR(20) NOT NULL, 
	result_json JSON, 
	is_error BOOL NOT NULL, 
	decided_by VARCHAR(255), 
	decided_at DATETIME, 
	expires_at DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_mcp_prop_case ON mcp_action_proposals (case_id);

CREATE INDEX ix_mcp_prop_status ON mcp_action_proposals (status);

CREATE INDEX ix_mcp_prop_tenant ON mcp_action_proposals (tenant_id);

CREATE INDEX ix_mcp_prop_user ON mcp_action_proposals (user_id);

CREATE TABLE mcp_idempotency_keys (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	idempotency_key VARCHAR(255) NOT NULL, 
	tool_name VARCHAR(100) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	response_json JSON, 
	is_error BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mcp_idem_user_key UNIQUE (user_id, idempotency_key)
);

CREATE INDEX ix_mcp_idem_created ON mcp_idempotency_keys (created_at);

CREATE TABLE mcp_token_grants (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	tenant_id VARCHAR(255), 
	tools JSON NOT NULL, 
	label VARCHAR(255), 
	revoked BOOL NOT NULL, 
	revoked_at DATETIME, 
	revoked_by VARCHAR(255), 
	expires_at DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_mcp_grant_expires ON mcp_token_grants (expires_at);

CREATE INDEX ix_mcp_grant_user ON mcp_token_grants (user_id);

CREATE TABLE migration_pipeline_runs (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name TEXT NOT NULL, 
	source_platform VARCHAR(50) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	mode VARCHAR(20) NOT NULL, 
	current_stage INTEGER NOT NULL, 
	scan_id CHAR(36), 
	import_job_id CHAR(36), 
	project_id CHAR(36), 
	package_id CHAR(36), 
	source_filename TEXT, 
	source_size BIGINT, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_mpr_status ON migration_pipeline_runs (status);

CREATE INDEX ix_mpr_tenant ON migration_pipeline_runs (tenant_id);

CREATE TABLE migration_scans (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	source_platform VARCHAR(50) NOT NULL, 
	source_version VARCHAR(100), 
	filename VARCHAR(500), 
	status VARCHAR(30) NOT NULL, 
	compatibility_score FLOAT, 
	effort_weeks INTEGER, 
	artifacts_found JSON NOT NULL, 
	scan_report JSON NOT NULL, 
	error_message TEXT, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE notification_logs (
	id CHAR(36) NOT NULL, 
	device_id CHAR(36), 
	user_id VARCHAR(255) NOT NULL, 
	event_type VARCHAR(128) NOT NULL, 
	channel VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	error TEXT, 
	sent_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_notif_log_device ON notification_logs (device_id);

CREATE INDEX idx_notif_log_sent ON notification_logs (sent_at);

CREATE INDEX idx_notif_log_status ON notification_logs (status);

CREATE INDEX idx_notif_log_user ON notification_logs (user_id);

CREATE TABLE notification_preferences (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	event_type VARCHAR(128) NOT NULL, 
	channels JSON NOT NULL, 
	enabled BOOL NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX idx_notif_pref_user_event ON notification_preferences (user_id, event_type);

CREATE TABLE payment_webhook_events (
	id CHAR(36) NOT NULL, 
	provider VARCHAR(50) NOT NULL, 
	event_type VARCHAR(255), 
	provider_ref VARCHAR(255), 
	payload JSON NOT NULL, 
	verified BOOL NOT NULL, 
	processed BOOL NOT NULL, 
	error TEXT, 
	received_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_pwh_provider_ref ON payment_webhook_events (provider_ref);

CREATE INDEX ix_pwh_received ON payment_webhook_events (received_at);

CREATE TABLE platform_update_plans (
	id CHAR(36) NOT NULL, 
	resolved_version TEXT NOT NULL, 
	channel TEXT NOT NULL, 
	soak_hours INTEGER NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	halted_reason TEXT, 
	approved_by TEXT, 
	approved_at DATETIME, 
	prod_approved_by TEXT, 
	prod_approved_at DATETIME, 
	soak_started_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_puo_plans_state ON platform_update_plans (state);

CREATE TABLE platform_update_settings (
	id INTEGER NOT NULL, 
	mode TEXT NOT NULL, 
	default_soak_hours INTEGER NOT NULL, 
	calendar_id CHAR(36), 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE portal_ask_feedback (
	id CHAR(36) NOT NULL, 
	tenant_slug VARCHAR(255) NOT NULL, 
	question TEXT NOT NULL, 
	helpful BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_portal_ask_feedback_tenant ON portal_ask_feedback (tenant_slug, created_at);

CREATE TABLE portals (
	id CHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	portal_type VARCHAR(50) NOT NULL, 
	modules JSON NOT NULL, 
	homepage VARCHAR(100) NOT NULL, 
	theme JSON NOT NULL, 
	tenant_id VARCHAR(255), 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX idx_portals_tenant ON portals (tenant_id);

CREATE INDEX idx_portals_type ON portals (portal_type);

CREATE TABLE process_definitions (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	version INTEGER NOT NULL, 
	description TEXT, 
	bpmn_xml TEXT NOT NULL, 
	case_type_id VARCHAR(255), 
	status VARCHAR(20) NOT NULL, 
	created_by VARCHAR(255), 
	tenant_id VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_pd_case_type ON process_definitions (case_type_id);

CREATE INDEX ix_pd_status ON process_definitions (status);

CREATE TABLE push_device_tokens (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	channel VARCHAR(32) NOT NULL, 
	token TEXT NOT NULL, 
	platform VARCHAR(64), 
	label VARCHAR(255), 
	is_active BOOL NOT NULL, 
	last_seen_at DATETIME, 
	created_at DATETIME NOT NULL, 
	tenant_id VARCHAR(64), 
	PRIMARY KEY (id)
);

CREATE INDEX idx_push_device_active ON push_device_tokens (is_active);

CREATE INDEX idx_push_device_channel ON push_device_tokens (channel);

CREATE INDEX idx_push_device_user ON push_device_tokens (user_id);

CREATE TABLE rate_cards (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	`role` VARCHAR(100) NOT NULL, 
	hourly_rate FLOAT NOT NULL, 
	currency VARCHAR(8) NOT NULL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_rate_cards_tenant_role UNIQUE (tenant_id, `role`)
);

CREATE TABLE refresh_tokens (
	token_hash VARCHAR(64) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	jti VARCHAR(36) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	revoked_at DATETIME, 
	revoked_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	device_id CHAR(36), 
	PRIMARY KEY (token_hash)
);

CREATE INDEX ix_refresh_tokens_device ON refresh_tokens (device_id);

CREATE INDEX ix_refresh_tokens_expires ON refresh_tokens (expires_at);

CREATE INDEX ix_refresh_tokens_user ON refresh_tokens (user_id);

CREATE TABLE region_registry (
	id CHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	provider VARCHAR(20) NOT NULL, 
	location VARCHAR(100), 
	endpoint VARCHAR(500), 
	connection_config JSON NOT NULL, 
	is_primary BOOL NOT NULL, 
	enabled BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE INDEX ix_region_enabled ON region_registry (enabled);

CREATE TABLE replay_runs (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	kind VARCHAR(16) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	branch_id CHAR(36), 
	candidate JSON NOT NULL, 
	case_id CHAR(36), 
	cohort_filter JSON NOT NULL, 
	config_epoch VARCHAR(32) NOT NULL, 
	estimate BOOL NOT NULL, 
	summary JSON, 
	result_digest VARCHAR(64), 
	anchored BOOL NOT NULL, 
	error TEXT, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_replay_runs_created ON replay_runs (created_at);

CREATE INDEX ix_replay_runs_status ON replay_runs (status);

CREATE INDEX ix_replay_runs_tenant ON replay_runs (tenant_id);

CREATE TABLE retention_policies (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	resource_type VARCHAR(100) NOT NULL, 
	retention_days INTEGER NOT NULL, 
	action VARCHAR(20) NOT NULL, 
	enabled BOOL NOT NULL, 
	last_run_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (resource_type)
);

CREATE TABLE revoked_sessions (
	token_hash VARCHAR(64) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	revoked_at DATETIME NOT NULL, 
	reason TEXT, 
	revoked_by VARCHAR(255), 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (token_hash)
);

CREATE INDEX ix_revoked_sessions_expires ON revoked_sessions (expires_at);

CREATE INDEX ix_revoked_sessions_user ON revoked_sessions (user_id);

CREATE TABLE rule_definitions (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	version VARCHAR(50) NOT NULL, 
	rule_type VARCHAR(50) NOT NULL, 
	scope VARCHAR(30) NOT NULL, 
	scope_target_id VARCHAR(255), 
	definition_json JSON NOT NULL, 
	enabled BOOL NOT NULL, 
	priority INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name, version)
);

CREATE TABLE saved_reports (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	description TEXT, 
	query_type VARCHAR(20) NOT NULL, 
	query_def JSON NOT NULL, 
	chart_type VARCHAR(30) NOT NULL, 
	created_by VARCHAR(255), 
	tenant_id VARCHAR(255), 
	is_public BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_saved_reports_created ON saved_reports (created_at);

CREATE INDEX ix_saved_reports_public ON saved_reports (is_public);

CREATE INDEX ix_saved_reports_tenant ON saved_reports (tenant_id);

CREATE TABLE scheduled_releases (
	id CHAR(36) NOT NULL, 
	feature_key VARCHAR(100) NOT NULL, 
	version VARCHAR(32), 
	title VARCHAR(255) NOT NULL, 
	description TEXT, 
	release_notes TEXT, 
	release_date DATE, 
	status VARCHAR(20) NOT NULL, 
	enabled VARCHAR(20), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	released_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (feature_key)
);

CREATE INDEX ix_scheduled_releases_date ON scheduled_releases (release_date);

CREATE INDEX ix_scheduled_releases_status ON scheduled_releases (status);

CREATE TABLE security_events (
	id CHAR(36) NOT NULL, 
	event_type VARCHAR(50) NOT NULL, 
	severity VARCHAR(20) NOT NULL, 
	user_id VARCHAR(255), 
	resource_type VARCHAR(100), 
	resource_id VARCHAR(255), 
	ip_address VARCHAR(45), 
	user_agent TEXT, 
	action VARCHAR(100), 
	outcome VARCHAR(20), 
	details JSON NOT NULL, 
	timestamp DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE security_rules (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	pattern_type VARCHAR(50) NOT NULL, 
	description TEXT, 
	threshold INTEGER NOT NULL, 
	window_seconds INTEGER NOT NULL, 
	action VARCHAR(20) NOT NULL, 
	severity VARCHAR(10) NOT NULL, 
	enabled BOOL NOT NULL, 
	tenant_id VARCHAR(255), 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_shield_rules_enabled ON security_rules (enabled);

CREATE INDEX ix_shield_rules_pattern ON security_rules (pattern_type);

CREATE TABLE shield_events (
	id CHAR(36) NOT NULL, 
	event_type VARCHAR(50) NOT NULL, 
	actor_id VARCHAR(255), 
	tenant_id VARCHAR(255), 
	case_type_id VARCHAR(255), 
	payload_hash VARCHAR(64), 
	score FLOAT NOT NULL, 
	patterns_matched JSON NOT NULL, 
	raw_context JSON NOT NULL, 
	recorded_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_shield_ev_actor ON shield_events (actor_id);

CREATE INDEX ix_shield_ev_recorded ON shield_events (recorded_at);

CREATE INDEX ix_shield_ev_type ON shield_events (event_type);

CREATE TABLE sso_providers (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	provider VARCHAR(30) NOT NULL, 
	client_id TEXT NOT NULL, 
	client_secret_enc JSON, 
	enabled BOOL NOT NULL, 
	config JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_sso_tenant ON sso_providers (tenant_id, provider);

CREATE TABLE storefront_stores (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	slug VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	currency VARCHAR(10) NOT NULL, 
	locale VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	settings JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_stores_slug UNIQUE (slug)
);

CREATE INDEX ix_storefront_stores_tenant ON storefront_stores (tenant_id);

CREATE TABLE sync_destinations (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	dest_type VARCHAR(30) NOT NULL, 
	connection_config JSON NOT NULL, 
	enabled BOOL NOT NULL, 
	tenant_id VARCHAR(255), 
	created_by VARCHAR(255), 
	last_synced_at DATETIME, 
	last_sync_status VARCHAR(20), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_sync_dest_enabled ON sync_destinations (enabled);

CREATE INDEX ix_sync_dest_tenant ON sync_destinations (tenant_id);

CREATE TABLE system_config (
	`key` VARCHAR(255) NOT NULL, 
	value JSON NOT NULL, 
	updated_at DATETIME NOT NULL, 
	updated_by VARCHAR(255), 
	PRIMARY KEY (`key`)
);

CREATE TABLE telemetry_events (
	id CHAR(36) NOT NULL, 
	event_type VARCHAR(128) NOT NULL, 
	severity VARCHAR(16) NOT NULL, 
	payload JSON NOT NULL, 
	request_id VARCHAR(64), 
	trace_id VARCHAR(64), 
	tenant_id VARCHAR(64), 
	user_id VARCHAR(128), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_telemetry_events_created_at ON telemetry_events (created_at);

CREATE INDEX ix_telemetry_events_event_type ON telemetry_events (event_type);

CREATE INDEX ix_telemetry_events_request_id ON telemetry_events (request_id);

CREATE INDEX ix_telemetry_events_severity ON telemetry_events (severity);

CREATE INDEX ix_telemetry_events_tenant_id ON telemetry_events (tenant_id);

CREATE INDEX ix_telemetry_events_trace_id ON telemetry_events (trace_id);

CREATE INDEX ix_telemetry_events_user_id ON telemetry_events (user_id);

CREATE TABLE tenant_deks (
	id CHAR(36) NOT NULL, 
	tenant_id CHAR(36), 
	key_version INTEGER NOT NULL, 
	wrapped_dek TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (tenant_id)
);

CREATE TABLE tenants (
	id CHAR(36) NOT NULL, 
	slug VARCHAR(100) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	description TEXT NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	settings JSON NOT NULL, 
	max_cases INTEGER, 
	max_users INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (slug)
);

CREATE TABLE user_directory (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	email VARCHAR(255), 
	display_name VARCHAR(255), 
	manager_user_id VARCHAR(255), 
	access_group_ids JSON NOT NULL, 
	roles JSON NOT NULL, 
	timezone VARCHAR(64) NOT NULL, 
	tenant_id VARCHAR(64), 
	is_active BOOL NOT NULL, 
	metadata_json JSON NOT NULL, 
	current_access_group_id CHAR(36), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (user_id)
);

CREATE INDEX idx_user_directory_manager ON user_directory (manager_user_id);

CREATE INDEX idx_user_directory_tenant ON user_directory (tenant_id);

CREATE TABLE variable_namespaces (
	id CHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	owner_type VARCHAR(20) NOT NULL, 
	owner_ref CHAR(36), 
	sensitivity VARCHAR(10) NOT NULL, 
	status VARCHAR(10) NOT NULL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);

CREATE TABLE velaris_sequences (
	name VARCHAR(64) NOT NULL, 
	value BIGINT NOT NULL, 
	PRIMARY KEY (name)
);

CREATE TABLE webauthn_challenges (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255), 
	challenge BLOB NOT NULL, 
	purpose VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_webauthn_chal_expires ON webauthn_challenges (expires_at);

CREATE TABLE webauthn_credentials (
	id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	credential_id VARBINARY(1023) NOT NULL, 
	public_key BLOB NOT NULL, 
	sign_count BIGINT NOT NULL, 
	transports JSON NOT NULL, 
	aaguid VARCHAR(64), 
	device_name VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_used_at DATETIME, 
	revoked_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (credential_id)
);

CREATE INDEX ix_webauthn_creds_user ON webauthn_credentials (user_id);

CREATE TABLE access_groups (
	id CHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	description TEXT NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	portal_id CHAR(36) NOT NULL, 
	role_ids JSON NOT NULL, 
	allowed_case_type_ids JSON NOT NULL, 
	allowed_queue_ids JSON NOT NULL, 
	is_default BOOL NOT NULL, 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT access_groups_name_tenant_uq UNIQUE (name, tenant_id), 
	FOREIGN KEY(portal_id) REFERENCES portals (id) ON DELETE RESTRICT
);

CREATE INDEX idx_access_groups_portal ON access_groups (portal_id);

CREATE INDEX idx_access_groups_tenant ON access_groups (tenant_id);

CREATE TABLE app_deployments (
	id CHAR(36) NOT NULL, 
	package_id CHAR(36) NOT NULL, 
	environment VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	deployed_by VARCHAR(255), 
	deployed_at DATETIME NOT NULL, 
	notes TEXT, 
	config_overrides JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(package_id) REFERENCES app_packages (id) ON DELETE CASCADE
);

CREATE INDEX ix_app_deployments_deployed ON app_deployments (deployed_at);

CREATE INDEX ix_app_deployments_environment ON app_deployments (environment);

CREATE INDEX ix_app_deployments_package ON app_deployments (package_id);

CREATE TABLE artifact_analyses (
	id CHAR(36) NOT NULL, 
	scan_id CHAR(36), 
	artifact_identifier VARCHAR(500) NOT NULL, 
	artifact_type VARCHAR(100), 
	source_code TEXT, 
	summary TEXT, 
	business_logic TEXT, 
	complexity VARCHAR(20), 
	external_calls JSON NOT NULL, 
	data_reads JSON NOT NULL, 
	data_writes JSON NOT NULL, 
	side_effects JSON NOT NULL, 
	helix_mapping JSON NOT NULL, 
	generated_code TEXT, 
	confidence FLOAT, 
	ai_model VARCHAR(100), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(scan_id) REFERENCES migration_scans (id) ON DELETE CASCADE
);

CREATE TABLE artifact_branches (
	id CHAR(36) NOT NULL, 
	name TEXT NOT NULL, 
	description TEXT, 
	branch_type VARCHAR(20) NOT NULL, 
	artifact_type VARCHAR(50), 
	artifact_id TEXT, 
	app_package_id CHAR(36), 
	source_env_id CHAR(36), 
	source_env_name TEXT NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	content_snapshot JSON NOT NULL, 
	base_snapshot JSON NOT NULL, 
	conflict_detected BOOL NOT NULL, 
	merge_diff JSON, 
	owner_id TEXT, 
	assigned_reviewer_id TEXT, 
	access_group_id CHAR(36), 
	created_by TEXT NOT NULL, 
	reviewed_by TEXT, 
	merged_by TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	merged_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(app_package_id) REFERENCES app_packages (id) ON DELETE SET NULL, 
	FOREIGN KEY(source_env_id) REFERENCES environment_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_artifact_branches_created ON artifact_branches (created_at);

CREATE INDEX ix_artifact_branches_source_env ON artifact_branches (source_env_id);

CREATE INDEX ix_artifact_branches_status ON artifact_branches (status);

CREATE INDEX ix_artifact_branches_type ON artifact_branches (branch_type, artifact_type);

CREATE TABLE auth_otp (
	id CHAR(36) NOT NULL, 
	user_id CHAR(36) NOT NULL, 
	otp_hash TEXT NOT NULL, 
	purpose VARCHAR(30) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	used_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES helix_users (id) ON DELETE CASCADE
);

CREATE INDEX ix_auth_otp_user ON auth_otp (user_id);

CREATE TABLE case_types (
	id CHAR(36) NOT NULL, 
	tenant_id CHAR(36), 
	name VARCHAR(255) NOT NULL, 
	version VARCHAR(50) NOT NULL, 
	lifecycle_process_id VARCHAR(255), 
	data_model_id CHAR(36), 
	security_profile_id CHAR(36), 
	default_priority VARCHAR(20) NOT NULL, 
	definition_json JSON NOT NULL, 
	icon VARCHAR(100), 
	color VARCHAR(7), 
	description TEXT NOT NULL, 
	tags JSON NOT NULL, 
	portal_enabled BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	is_deleted BOOL NOT NULL, 
	deleted_at DATETIME, 
	deleted_by VARCHAR(255), 
	intake_trigger VARCHAR(20) NOT NULL, 
	trigger_connector_id CHAR(36), 
	filter_conditions JSON NOT NULL, 
	field_mapping JSON NOT NULL, 
	process_definition_id CHAR(36), 
	PRIMARY KEY (id), 
	UNIQUE (name, version), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_case_types_tenant_id ON case_types (tenant_id);

CREATE TABLE checkout_webhook_events (
	id CHAR(36) NOT NULL, 
	integration_id CHAR(36), 
	raw JSON NOT NULL, 
	mapped JSON NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	order_id CHAR(36), 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(integration_id) REFERENCES checkout_webhook_integrations (id) ON DELETE CASCADE
);

CREATE INDEX ix_checkout_wh_events_created ON checkout_webhook_events (created_at);

CREATE INDEX ix_checkout_wh_events_integration ON checkout_webhook_events (integration_id);

CREATE TABLE dead_letter_queue (
	id CHAR(36) NOT NULL, 
	connector_id CHAR(36), 
	case_id CHAR(36), 
	step_id VARCHAR(255), 
	payload JSON NOT NULL, 
	error TEXT, 
	retry_count INTEGER NOT NULL, 
	max_retries INTEGER NOT NULL, 
	next_retry_at DATETIME, 
	resolution VARCHAR(20), 
	created_at DATETIME NOT NULL, 
	resolved_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_dlq_connector ON dead_letter_queue (connector_id);

CREATE INDEX ix_dlq_resolution ON dead_letter_queue (resolution);

CREATE INDEX ix_dlq_retry ON dead_letter_queue (next_retry_at);

CREATE TABLE deployment_runs (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	package_id CHAR(36), 
	from_env_id CHAR(36), 
	to_env_id CHAR(36), 
	risk_level VARCHAR(20) NOT NULL, 
	risk_summary JSON NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	approval_case_id CHAR(36), 
	approved_by TEXT, 
	rejected_by TEXT, 
	rejection_reason TEXT, 
	initiated_by TEXT NOT NULL, 
	deploy_notes TEXT, 
	deployed_at DATETIME, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(from_env_id) REFERENCES environment_registry (id) ON DELETE SET NULL, 
	FOREIGN KEY(to_env_id) REFERENCES environment_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_dr_status ON deployment_runs (status);

CREATE INDEX ix_dr_tenant ON deployment_runs (tenant_id);

CREATE INDEX ix_dr_to_env ON deployment_runs (to_env_id);

CREATE TABLE deployment_windows (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	env_id CHAR(36), 
	name TEXT NOT NULL, 
	days_of_week JSON NOT NULL, 
	start_hour_utc INTEGER NOT NULL, 
	end_hour_utc INTEGER NOT NULL, 
	enabled BOOL NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(env_id) REFERENCES environment_registry (id) ON DELETE CASCADE
);

CREATE INDEX ix_dw_env ON deployment_windows (env_id);

CREATE TABLE field_population_audit (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36), 
	form_id VARCHAR(255), 
	field_key VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	user_id VARCHAR(255), 
	response_hash VARCHAR(64), 
	populated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_fpa_case ON field_population_audit (case_id);

CREATE INDEX ix_fpa_connector ON field_population_audit (connector_id);

CREATE INDEX ix_fpa_user ON field_population_audit (user_id);

CREATE TABLE form_definitions (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	version VARCHAR(50) NOT NULL, 
	data_model_id CHAR(36), 
	definition_json JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name, version), 
	FOREIGN KEY(data_model_id) REFERENCES data_models (id)
);

CREATE TABLE graph_edges (
	id CHAR(36) NOT NULL, 
	from_node_id CHAR(36) NOT NULL, 
	to_node_id CHAR(36) NOT NULL, 
	edge_type VARCHAR(50) NOT NULL, 
	weight FLOAT NOT NULL, 
	properties JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_graph_edges UNIQUE (from_node_id, to_node_id, edge_type), 
	FOREIGN KEY(from_node_id) REFERENCES graph_nodes (id) ON DELETE CASCADE, 
	FOREIGN KEY(to_node_id) REFERENCES graph_nodes (id) ON DELETE CASCADE
);

CREATE INDEX ix_graph_edges_from ON graph_edges (from_node_id);

CREATE INDEX ix_graph_edges_to ON graph_edges (to_node_id);

CREATE INDEX ix_graph_edges_type ON graph_edges (edge_type);

CREATE TABLE hxdocs_articles (
	id CHAR(36) NOT NULL, 
	space_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	title TEXT NOT NULL, 
	slug VARCHAR(200) NOT NULL, 
	content JSON NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	is_public BOOL NOT NULL, 
	auto_generated BOOL NOT NULL, 
	source_concept TEXT, 
	word_count INTEGER NOT NULL, 
	version INTEGER NOT NULL, 
	package_version VARCHAR(50), 
	tags JSON NOT NULL, 
	created_by TEXT, 
	updated_by TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(space_id) REFERENCES hxdocs_spaces (id) ON DELETE CASCADE
);

CREATE INDEX ix_hxda_public ON hxdocs_articles (is_public);

CREATE INDEX ix_hxda_space ON hxdocs_articles (space_id);

CREATE INDEX ix_hxda_status ON hxdocs_articles (status);

CREATE INDEX ix_hxda_tenant ON hxdocs_articles (tenant_id);

CREATE TABLE hxnexus_messages (
	id CHAR(36) NOT NULL, 
	conversation_id CHAR(36) NOT NULL, 
	`role` VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES hxnexus_conversations (id)
);

CREATE INDEX idx_hxnexus_msg_conv ON hxnexus_messages (conversation_id);

CREATE TABLE integration_calls (
	id CHAR(36) NOT NULL, 
	connector_id CHAR(36), 
	case_id CHAR(36), 
	step_id VARCHAR(255), 
	status VARCHAR(20) NOT NULL, 
	request JSON NOT NULL, 
	response JSON, 
	error TEXT, 
	latency_ms INTEGER, 
	retry_count INTEGER NOT NULL, 
	next_retry_at DATETIME, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_int_calls_case ON integration_calls (case_id);

CREATE INDEX ix_int_calls_connector ON integration_calls (connector_id);

CREATE INDEX ix_int_calls_created ON integration_calls (created_at);

CREATE INDEX ix_int_calls_status ON integration_calls (status);

CREATE TABLE marketplace_network_log (
	id CHAR(36) NOT NULL, 
	workspace_id CHAR(36) NOT NULL, 
	package_id VARCHAR(255) NOT NULL, 
	destination_url VARCHAR(1024) NOT NULL, 
	destination_ip VARCHAR(64), 
	http_method VARCHAR(16), 
	bytes_sent INTEGER NOT NULL, 
	bytes_received INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	http_status_code INTEGER, 
	is_declared BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES marketplace_workspaces (id) ON DELETE CASCADE
);

CREATE INDEX ix_mnl_package ON marketplace_network_log (package_id);

CREATE INDEX ix_mnl_workspace ON marketplace_network_log (workspace_id);

CREATE TABLE marketplace_packages (
	id VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	description TEXT NOT NULL, 
	package_type VARCHAR(64) NOT NULL, 
	category VARCHAR(128) NOT NULL, 
	publisher VARCHAR(255) NOT NULL, 
	publisher_tier VARCHAR(32) NOT NULL, 
	version VARCHAR(64) NOT NULL, 
	price VARCHAR(32) NOT NULL, 
	price_label VARCHAR(64), 
	contact_url VARCHAR(512), 
	rating FLOAT NOT NULL, 
	installs INTEGER NOT NULL, 
	download_url VARCHAR(512) NOT NULL, 
	checksum_sha256 VARCHAR(64) NOT NULL, 
	outbound_domains TEXT NOT NULL, 
	tags TEXT NOT NULL, 
	icon_color VARCHAR(16), 
	icon_letter VARCHAR(8), 
	min_platform_version VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32), 
	release_notes TEXT, 
	all_versions TEXT NOT NULL, 
	source_id CHAR(36), 
	fetched_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(source_id) REFERENCES marketplace_sources (id) ON DELETE SET NULL
);

CREATE TABLE marketplace_whitelist (
	id CHAR(36) NOT NULL, 
	workspace_id CHAR(36) NOT NULL, 
	package_id VARCHAR(255) NOT NULL, 
	domain VARCHAR(255) NOT NULL, 
	justification TEXT, 
	status VARCHAR(32) NOT NULL, 
	requested_by VARCHAR(255) NOT NULL, 
	decided_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	decided_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES marketplace_workspaces (id) ON DELETE CASCADE
);

CREATE INDEX ix_mwl_workspace ON marketplace_whitelist (workspace_id);

CREATE TABLE marketplace_workspace_items (
	id CHAR(36) NOT NULL, 
	workspace_id CHAR(36) NOT NULL, 
	package_id VARCHAR(255) NOT NULL, 
	package_version VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	licence_key_enc TEXT, 
	installed_at DATETIME NOT NULL, 
	approved_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workspace_id) REFERENCES marketplace_workspaces (id) ON DELETE CASCADE
);

CREATE INDEX ix_mwi_workspace ON marketplace_workspace_items (workspace_id);

CREATE TABLE migration_projects (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	source_platform VARCHAR(50), 
	scan_id CHAR(36), 
	status VARCHAR(30) NOT NULL, 
	total_artifacts INTEGER NOT NULL, 
	analyzed_count INTEGER NOT NULL, 
	generated_count INTEGER NOT NULL, 
	ported_count INTEGER NOT NULL, 
	roadmap JSON NOT NULL, 
	dependencies JSON NOT NULL, 
	settings JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(scan_id) REFERENCES migration_scans (id) ON DELETE SET NULL
);

CREATE TABLE namespace_grants (
	id CHAR(36) NOT NULL, 
	namespace_id CHAR(36) NOT NULL, 
	grantee_type VARCHAR(20) NOT NULL, 
	grantee_ref VARCHAR(255) NOT NULL, 
	capability VARCHAR(10) NOT NULL, 
	granted_by VARCHAR(255), 
	granted_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (namespace_id, grantee_type, grantee_ref, capability), 
	FOREIGN KEY(namespace_id) REFERENCES variable_namespaces (id) ON DELETE CASCADE
);

CREATE TABLE outbound_connector_rules (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name TEXT NOT NULL, 
	trigger_event VARCHAR(50) NOT NULL, 
	case_type_id CHAR(36), 
	condition_expr JSON, 
	connector_id CHAR(36), 
	input_mapping JSON NOT NULL, 
	enabled BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_ocr_connector ON outbound_connector_rules (connector_id);

CREATE INDEX ix_ocr_enabled ON outbound_connector_rules (enabled);

CREATE INDEX ix_ocr_tenant ON outbound_connector_rules (tenant_id);

CREATE TABLE pipeline_stage_events (
	id CHAR(36) NOT NULL, 
	run_id CHAR(36) NOT NULL, 
	stage INTEGER NOT NULL, 
	stage_name VARCHAR(100) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	summary JSON NOT NULL, 
	error TEXT, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES migration_pipeline_runs (id) ON DELETE CASCADE
);

CREATE INDEX ix_pse_run ON pipeline_stage_events (run_id);

CREATE TABLE platform_update_runs (
	id CHAR(36) NOT NULL, 
	plan_id CHAR(36) NOT NULL, 
	environment_id CHAR(36) NOT NULL, 
	ring_order INTEGER NOT NULL, 
	is_final_ring BOOL NOT NULL, 
	state TEXT NOT NULL, 
	detail TEXT, 
	triggered_at DATETIME, 
	finished_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plan_id) REFERENCES platform_update_plans (id) ON DELETE CASCADE, 
	FOREIGN KEY(environment_id) REFERENCES environment_registry (id) ON DELETE CASCADE
);

CREATE INDEX ix_puo_runs_plan ON platform_update_runs (plan_id);

CREATE TABLE portal_customers (
	id CHAR(36) NOT NULL, 
	tenant_id CHAR(36) NOT NULL, 
	primary_email VARCHAR(255) NOT NULL, 
	alt_email VARCHAR(255), 
	preferred_email VARCHAR(10) NOT NULL, 
	display_name VARCHAR(255) NOT NULL, 
	phone VARCHAR(64), 
	verified BOOL NOT NULL, 
	otp_code VARCHAR(64), 
	otp_expires_at DATETIME, 
	notify_email BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_active_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_portal_customer_email_tenant UNIQUE (tenant_id, primary_email), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE INDEX ix_portal_customers_email ON portal_customers (primary_email);

CREATE INDEX ix_portal_customers_tenant ON portal_customers (tenant_id);

CREATE TABLE process_instances (
	id CHAR(36) NOT NULL, 
	definition_id CHAR(36) NOT NULL, 
	case_id CHAR(36), 
	status VARCHAR(20) NOT NULL, 
	current_node VARCHAR(255), 
	context JSON NOT NULL, 
	error_node VARCHAR(255), 
	error_message TEXT, 
	started_at DATETIME NOT NULL, 
	ended_at DATETIME, 
	tenant_id VARCHAR(255), 
	PRIMARY KEY (id), 
	FOREIGN KEY(definition_id) REFERENCES process_definitions (id) ON DELETE RESTRICT
);

CREATE INDEX ix_pi_case ON process_instances (case_id);

CREATE INDEX ix_pi_definition ON process_instances (definition_id);

CREATE INDEX ix_pi_status ON process_instances (status);

CREATE INDEX ix_pi_tenant ON process_instances (tenant_id);

CREATE TABLE region_access_log (
	id CHAR(36) NOT NULL, 
	region_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	actor_id VARCHAR(255), 
	action VARCHAR(50) NOT NULL, 
	resource VARCHAR(255), 
	legal_basis VARCHAR(100), 
	recorded_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(region_id) REFERENCES region_registry (id) ON DELETE CASCADE
);

CREATE INDEX ix_ral_recorded ON region_access_log (recorded_at);

CREATE INDEX ix_ral_region ON region_access_log (region_id);

CREATE INDEX ix_ral_tenant ON region_access_log (tenant_id);

CREATE TABLE region_health_log (
	id CHAR(36) NOT NULL, 
	region_id CHAR(36) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	latency_ms INTEGER, 
	active_cases INTEGER, 
	replication_lag_ms INTEGER, 
	error_msg TEXT, 
	recorded_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(region_id) REFERENCES region_registry (id) ON DELETE CASCADE
);

CREATE INDEX ix_rhl_recorded ON region_health_log (recorded_at);

CREATE INDEX ix_rhl_region ON region_health_log (region_id);

CREATE TABLE replay_results (
	id CHAR(36) NOT NULL, 
	run_id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	determinacy VARCHAR(16) NOT NULL, 
	exclusion_reason TEXT, 
	divergence_point VARCHAR(255), 
	baseline_metrics JSON NOT NULL, 
	counterfactual_metrics JSON, 
	trace JSON, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES replay_runs (id) ON DELETE CASCADE
);

CREATE INDEX ix_replay_results_case ON replay_results (case_id);

CREATE INDEX ix_replay_results_run ON replay_results (run_id);

CREATE TABLE report_subscriptions (
	id CHAR(36) NOT NULL, 
	report_id CHAR(36) NOT NULL, 
	delivery_type VARCHAR(20) NOT NULL, 
	destination VARCHAR(500) NOT NULL, 
	schedule VARCHAR(50) NOT NULL, 
	format VARCHAR(10) NOT NULL, 
	enabled BOOL NOT NULL, 
	last_sent_at DATETIME, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(report_id) REFERENCES saved_reports (id) ON DELETE CASCADE
);

CREATE TABLE security_incidents (
	id CHAR(36) NOT NULL, 
	rule_id CHAR(36), 
	pattern_type VARCHAR(50) NOT NULL, 
	severity VARCHAR(10) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	actor_id VARCHAR(255), 
	tenant_id VARCHAR(255), 
	case_type_id VARCHAR(255), 
	context JSON NOT NULL, 
	explanation TEXT, 
	detected_at DATETIME NOT NULL, 
	resolved_at DATETIME, 
	resolved_by VARCHAR(255), 
	PRIMARY KEY (id), 
	FOREIGN KEY(rule_id) REFERENCES security_rules (id) ON DELETE SET NULL
);

CREATE INDEX ix_shield_inc_actor ON security_incidents (actor_id);

CREATE INDEX ix_shield_inc_detected ON security_incidents (detected_at);

CREATE INDEX ix_shield_inc_severity ON security_incidents (severity);

CREATE INDEX ix_shield_inc_status ON security_incidents (status);

CREATE INDEX ix_shield_inc_tenant ON security_incidents (tenant_id);

CREATE TABLE sovereignty_rules (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	case_type_id VARCHAR(255), 
	region_id CHAR(36) NOT NULL, 
	regulation VARCHAR(50) NOT NULL, 
	description TEXT, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(region_id) REFERENCES region_registry (id) ON DELETE CASCADE
);

CREATE INDEX ix_sov_case_type ON sovereignty_rules (case_type_id);

CREATE INDEX ix_sov_region ON sovereignty_rules (region_id);

CREATE INDEX ix_sov_tenant ON sovereignty_rules (tenant_id);

CREATE TABLE storefront_analytics_events (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	event VARCHAR(50) NOT NULL, 
	data JSON NOT NULL, 
	session VARCHAR(128), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_events_created ON storefront_analytics_events (created_at);

CREATE INDEX ix_storefront_events_store ON storefront_analytics_events (store_id);

CREATE TABLE storefront_categories (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	parent_id CHAR(36), 
	name VARCHAR(255) NOT NULL, 
	slug VARCHAR(255) NOT NULL, 
	description TEXT NOT NULL, 
	banner_path VARCHAR(1024), 
	display_order INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_categories_slug UNIQUE (store_id, slug), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE, 
	FOREIGN KEY(parent_id) REFERENCES storefront_categories (id) ON DELETE SET NULL
);

CREATE INDEX ix_storefront_categories_parent ON storefront_categories (parent_id);

CREATE INDEX ix_storefront_categories_store ON storefront_categories (store_id);

CREATE TABLE storefront_domains (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	domain VARCHAR(255) NOT NULL, 
	domain_type VARCHAR(20) NOT NULL, 
	dns_verified BOOL NOT NULL, 
	ssl_status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_domains_domain UNIQUE (domain), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_domains_store ON storefront_domains (store_id);

CREATE TABLE storefront_media (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	media_path VARCHAR(1024) NOT NULL, 
	media_type VARCHAR(50) NOT NULL, 
	size_bytes BIGINT NOT NULL, 
	alt_text VARCHAR(512) NOT NULL, 
	folder VARCHAR(512) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_media_store ON storefront_media (store_id);

CREATE TABLE storefront_navigation (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	location VARCHAR(20) NOT NULL, 
	items JSON NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_nav_location UNIQUE (store_id, location), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE TABLE storefront_pages (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	page_slug VARCHAR(255) NOT NULL, 
	title VARCHAR(512) NOT NULL, 
	sections JSON NOT NULL, 
	is_published BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_pages_slug UNIQUE (store_id, page_slug), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_pages_store ON storefront_pages (store_id);

CREATE TABLE storefront_products (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	name VARCHAR(512) NOT NULL, 
	slug VARCHAR(255) NOT NULL, 
	sku VARCHAR(255), 
	description TEXT NOT NULL, 
	short_description VARCHAR(512) NOT NULL, 
	tags JSON NOT NULL, 
	price_cents BIGINT NOT NULL, 
	compare_at_cents BIGINT, 
	tax_class VARCHAR(20) NOT NULL, 
	weight_grams INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	stock_quantity INTEGER, 
	low_stock_threshold INTEGER, 
	is_featured BOOL NOT NULL, 
	is_digital BOOL NOT NULL, 
	metadata JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_products_slug UNIQUE (store_id, slug), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_products_status ON storefront_products (status);

CREATE INDEX ix_storefront_products_store ON storefront_products (store_id);

CREATE TABLE storefront_promotions (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	code VARCHAR(64), 
	discount_type VARCHAR(30) NOT NULL, 
	config JSON NOT NULL, 
	applies_to JSON NOT NULL, 
	min_order_cents BIGINT, 
	usage_limit INTEGER, 
	per_customer_limit INTEGER, 
	valid_from DATETIME, 
	valid_until DATETIME, 
	stackable BOOL NOT NULL, 
	active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_promotions_code ON storefront_promotions (code);

CREATE INDEX ix_storefront_promotions_store ON storefront_promotions (store_id);

CREATE TABLE storefront_seo_overrides (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	target_type VARCHAR(20) NOT NULL, 
	target_id VARCHAR(255) NOT NULL, 
	meta_title VARCHAR(255) NOT NULL, 
	meta_description VARCHAR(512) NOT NULL, 
	og_title VARCHAR(255) NOT NULL, 
	og_description VARCHAR(512) NOT NULL, 
	og_image VARCHAR(1024), 
	canonical_url VARCHAR(1024), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_seo_target UNIQUE (store_id, target_type, target_id), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE TABLE storefront_subscribers (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_subscribers_email UNIQUE (store_id, email), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE TABLE storefront_themes (
	id CHAR(36) NOT NULL, 
	store_id CHAR(36) NOT NULL, 
	config JSON NOT NULL, 
	version INTEGER NOT NULL, 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(store_id) REFERENCES storefront_stores (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_themes_store ON storefront_themes (store_id);

CREATE TABLE sync_field_mappings (
	id CHAR(36) NOT NULL, 
	destination_id CHAR(36) NOT NULL, 
	case_type_id VARCHAR(255), 
	source_field VARCHAR(255) NOT NULL, 
	dest_column VARCHAR(255) NOT NULL, 
	transform VARCHAR(30) NOT NULL, 
	pii BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(destination_id) REFERENCES sync_destinations (id) ON DELETE CASCADE
);

CREATE TABLE sync_redaction_rules (
	id CHAR(36) NOT NULL, 
	destination_id CHAR(36) NOT NULL, 
	case_type_id VARCHAR(255), 
	field_path VARCHAR(255) NOT NULL, 
	action VARCHAR(10) NOT NULL, 
	reason TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(destination_id) REFERENCES sync_destinations (id) ON DELETE CASCADE
);

CREATE TABLE sync_runs (
	id CHAR(36) NOT NULL, 
	destination_id CHAR(36) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	rows_synced INTEGER NOT NULL, 
	error_msg TEXT, 
	watermark_from DATETIME, 
	watermark_to DATETIME, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(destination_id) REFERENCES sync_destinations (id) ON DELETE CASCADE
);

CREATE INDEX ix_sync_runs_dest ON sync_runs (destination_id);

CREATE INDEX ix_sync_runs_started ON sync_runs (started_at);

CREATE INDEX ix_sync_runs_status ON sync_runs (status);

CREATE TABLE tenant_memberships (
	id CHAR(36) NOT NULL, 
	tenant_id CHAR(36) NOT NULL, 
	user_id VARCHAR(255) NOT NULL, 
	`role` VARCHAR(50) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (tenant_id, user_id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE
);

CREATE TABLE tenant_region_assignments (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	region_id CHAR(36) NOT NULL, 
	assignment_type VARCHAR(20) NOT NULL, 
	migrated_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_tra UNIQUE (tenant_id, region_id, assignment_type), 
	FOREIGN KEY(region_id) REFERENCES region_registry (id) ON DELETE CASCADE
);

CREATE INDEX ix_tra_tenant ON tenant_region_assignments (tenant_id);

CREATE TABLE webhook_receiver_rules (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	name TEXT NOT NULL, 
	case_id_field TEXT, 
	match_case_field TEXT, 
	match_payload_field TEXT, 
	field_updates JSON NOT NULL, 
	advance_stage BOOL NOT NULL, 
	enabled BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE CASCADE
);

CREATE INDEX ix_wrr_connector ON webhook_receiver_rules (connector_id);

CREATE INDEX ix_wrr_tenant ON webhook_receiver_rules (tenant_id);

CREATE TABLE work_queues (
	id CHAR(36) NOT NULL, 
	tenant_id CHAR(36), 
	name VARCHAR(255) NOT NULL, 
	description TEXT NOT NULL, 
	filter_criteria JSON NOT NULL, 
	sort_fields JSON NOT NULL, 
	sort_ascending BOOL NOT NULL, 
	visible_to_roles JSON NOT NULL, 
	auto_assignment BOOL NOT NULL, 
	urgency_formula TEXT, 
	max_items INTEGER, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);

CREATE INDEX ix_work_queues_tenant_id ON work_queues (tenant_id);

CREATE TABLE branch_reviews (
	id CHAR(36) NOT NULL, 
	branch_id CHAR(36) NOT NULL, 
	reviewer_id TEXT NOT NULL, 
	decision VARCHAR(30) NOT NULL, 
	comments TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(branch_id) REFERENCES artifact_branches (id) ON DELETE CASCADE
);

CREATE INDEX ix_branch_reviews_branch ON branch_reviews (branch_id);

CREATE INDEX ix_branch_reviews_created ON branch_reviews (created_at);

CREATE TABLE case_instances (
	id CHAR(36) NOT NULL, 
	case_type_id CHAR(36) NOT NULL, 
	case_type_version VARCHAR(50) NOT NULL, 
	process_instance_id VARCHAR(255), 
	status VARCHAR(30) NOT NULL, 
	priority VARCHAR(20) NOT NULL, 
	urgency_score FLOAT NOT NULL, 
	current_stage_id VARCHAR(255), 
	parent_case_id CHAR(36), 
	data JSON NOT NULL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	resolved_at DATETIME, 
	closed_at DATETIME, 
	metadata JSON NOT NULL, 
	tenant_id CHAR(36), 
	case_number VARCHAR(30), 
	portal_tracking_token CHAR(36), 
	portal_submitter_name VARCHAR(255), 
	portal_submitter_email VARCHAR(255), 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id), 
	FOREIGN KEY(parent_case_id) REFERENCES case_instances (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
	UNIQUE (case_number), 
	UNIQUE (portal_tracking_token)
);

CREATE INDEX idx_cases_created ON case_instances (created_at);

CREATE INDEX idx_cases_parent ON case_instances (parent_case_id);

CREATE INDEX idx_cases_priority ON case_instances (priority);

CREATE INDEX idx_cases_status ON case_instances (status);

CREATE INDEX idx_cases_type ON case_instances (case_type_id);

CREATE INDEX idx_cases_urgency ON case_instances (urgency_score);

CREATE INDEX ix_case_instances_tenant_id ON case_instances (tenant_id);

CREATE TABLE case_type_notification_overrides (
	id CHAR(36) NOT NULL, 
	case_type_id CHAR(36) NOT NULL, 
	event_type VARCHAR(128) NOT NULL, 
	channels JSON NOT NULL, 
	enabled BOOL NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id)
);

CREATE UNIQUE INDEX idx_ctno_case_type_event ON case_type_notification_overrides (case_type_id, event_type);

CREATE TABLE case_type_stages (
	id CHAR(36) NOT NULL, 
	case_type_id CHAR(36) NOT NULL, 
	stage_id VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	stage_type VARCHAR(50) NOT NULL, 
	`order` INTEGER NOT NULL, 
	sla_policy_id VARCHAR(255), 
	definition_json JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (case_type_id, stage_id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id) ON DELETE CASCADE
);

CREATE TABLE case_type_variables (
	id CHAR(36) NOT NULL, 
	case_type_id CHAR(36) NOT NULL, 
	namespace_id CHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	full_key VARCHAR(201) NOT NULL, 
	var_type VARCHAR(20) NOT NULL, 
	definition_status VARCHAR(12) NOT NULL, 
	sensitivity_override VARCHAR(10), 
	label VARCHAR(255), 
	description TEXT, 
	default_value TEXT, 
	required BOOL NOT NULL, 
	indexed BOOL NOT NULL, 
	promoted_source VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (case_type_id, full_key), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id) ON DELETE CASCADE, 
	FOREIGN KEY(namespace_id) REFERENCES variable_namespaces (id)
);

CREATE INDEX ix_ctv_case_type ON case_type_variables (case_type_id);

CREATE INDEX ix_ctv_status ON case_type_variables (case_type_id, definition_status);

CREATE TABLE deployment_health_checks (
	id CHAR(36) NOT NULL, 
	run_id CHAR(36) NOT NULL, 
	check_url TEXT, 
	status_code INTEGER, 
	response_ms INTEGER, 
	healthy BOOL, 
	error TEXT, 
	checked_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES deployment_runs (id) ON DELETE CASCADE
);

CREATE INDEX ix_dhc_run ON deployment_health_checks (run_id);

CREATE TABLE email_templates (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	description TEXT NOT NULL, 
	subject VARCHAR(998) NOT NULL, 
	body_text TEXT NOT NULL, 
	body_html TEXT, 
	engine VARCHAR(16) NOT NULL, 
	scope VARCHAR(32) NOT NULL, 
	case_type_id CHAR(36), 
	tenant_id VARCHAR(64), 
	is_active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id)
);

CREATE INDEX idx_email_templates_case_type ON email_templates (case_type_id);

CREATE INDEX idx_email_templates_scope ON email_templates (scope);

CREATE TABLE escalation_trees (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	description TEXT NOT NULL, 
	scope VARCHAR(32) NOT NULL, 
	case_type_id CHAR(36), 
	tenant_id VARCHAR(64), 
	tree_json JSON NOT NULL, 
	is_active BOOL NOT NULL, 
	created_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id)
);

CREATE INDEX idx_escalation_trees_case_type ON escalation_trees (case_type_id);

CREATE INDEX idx_escalation_trees_scope ON escalation_trees (scope);

CREATE INDEX idx_escalation_trees_tenant ON escalation_trees (tenant_id);

CREATE TABLE hxdocs_article_versions (
	id CHAR(36) NOT NULL, 
	article_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	version INTEGER NOT NULL, 
	title TEXT NOT NULL, 
	content JSON NOT NULL, 
	package_version VARCHAR(50), 
	saved_by TEXT, 
	saved_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(article_id) REFERENCES hxdocs_articles (id) ON DELETE CASCADE
);

CREATE INDEX ix_hxdav_article ON hxdocs_article_versions (article_id);

CREATE INDEX ix_hxdav_version ON hxdocs_article_versions (version);

CREATE TABLE hxwork_boards (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name TEXT NOT NULL, 
	description TEXT, 
	case_type_id CHAR(36), 
	artifact_type TEXT, 
	artifact_id TEXT, 
	column_config JSON NOT NULL, 
	created_by TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id) ON DELETE SET NULL
);

CREATE INDEX ix_board_case_type ON hxwork_boards (case_type_id);

CREATE INDEX ix_board_tenant ON hxwork_boards (tenant_id);

CREATE TABLE intake_events (
	id CHAR(36) NOT NULL, 
	case_type_id CHAR(36), 
	connector_id CHAR(36), 
	source_ip VARCHAR(50), 
	raw_payload JSON NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	filter_result JSON NOT NULL, 
	created_case_id CHAR(36), 
	process_instance_id CHAR(36), 
	error TEXT, 
	received_at DATETIME NOT NULL, 
	processed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id) ON DELETE SET NULL
);

CREATE INDEX ix_intake_case ON intake_events (created_case_id);

CREATE INDEX ix_intake_case_type ON intake_events (case_type_id);

CREATE INDEX ix_intake_received ON intake_events (received_at);

CREATE INDEX ix_intake_status ON intake_events (status);

CREATE TABLE migration_tasks (
	id CHAR(36) NOT NULL, 
	project_id CHAR(36) NOT NULL, 
	artifact_id VARCHAR(500) NOT NULL, 
	artifact_type VARCHAR(100), 
	artifact_name VARCHAR(255), 
	phase INTEGER NOT NULL, 
	sequence INTEGER NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	depends_on JSON NOT NULL, 
	analysis_id CHAR(36), 
	generated_code TEXT, 
	complexity VARCHAR(20), 
	estimated_hours FLOAT, 
	actual_hours FLOAT, 
	notes TEXT, 
	started_at DATETIME, 
	completed_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES migration_projects (id) ON DELETE CASCADE, 
	FOREIGN KEY(analysis_id) REFERENCES artifact_analyses (id) ON DELETE SET NULL
);

CREATE TABLE operator_access_groups (
	id CHAR(36) NOT NULL, 
	operator_id VARCHAR(255) NOT NULL, 
	access_group_id CHAR(36) NOT NULL, 
	is_primary BOOL NOT NULL, 
	assigned_by VARCHAR(255), 
	assigned_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT oag_operator_group_uq UNIQUE (operator_id, access_group_id), 
	FOREIGN KEY(access_group_id) REFERENCES access_groups (id) ON DELETE CASCADE
);

CREATE INDEX idx_oag_access_group ON operator_access_groups (access_group_id);

CREATE INDEX idx_oag_operator ON operator_access_groups (operator_id);

CREATE TABLE outbox (
	id CHAR(36) NOT NULL, 
	event_type TEXT NOT NULL, 
	payload JSON NOT NULL, 
	case_type_id CHAR(36), 
	created_at DATETIME NOT NULL, 
	claimed_at DATETIME, 
	delivered_at DATETIME, 
	attempts INTEGER NOT NULL, 
	next_attempt_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id) ON DELETE SET NULL
);

CREATE INDEX ix_outbox_pending ON outbox (created_at);

CREATE TABLE process_case_bindings (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	instance_id CHAR(36) NOT NULL, 
	binding_type VARCHAR(30) NOT NULL, 
	direction VARCHAR(30) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	stage_id VARCHAR(255), 
	step_id VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	resolved_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(instance_id) REFERENCES process_instances (id) ON DELETE CASCADE
);

CREATE INDEX ix_pcb_case ON process_case_bindings (case_id);

CREATE INDEX ix_pcb_instance ON process_case_bindings (instance_id);

CREATE INDEX ix_pcb_status ON process_case_bindings (status);

CREATE TABLE process_task_log (
	id CHAR(36) NOT NULL, 
	instance_id CHAR(36) NOT NULL, 
	node_id VARCHAR(255) NOT NULL, 
	node_name VARCHAR(255), 
	node_type VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	input_context JSON NOT NULL, 
	result JSON, 
	error TEXT, 
	started_at DATETIME NOT NULL, 
	ended_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(instance_id) REFERENCES process_instances (id) ON DELETE CASCADE
);

CREATE INDEX ix_ptl_instance ON process_task_log (instance_id);

CREATE INDEX ix_ptl_node_id ON process_task_log (node_id);

CREATE INDEX ix_ptl_status ON process_task_log (status);

CREATE TABLE storefront_product_categories (
	id CHAR(36) NOT NULL, 
	product_id CHAR(36) NOT NULL, 
	category_id CHAR(36) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_prodcat UNIQUE (product_id, category_id), 
	FOREIGN KEY(product_id) REFERENCES storefront_products (id) ON DELETE CASCADE, 
	FOREIGN KEY(category_id) REFERENCES storefront_categories (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_prodcat_category ON storefront_product_categories (category_id);

CREATE INDEX ix_storefront_prodcat_product ON storefront_product_categories (product_id);

CREATE TABLE storefront_product_images (
	id CHAR(36) NOT NULL, 
	product_id CHAR(36) NOT NULL, 
	media_path VARCHAR(1024) NOT NULL, 
	alt_text VARCHAR(512) NOT NULL, 
	display_order INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(product_id) REFERENCES storefront_products (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_images_product ON storefront_product_images (product_id);

CREATE TABLE storefront_product_variants (
	id CHAR(36) NOT NULL, 
	product_id CHAR(36) NOT NULL, 
	sku VARCHAR(255) NOT NULL, 
	option_values JSON NOT NULL, 
	price_cents BIGINT, 
	stock_quantity INTEGER, 
	media_path VARCHAR(1024), 
	display_order INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_storefront_variants_sku UNIQUE (product_id, sku), 
	FOREIGN KEY(product_id) REFERENCES storefront_products (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_variants_product ON storefront_product_variants (product_id);

CREATE TABLE storefront_promotion_uses (
	id CHAR(36) NOT NULL, 
	promotion_id CHAR(36) NOT NULL, 
	order_ref VARCHAR(255), 
	customer_email VARCHAR(255), 
	used_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(promotion_id) REFERENCES storefront_promotions (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_promouse_promo ON storefront_promotion_uses (promotion_id);

CREATE TABLE storefront_variant_options (
	id CHAR(36) NOT NULL, 
	product_id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	`values` JSON NOT NULL, 
	display_order INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(product_id) REFERENCES storefront_products (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_varopt_product ON storefront_variant_options (product_id);

CREATE TABLE webhook_receiver_events (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	rule_id CHAR(36), 
	payload JSON NOT NULL, 
	matched_case_id CHAR(36), 
	status VARCHAR(50) NOT NULL, 
	error TEXT, 
	received_at DATETIME NOT NULL, 
	processed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL, 
	FOREIGN KEY(rule_id) REFERENCES webhook_receiver_rules (id) ON DELETE SET NULL
);

CREATE INDEX ix_wre_case ON webhook_receiver_events (matched_case_id);

CREATE INDEX ix_wre_connector ON webhook_receiver_events (connector_id);

CREATE INDEX ix_wre_status ON webhook_receiver_events (status);

CREATE TABLE webhook_subscriptions (
	id CHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	url TEXT NOT NULL, 
	secret VARCHAR(255), 
	events JSON NOT NULL, 
	case_type_id CHAR(36), 
	is_active BOOL NOT NULL, 
	headers JSON NOT NULL, 
	retry_count INTEGER NOT NULL, 
	timeout_seconds INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id)
);

CREATE TABLE case_assignments (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	assignee_type VARCHAR(20) NOT NULL, 
	assignee_id VARCHAR(255) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	assigned_at DATETIME NOT NULL, 
	due_at DATETIME, 
	claimed_at DATETIME, 
	completed_at DATETIME, 
	assigned_by VARCHAR(255), 
	locked_by VARCHAR(255), 
	locked_at DATETIME, 
	lock_expires_at DATETIME, 
	metadata JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE INDEX idx_assignments_assignee ON case_assignments (assignee_type, assignee_id);

CREATE INDEX idx_assignments_case ON case_assignments (case_id);

CREATE INDEX idx_assignments_status ON case_assignments (status);

CREATE TABLE case_audit_log (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	action VARCHAR(100) NOT NULL, 
	actor_id VARCHAR(255), 
	actor_type VARCHAR(20) NOT NULL, 
	timestamp DATETIME NOT NULL, 
	details JSON NOT NULL, 
	previous_value JSON, 
	new_value JSON, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id)
);

CREATE INDEX idx_audit_case ON case_audit_log (case_id, timestamp);

CREATE TABLE case_event_log (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	case_type_id CHAR(36) NOT NULL, 
	activity VARCHAR(255) NOT NULL, 
	activity_type VARCHAR(50) NOT NULL, 
	stage_id VARCHAR(255), 
	step_id VARCHAR(255), 
	actor_id VARCHAR(255), 
	actor_type VARCHAR(20), 
	timestamp DATETIME NOT NULL, 
	duration_seconds INTEGER, 
	resource_id VARCHAR(255), 
	outcome VARCHAR(50), 
	metadata JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id)
);

CREATE TABLE case_instance_variables (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	full_key VARCHAR(201) NOT NULL, 
	value_text TEXT, 
	value_num FLOAT, 
	value_bool BOOL, 
	value_json JSON, 
	written_by VARCHAR(255) NOT NULL, 
	written_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (case_id, full_key), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE INDEX ix_civ_key_num ON case_instance_variables (full_key, value_num);

CREATE INDEX ix_civ_key_text ON case_instance_variables (full_key, value_text(255));

CREATE TABLE case_messages (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	author VARCHAR(255) NOT NULL, 
	author_name VARCHAR(255), 
	body TEXT NOT NULL, 
	portal_visible BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE INDEX idx_case_messages_case ON case_messages (case_id, created_at);

CREATE TABLE case_relationships (
	id CHAR(36) NOT NULL, 
	source_case_id CHAR(36) NOT NULL, 
	target_case_id CHAR(36) NOT NULL, 
	relationship_type VARCHAR(30) NOT NULL, 
	propagate_status BOOL NOT NULL, 
	propagate_priority BOOL NOT NULL, 
	required BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (source_case_id, target_case_id, relationship_type), 
	FOREIGN KEY(source_case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(target_case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE TABLE case_sla_instances (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	sla_policy_id VARCHAR(255) NOT NULL, 
	target_id VARCHAR(255) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	started_at DATETIME NOT NULL, 
	goal_at DATETIME NOT NULL, 
	deadline_at DATETIME NOT NULL, 
	paused_at DATETIME, 
	pause_reason VARCHAR(255), 
	pause_reasons_log JSON NOT NULL, 
	escalation_level INTEGER NOT NULL, 
	escalation_tree_snapshot JSON NOT NULL, 
	escalation_history JSON NOT NULL, 
	business_calendar_id CHAR(36), 
	paused_duration_seconds INTEGER NOT NULL, 
	breached_at DATETIME, 
	metadata JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE INDEX idx_sla_case ON case_sla_instances (case_id);

CREATE TABLE case_step_completions (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	stage_id VARCHAR(255) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	step_type VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	data JSON NOT NULL, 
	completed_by VARCHAR(255), 
	completed_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT csc_case_step_uq UNIQUE (case_id, step_id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE INDEX idx_csc_case_id ON case_step_completions (case_id);

CREATE INDEX idx_csc_stage_id ON case_step_completions (case_id, stage_id);

CREATE TABLE case_type_steps (
	id CHAR(36) NOT NULL, 
	case_type_id CHAR(36) NOT NULL, 
	stage_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	step_type VARCHAR(50) NOT NULL, 
	bpmn_element_id VARCHAR(255) NOT NULL, 
	definition_json JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (case_type_id, step_id), 
	FOREIGN KEY(case_type_id) REFERENCES case_types (id) ON DELETE CASCADE, 
	FOREIGN KEY(stage_id) REFERENCES case_type_stages (id) ON DELETE CASCADE
);

CREATE TABLE checkout_orders (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36), 
	tracking_token VARCHAR(64) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	currency VARCHAR(10) NOT NULL, 
	total_cents BIGINT NOT NULL, 
	customer JSON NOT NULL, 
	shipping JSON NOT NULL, 
	metadata JSON NOT NULL, 
	source VARCHAR(50) NOT NULL, 
	idempotency_key VARCHAR(255), 
	integration_id CHAR(36), 
	payment_request_id CHAR(36), 
	is_test BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_checkout_orders_tracking UNIQUE (tracking_token), 
	CONSTRAINT uq_checkout_orders_idem UNIQUE (tenant_id, idempotency_key), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE SET NULL, 
	FOREIGN KEY(integration_id) REFERENCES checkout_webhook_integrations (id) ON DELETE SET NULL
);

CREATE INDEX ix_checkout_orders_case ON checkout_orders (case_id);

CREATE INDEX ix_checkout_orders_status ON checkout_orders (status);

CREATE INDEX ix_checkout_orders_tenant ON checkout_orders (tenant_id);

CREATE TABLE crm_sync_records (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	provider VARCHAR(50) NOT NULL, 
	crm_object_type VARCHAR(100), 
	crm_record_id VARCHAR(255), 
	crm_record_url TEXT, 
	status VARCHAR(50) NOT NULL, 
	sync_data JSON NOT NULL, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	synced_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_crm_sync_case ON crm_sync_records (case_id);

CREATE INDEX ix_crm_sync_record ON crm_sync_records (crm_record_id);

CREATE INDEX ix_crm_sync_status ON crm_sync_records (status);

CREATE TABLE doc_extraction_jobs (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	provider VARCHAR(50) NOT NULL, 
	document_id CHAR(36), 
	document_name TEXT, 
	source_url TEXT, 
	extracted_fields JSON NOT NULL, 
	raw_text TEXT, 
	confidence FLOAT, 
	status VARCHAR(50) NOT NULL, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_docex_case ON doc_extraction_jobs (case_id);

CREATE INDEX ix_docex_status ON doc_extraction_jobs (status);

CREATE TABLE doc_storage_routes (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	provider VARCHAR(50) NOT NULL, 
	document_name TEXT NOT NULL, 
	bucket TEXT, 
	object_key TEXT, 
	storage_url TEXT, 
	presigned_url TEXT, 
	size_bytes BIGINT, 
	content_type VARCHAR(255), 
	status VARCHAR(50) NOT NULL, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	uploaded_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_docst_case ON doc_storage_routes (case_id);

CREATE INDEX ix_docst_status ON doc_storage_routes (status);

CREATE TABLE documents (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	filename VARCHAR(512) NOT NULL, 
	content_type VARCHAR(128) NOT NULL, 
	current_version INTEGER NOT NULL, 
	uploaded_by VARCHAR(255), 
	tenant_id VARCHAR(64), 
	tags JSON NOT NULL, 
	ocr_text TEXT, 
	is_deleted BOOL NOT NULL, 
	deleted_at DATETIME, 
	deleted_by VARCHAR(255), 
	portal_visible BOOL NOT NULL, 
	portal_source VARCHAR(20), 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id)
);

CREATE INDEX idx_documents_case ON documents (case_id);

CREATE INDEX idx_documents_deleted ON documents (is_deleted);

CREATE INDEX idx_documents_tenant ON documents (tenant_id);

CREATE TABLE esign_requests (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	provider VARCHAR(50) NOT NULL, 
	envelope_id VARCHAR(255), 
	signing_url TEXT, 
	document_name TEXT, 
	signer_email TEXT, 
	signer_name TEXT, 
	status VARCHAR(50) NOT NULL, 
	signed_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_esign_case ON esign_requests (case_id);

CREATE INDEX ix_esign_envelope ON esign_requests (envelope_id);

CREATE INDEX ix_esign_status ON esign_requests (status);

CREATE TABLE hxcanvas_boards (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name TEXT NOT NULL, 
	description TEXT, 
	case_id CHAR(36), 
	created_by TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE SET NULL
);

CREATE INDEX ix_hxcb_case ON hxcanvas_boards (case_id);

CREATE INDEX ix_hxcb_tenant ON hxcanvas_boards (tenant_id);

CREATE TABLE hxwork_card_relations (
	id CHAR(36) NOT NULL, 
	board_id CHAR(36) NOT NULL, 
	from_case_id CHAR(36) NOT NULL, 
	to_case_id CHAR(36) NOT NULL, 
	relation_type VARCHAR(30) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(board_id) REFERENCES hxwork_boards (id) ON DELETE CASCADE
);

CREATE INDEX ix_cr_board ON hxwork_card_relations (board_id);

CREATE INDEX ix_cr_from ON hxwork_card_relations (from_case_id);

CREATE INDEX ix_cr_to ON hxwork_card_relations (to_case_id);

CREATE TABLE hxwork_sprints (
	id CHAR(36) NOT NULL, 
	board_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	name TEXT NOT NULL, 
	goal TEXT, 
	status VARCHAR(20) NOT NULL, 
	start_date DATETIME, 
	end_date DATETIME, 
	velocity INTEGER, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(board_id) REFERENCES hxwork_boards (id) ON DELETE CASCADE
);

CREATE INDEX ix_sprint_board ON hxwork_sprints (board_id);

CREATE INDEX ix_sprint_status ON hxwork_sprints (status);

CREATE TABLE identity_verifications (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	provider VARCHAR(50) NOT NULL, 
	check_id VARCHAR(255), 
	applicant_id VARCHAR(255), 
	sdk_token TEXT, 
	verification_url TEXT, 
	status VARCHAR(50) NOT NULL, 
	result VARCHAR(50), 
	result_hash TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_iv_case ON identity_verifications (case_id);

CREATE INDEX ix_iv_check ON identity_verifications (check_id);

CREATE INDEX ix_iv_status ON identity_verifications (status);

CREATE TABLE invoice_records (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	provider VARCHAR(50) NOT NULL, 
	invoice_id VARCHAR(255), 
	invoice_number VARCHAR(100), 
	invoice_url TEXT, 
	amount_cents BIGINT, 
	currency VARCHAR(10) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	contact_name TEXT, 
	line_items JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	issued_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_invoice_rec_case ON invoice_records (case_id);

CREATE INDEX ix_invoice_rec_invoice ON invoice_records (invoice_id);

CREATE INDEX ix_invoice_rec_status ON invoice_records (status);

CREATE TABLE payment_disbursements (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	amount_cents BIGINT NOT NULL, 
	currency VARCHAR(10) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	description TEXT, 
	bank_reference TEXT, 
	notes TEXT, 
	confirmed_by VARCHAR(255), 
	confirmed_at DATETIME, 
	completed_at DATETIME, 
	disbursement_executed BOOL NOT NULL, 
	disbursement_executed_at DATETIME, 
	updated_at DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE INDEX ix_payment_disbursements_case ON payment_disbursements (case_id);

CREATE INDEX ix_payment_disbursements_status ON payment_disbursements (status);

CREATE INDEX ix_payment_disbursements_tenant ON payment_disbursements (tenant_id);

CREATE TABLE payment_requests (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	provider VARCHAR(50) NOT NULL, 
	provider_ref VARCHAR(255), 
	checkout_url TEXT, 
	amount_cents BIGINT NOT NULL, 
	currency VARCHAR(10) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	description TEXT, 
	metadata JSON NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_payment_requests_case ON payment_requests (case_id);

CREATE INDEX ix_payment_requests_ref ON payment_requests (provider_ref);

CREATE INDEX ix_payment_requests_status ON payment_requests (status);

CREATE INDEX ix_payment_requests_tenant ON payment_requests (tenant_id);

CREATE TABLE portal_csat (
	case_id CHAR(36) NOT NULL, 
	customer_id CHAR(36) NOT NULL, 
	rating INTEGER NOT NULL, 
	comment TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (case_id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(customer_id) REFERENCES portal_customers (id) ON DELETE CASCADE
);

CREATE TABLE portal_customer_cases (
	customer_id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	linked_at DATETIME NOT NULL, 
	PRIMARY KEY (customer_id, case_id), 
	FOREIGN KEY(customer_id) REFERENCES portal_customers (id) ON DELETE CASCADE, 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE INDEX ix_pcc_case ON portal_customer_cases (case_id);

CREATE INDEX ix_pcc_customer ON portal_customer_cases (customer_id);

CREATE TABLE portal_submission_refs (
	client_ref CHAR(36) NOT NULL, 
	tenant_slug VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (client_ref), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE
);

CREATE INDEX idx_portal_submission_refs_created ON portal_submission_refs (created_at);

CREATE TABLE slack_notifications (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	channel VARCHAR(255), 
	message TEXT NOT NULL, 
	blocks JSON NOT NULL, 
	slack_ts VARCHAR(100), 
	status VARCHAR(50) NOT NULL, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	sent_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_slack_case ON slack_notifications (case_id);

CREATE INDEX ix_slack_status ON slack_notifications (status);

CREATE TABLE sms_messages (
	id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	step_id VARCHAR(255) NOT NULL, 
	connector_id CHAR(36), 
	provider VARCHAR(50) NOT NULL, 
	to_number VARCHAR(50) NOT NULL, 
	from_number VARCHAR(50), 
	body TEXT NOT NULL, 
	message_sid VARCHAR(255), 
	status VARCHAR(50) NOT NULL, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	sent_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connector_registry (id) ON DELETE SET NULL
);

CREATE INDEX ix_sms_case ON sms_messages (case_id);

CREATE INDEX ix_sms_status ON sms_messages (status);

CREATE TABLE storefront_inventory_log (
	id CHAR(36) NOT NULL, 
	variant_id CHAR(36) NOT NULL, 
	`change` INTEGER NOT NULL, 
	new_quantity INTEGER, 
	reason VARCHAR(100) NOT NULL, 
	actor VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(variant_id) REFERENCES storefront_product_variants (id) ON DELETE CASCADE
);

CREATE INDEX ix_storefront_invlog_variant ON storefront_inventory_log (variant_id);

CREATE TABLE trace_events (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36), 
	tenant_id VARCHAR(255) NOT NULL, 
	event_type VARCHAR(50) NOT NULL, 
	actor_user_id VARCHAR(255), 
	actor_ip VARCHAR(45), 
	payload JSON NOT NULL, 
	occurred_at DATETIME NOT NULL, 
	session_id VARCHAR(255), 
	latency_ms INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE SET NULL
);

CREATE INDEX ix_trace_events_actor ON trace_events (actor_user_id, occurred_at);

CREATE INDEX ix_trace_events_case_id ON trace_events (case_id, occurred_at);

CREATE INDEX ix_trace_events_session ON trace_events (session_id, occurred_at);

CREATE INDEX ix_trace_events_tenant ON trace_events (tenant_id, occurred_at);

CREATE INDEX ix_trace_events_type ON trace_events (event_type, occurred_at);

CREATE TABLE webhook_deliveries (
	id CHAR(36) NOT NULL, 
	subscription_id CHAR(36) NOT NULL, 
	event_type VARCHAR(100) NOT NULL, 
	payload JSON NOT NULL, 
	response_status INTEGER, 
	response_body TEXT, 
	attempt INTEGER NOT NULL, 
	delivered_at DATETIME, 
	next_retry_at DATETIME, 
	status VARCHAR(20) NOT NULL, 
	error_message TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(subscription_id) REFERENCES webhook_subscriptions (id) ON DELETE CASCADE
);

CREATE TABLE case_sessions (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	driver VARCHAR(20) NOT NULL, 
	provider VARCHAR(50) NOT NULL, 
	connector_id CHAR(36), 
	status VARCHAR(20) NOT NULL, 
	title VARCHAR(255), 
	external_meeting_id TEXT, 
	join_url TEXT, 
	scheduled_at DATETIME, 
	started_by VARCHAR(255) NOT NULL, 
	started_at DATETIME, 
	ended_at DATETIME, 
	record_intent BOOL NOT NULL, 
	recording_status VARCHAR(20) NOT NULL, 
	recording_egress_id TEXT, 
	recording_document_id CHAR(36), 
	audit_anchor_ref TEXT, 
	transcript_status VARCHAR(20) NOT NULL, 
	transcript_document_id CHAR(36), 
	transcript_anchor_ref TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(recording_document_id) REFERENCES documents (id), 
	FOREIGN KEY(transcript_document_id) REFERENCES documents (id)
);

CREATE INDEX ix_case_sessions_case ON case_sessions (case_id);

CREATE INDEX ix_case_sessions_status ON case_sessions (status);

CREATE INDEX ix_case_sessions_tenant ON case_sessions (tenant_id);

CREATE TABLE checkout_notifications_log (
	id CHAR(36) NOT NULL, 
	order_id CHAR(36) NOT NULL, 
	event VARCHAR(100) NOT NULL, 
	channel VARCHAR(20) NOT NULL, 
	status VARCHAR(50) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(order_id) REFERENCES checkout_orders (id) ON DELETE CASCADE
);

CREATE INDEX ix_checkout_notif_order ON checkout_notifications_log (order_id);

CREATE TABLE checkout_order_items (
	id CHAR(36) NOT NULL, 
	order_id CHAR(36) NOT NULL, 
	sku VARCHAR(255) NOT NULL, 
	name VARCHAR(512) NOT NULL, 
	quantity INTEGER NOT NULL, 
	unit_price_cents BIGINT NOT NULL, 
	metadata JSON NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(order_id) REFERENCES checkout_orders (id) ON DELETE CASCADE
);

CREATE INDEX ix_checkout_order_items_order ON checkout_order_items (order_id);

CREATE TABLE document_verifications (
	id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	document_id CHAR(36) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	checks JSON NOT NULL, 
	verified_by VARCHAR(255) NOT NULL, 
	notes TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES case_instances (id) ON DELETE CASCADE, 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE INDEX idx_doc_verifications_case ON document_verifications (case_id, created_at);

CREATE INDEX idx_doc_verifications_doc ON document_verifications (document_id);

CREATE TABLE document_versions (
	id CHAR(36) NOT NULL, 
	document_id CHAR(36) NOT NULL, 
	version INTEGER NOT NULL, 
	storage_key VARCHAR(1024) NOT NULL, 
	size_bytes INTEGER NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	uploaded_by VARCHAR(255), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_document_version UNIQUE (document_id, version), 
	FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE
);

CREATE INDEX idx_document_versions_doc ON document_versions (document_id);

CREATE TABLE hxcanvas_items (
	id CHAR(36) NOT NULL, 
	board_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255) NOT NULL, 
	type VARCHAR(30) NOT NULL, 
	x FLOAT NOT NULL, 
	y FLOAT NOT NULL, 
	width FLOAT NOT NULL, 
	height FLOAT NOT NULL, 
	data JSON NOT NULL, 
	z_index INTEGER NOT NULL, 
	created_by TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(board_id) REFERENCES hxcanvas_boards (id) ON DELETE CASCADE
);

CREATE INDEX ix_hxci_board ON hxcanvas_items (board_id);

CREATE INDEX ix_hxci_tenant ON hxcanvas_items (tenant_id);

CREATE INDEX ix_hxci_type ON hxcanvas_items (type);

CREATE TABLE hxwork_sprint_cards (
	sprint_id CHAR(36) NOT NULL, 
	case_id CHAR(36) NOT NULL, 
	story_points INTEGER NOT NULL, 
	added_at DATETIME NOT NULL, 
	PRIMARY KEY (sprint_id, case_id), 
	FOREIGN KEY(sprint_id) REFERENCES hxwork_sprints (id) ON DELETE CASCADE
);

CREATE TABLE hxwork_stories (
	id CHAR(36) NOT NULL, 
	board_id CHAR(36) NOT NULL, 
	sprint_id CHAR(36), 
	branch_id CHAR(36), 
	branch_name TEXT, 
	title TEXT NOT NULL, 
	description TEXT, 
	acceptance_criteria TEXT, 
	status VARCHAR(30) NOT NULL, 
	story_points INTEGER, 
	assigned_to TEXT, 
	linked_commit_ids JSON NOT NULL, 
	created_by TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(board_id) REFERENCES hxwork_boards (id) ON DELETE CASCADE, 
	FOREIGN KEY(sprint_id) REFERENCES hxwork_sprints (id) ON DELETE SET NULL, 
	FOREIGN KEY(branch_id) REFERENCES artifact_branches (id) ON DELETE SET NULL
);

CREATE INDEX ix_hws_board ON hxwork_stories (board_id);

CREATE INDEX ix_hws_branch ON hxwork_stories (branch_id);

CREATE INDEX ix_hws_sprint ON hxwork_stories (sprint_id);

CREATE INDEX ix_hws_status ON hxwork_stories (status);

CREATE TABLE case_session_caption_segments (
	id CHAR(36) NOT NULL, 
	session_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	speaker VARCHAR(255) NOT NULL, 
	text TEXT NOT NULL, 
	spoken_at DATETIME NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES case_sessions (id) ON DELETE CASCADE
);

CREATE INDEX idx_caption_segments_session ON case_session_caption_segments (session_id, spoken_at);

CREATE TABLE case_session_intelligence (
	session_id CHAR(36) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	transcript_document_id CHAR(36), 
	summary TEXT, 
	action_items JSON NOT NULL, 
	language VARCHAR(16), 
	duration_seconds INTEGER, 
	model_versions JSON NOT NULL, 
	error TEXT, 
	requested_by VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (session_id), 
	FOREIGN KEY(session_id) REFERENCES case_sessions (id) ON DELETE CASCADE
);

CREATE TABLE case_session_participants (
	id CHAR(36) NOT NULL, 
	session_id CHAR(36) NOT NULL, 
	tenant_id VARCHAR(255), 
	identity VARCHAR(512) NOT NULL, 
	display_name VARCHAR(255), 
	`role` VARCHAR(20) NOT NULL, 
	invited_by VARCHAR(255), 
	invite_token_hash VARCHAR(64), 
	invite_expires_at DATETIME, 
	token_used_at DATETIME, 
	joined_at DATETIME, 
	left_at DATETIME, 
	consent_recorded_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES case_sessions (id) ON DELETE CASCADE, 
	UNIQUE (invite_token_hash)
);

CREATE INDEX ix_csp_session ON case_session_participants (session_id);

CREATE INDEX ix_csp_tenant ON case_session_participants (tenant_id);

CREATE TABLE hxwork_story_relations (
	id CHAR(36) NOT NULL, 
	board_id CHAR(36) NOT NULL, 
	from_story CHAR(36) NOT NULL, 
	to_story CHAR(36) NOT NULL, 
	relation VARCHAR(30) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(board_id) REFERENCES hxwork_boards (id) ON DELETE CASCADE, 
	FOREIGN KEY(from_story) REFERENCES hxwork_stories (id) ON DELETE CASCADE, 
	FOREIGN KEY(to_story) REFERENCES hxwork_stories (id) ON DELETE CASCADE
);

CREATE INDEX ix_hws_rel_board ON hxwork_story_relations (board_id);

SET FOREIGN_KEY_CHECKS = 1;
