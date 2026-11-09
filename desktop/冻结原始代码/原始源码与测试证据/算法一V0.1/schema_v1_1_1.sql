CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS chaintrace;
SET search_path TO chaintrace;

-- V1.1 keeps the V1 relational model and adds stronger namespace, provenance,
-- case-scope, sensitive-field and append-only audit controls.

CREATE TABLE system_user (
    user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_code varchar(64) NOT NULL UNIQUE,
    display_name varchar(128) NOT NULL,
    role_code varchar(32) NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE investigation_case (
    case_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_no varchar(64) NOT NULL UNIQUE,
    case_name varchar(200) NOT NULL,
    case_type varchar(32) NOT NULL,
    status varchar(24) NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT','ACTIVE','CLOSED','ARCHIVED')),
    sensitivity_level varchar(16) NOT NULL DEFAULT 'NORMAL'
        CHECK (sensitivity_level IN ('NORMAL','RESTRICTED','SECRET')),
    incident_start_at timestamptz,
    incident_end_at timestamptz,
    description text,
    created_by uuid NOT NULL REFERENCES system_user(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (incident_end_at IS NULL OR incident_start_at IS NULL OR incident_end_at >= incident_start_at)
);

CREATE TABLE person (
    person_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person_code varchar(64) NOT NULL UNIQUE,
    display_name varchar(128) NOT NULL,
    legal_name_ciphertext text,
    id_no_hmac char(64),
    id_no_key_version varchar(32),
    identity_status varchar(24) NOT NULL DEFAULT 'UNKNOWN'
        CHECK (identity_status IN ('UNKNOWN','UNVERIFIED','VERIFIED','EXCLUDED')),
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE case_person (
    case_person_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    person_id uuid NOT NULL REFERENCES person(person_id),
    role_code varchar(32) NOT NULL,
    case_status varchar(24) NOT NULL DEFAULT 'CANDIDATE'
        CHECK (case_status IN ('KNOWN','CANDIDATE','WANTED','CLEARED','DISPUTED')),
    fact_status varchar(16) NOT NULL DEFAULT 'FACT'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    valid_from timestamptz,
    valid_to timestamptz,
    source_note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, person_id, role_code),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE TABLE network (
    network_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    network_type varchar(24) NOT NULL
        CHECK (network_type IN ('BLOCKCHAIN','EXCHANGE','BANK','PAYMENT','OTHER')),
    network_code varchar(64) NOT NULL UNIQUE,
    network_name varchar(128) NOT NULL,
    chain_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE asset (
    asset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    network_id uuid REFERENCES network(network_id),
    symbol varchar(32) NOT NULL,
    asset_name varchar(128) NOT NULL,
    contract_address varchar(128),
    decimals smallint CHECK (decimals IS NULL OR (decimals >= 0 AND decimals <= 36)),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (network_id, symbol, contract_address)
);

CREATE TABLE financial_account (
    account_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_type varchar(32) NOT NULL
        CHECK (account_type IN ('ONCHAIN_WALLET','EXCHANGE_ACCOUNT','BANK_ACCOUNT','PAYMENT_ACCOUNT','UNKNOWN')),
    network_id uuid REFERENCES network(network_id),
    provider_name varchar(128),
    account_label varchar(128),
    account_status varchar(24) NOT NULL DEFAULT 'UNKNOWN'
        CHECK (account_status IN ('ACTIVE','INACTIVE','UNKNOWN','EXCLUDED')),
    fact_status varchar(16) NOT NULL DEFAULT 'UNKNOWN'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE account_identifier (
    identifier_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id uuid NOT NULL REFERENCES financial_account(account_id),
    namespace_network_id uuid REFERENCES network(network_id),
    identifier_type varchar(32) NOT NULL,
    value_norm varchar(256) NOT NULL,
    value_display varchar(256) NOT NULL,
    is_primary boolean NOT NULL DEFAULT false,
    observed_from timestamptz,
    observed_to timestamptz,
    source_record_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (namespace_network_id, identifier_type, value_norm),
    CHECK (observed_to IS NULL OR observed_from IS NULL OR observed_to >= observed_from)
);

CREATE TABLE case_account (
    case_account_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    account_id uuid NOT NULL REFERENCES financial_account(account_id),
    case_role varchar(32) NOT NULL,
    fact_status varchar(16) NOT NULL DEFAULT 'CANDIDATE'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    label varchar(128),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, account_id, case_role)
);

CREATE TABLE data_source (
    data_source_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    source_type varchar(32) NOT NULL,
    provider varchar(128),
    locator text,
    is_simulated boolean NOT NULL DEFAULT false,
    reliability_level varchar(16) NOT NULL DEFAULT 'MEDIUM'
        CHECK (reliability_level IN ('LOW','MEDIUM','HIGH')),
    acquired_at timestamptz,
    acquired_by uuid REFERENCES system_user(user_id),
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, data_source_id)
);

CREATE TABLE source_record (
    source_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    data_source_id uuid NOT NULL REFERENCES data_source(data_source_id),
    external_record_id varchar(256),
    record_type varchar(64) NOT NULL,
    observed_at timestamptz,
    raw_payload jsonb NOT NULL,
    raw_sha256 char(64) NOT NULL,
    normalization_version varchar(32),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, source_record_id),
    FOREIGN KEY (case_id, data_source_id)
        REFERENCES data_source(case_id, data_source_id)
);

ALTER TABLE account_identifier
    ADD CONSTRAINT fk_account_identifier_source_record
    FOREIGN KEY (source_record_id) REFERENCES source_record(source_record_id);

CREATE TABLE evidence_item (
    evidence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    evidence_type varchar(32) NOT NULL,
    title varchar(200) NOT NULL,
    original_locator text,
    file_sha256 char(64),
    hash_algorithm varchar(16) DEFAULT 'SHA256',
    page_or_location varchar(128),
    collected_at timestamptz,
    collected_by uuid REFERENCES system_user(user_id),
    integrity_status varchar(24) NOT NULL DEFAULT 'PENDING'
        CHECK (integrity_status IN ('INTACT','PENDING','ISSUE')),
    fact_status varchar(16) NOT NULL DEFAULT 'FACT'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    confidentiality_level varchar(16) NOT NULL DEFAULT 'NORMAL'
        CHECK (confidentiality_level IN ('NORMAL','RESTRICTED','SECRET')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, evidence_id)
);

CREATE TABLE device (
    device_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    device_type varchar(32) NOT NULL,
    device_code varchar(128) NOT NULL,
    serial_hmac char(64),
    serial_key_version varchar(32),
    model varchar(128),
    fact_status varchar(16) NOT NULL DEFAULT 'UNKNOWN'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, device_code),
    UNIQUE (case_id, device_id)
);

CREATE TABLE network_observation (
    observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    device_id uuid,
    ip_hmac char(64),
    mac_hmac char(64),
    hmac_key_version varchar(32),
    observed_at timestamptz NOT NULL,
    source_record_id uuid REFERENCES source_record(source_record_id),
    observation_type varchar(32) NOT NULL,
    fact_status varchar(16) NOT NULL DEFAULT 'FACT'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ip_hmac IS NOT NULL OR mac_hmac IS NOT NULL),
    FOREIGN KEY (case_id, device_id)
        REFERENCES device(case_id, device_id),
    FOREIGN KEY (case_id, source_record_id)
        REFERENCES source_record(case_id, source_record_id)
);

CREATE TABLE money_event (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    event_type varchar(32) NOT NULL
        CHECK (event_type IN ('ONCHAIN_TRANSFER','EXCHANGE_DEPOSIT','EXCHANGE_WITHDRAWAL','INTERNAL_TRANSFER','BANK_TRANSFER','PAYMENT_ORDER','CONVERSION','FEE','UNKNOWN')),
    network_id uuid REFERENCES network(network_id),
    asset_id uuid REFERENCES asset(asset_id),
    from_account_id uuid REFERENCES financial_account(account_id),
    to_account_id uuid REFERENCES financial_account(account_id),
    amount numeric(38,18) NOT NULL CHECK (amount > 0),
    fee_amount numeric(38,18) CHECK (fee_amount IS NULL OR fee_amount >= 0),
    fiat_amount numeric(20,8),
    fiat_currency char(3),
    event_time timestamptz NOT NULL,
    tx_hash varchar(256),
    tx_index integer CHECK (tx_index IS NULL OR tx_index >= 0),
    tx_index_kind varchar(24)
        CHECK (tx_index_kind IN ('TRANSFER_LOG','TX_OUTPUT','INTERNAL_LINE','UNKNOWN')),
    reference_no varchar(256),
    primary_source_record_id uuid,
    parent_event_id uuid,
    fact_status varchar(16) NOT NULL DEFAULT 'FACT'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    confidence_level varchar(16) NOT NULL DEFAULT 'HIGH'
        CHECK (confidence_level IN ('LOW','MEDIUM','HIGH')),
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (from_account_id IS NULL OR to_account_id IS NULL OR from_account_id <> to_account_id),
    UNIQUE (case_id, event_id),
    FOREIGN KEY (case_id, parent_event_id)
        REFERENCES money_event(case_id, event_id),
    FOREIGN KEY (case_id, primary_source_record_id)
        REFERENCES source_record(case_id, source_record_id)
);

CREATE TABLE money_event_source_record (
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    event_id uuid NOT NULL,
    source_record_id uuid NOT NULL,
    source_role varchar(24) NOT NULL
        CHECK (source_role IN ('PRIMARY','SUPPORTING','CONTRADICTING','DERIVED_FROM')),
    link_status varchar(24) NOT NULL DEFAULT 'PENDING'
        CHECK (link_status IN ('PENDING','ACCEPTED','REJECTED','DISPUTED')),
    matching_basis jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, source_record_id, source_role),
    FOREIGN KEY (case_id, event_id)
        REFERENCES money_event(case_id, event_id) ON DELETE CASCADE,
    FOREIGN KEY (case_id, source_record_id)
        REFERENCES source_record(case_id, source_record_id)
);

CREATE TABLE entity_node (
    node_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    node_type varchar(32) NOT NULL,
    ref_id uuid NOT NULL,
    display_label varchar(256),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, node_type, ref_id),
    UNIQUE (case_id, node_id),
    CHECK (node_type IN ('CASE','PERSON','ACCOUNT','EVENT','DEVICE','EVIDENCE','CLAIM','CLUE'))
);

CREATE TABLE claim (
    claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    evidence_id uuid NOT NULL,
    subject_node_id uuid,
    predicate varchar(64) NOT NULL,
    object_node_id uuid,
    asset_id uuid REFERENCES asset(asset_id),
    amount numeric(38,18) CHECK (amount IS NULL OR amount > 0),
    event_time_from timestamptz,
    event_time_to timestamptz,
    text_span text,
    fact_status varchar(16) NOT NULL DEFAULT 'CANDIDATE'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (event_time_to IS NULL OR event_time_from IS NULL OR event_time_to >= event_time_from),
    FOREIGN KEY (case_id, evidence_id)
        REFERENCES evidence_item(case_id, evidence_id),
    FOREIGN KEY (case_id, subject_node_id)
        REFERENCES entity_node(case_id, node_id),
    FOREIGN KEY (case_id, object_node_id)
        REFERENCES entity_node(case_id, node_id)
);

CREATE TABLE investigative_relation (
    relation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    left_node_id uuid NOT NULL,
    right_node_id uuid NOT NULL,
    relation_type varchar(64) NOT NULL,
    valid_from timestamptz,
    valid_to timestamptz,
    fact_status varchar(16) NOT NULL DEFAULT 'CANDIDATE'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    confidence_level varchar(16) NOT NULL DEFAULT 'LOW'
        CHECK (confidence_level IN ('LOW','MEDIUM','HIGH')),
    conclusion text,
    reviewer_id uuid REFERENCES system_user(user_id),
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, relation_id),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from),
    FOREIGN KEY (case_id, left_node_id)
        REFERENCES entity_node(case_id, node_id),
    FOREIGN KEY (case_id, right_node_id)
        REFERENCES entity_node(case_id, node_id)
);

CREATE TABLE relation_evidence (
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    relation_id uuid NOT NULL,
    evidence_id uuid NOT NULL,
    support_type varchar(24) NOT NULL
        CHECK (support_type IN ('SUPPORTS','CONTRADICTS','CONTEXT')),
    description text,
    weight numeric(8,4),
    PRIMARY KEY (relation_id, evidence_id, support_type),
    FOREIGN KEY (case_id, relation_id)
        REFERENCES investigative_relation(case_id, relation_id) ON DELETE CASCADE,
    FOREIGN KEY (case_id, evidence_id)
        REFERENCES evidence_item(case_id, evidence_id)
);

CREATE TABLE analysis_run (
    run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    algorithm_code varchar(64) NOT NULL,
    algorithm_version varchar(32) NOT NULL,
    input_snapshot_hash char(64) NOT NULL,
    parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(24) NOT NULL DEFAULT 'RUNNING'
        CHECK (status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    operator_id uuid REFERENCES system_user(user_id),
    UNIQUE (case_id, run_id),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE candidate_match (
    match_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    run_id uuid NOT NULL,
    left_node_id uuid NOT NULL,
    right_node_id uuid NOT NULL,
    match_type varchar(32) NOT NULL,
    score numeric(8,4) CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
    explanation jsonb NOT NULL DEFAULT '{}'::jsonb,
    missing_items jsonb NOT NULL DEFAULT '[]'::jsonb,
    conflict_items jsonb NOT NULL DEFAULT '[]'::jsonb,
    review_status varchar(24) NOT NULL DEFAULT 'PENDING'
        CHECK (review_status IN ('PENDING','ACCEPTED','REJECTED','DISPUTED')),
    reviewer_id uuid REFERENCES system_user(user_id),
    reviewed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (case_id, left_node_id)
        REFERENCES entity_node(case_id, node_id),
    FOREIGN KEY (case_id, right_node_id)
        REFERENCES entity_node(case_id, node_id),
    FOREIGN KEY (case_id, run_id)
        REFERENCES analysis_run(case_id, run_id)
);

CREATE OR REPLACE FUNCTION validate_candidate_match_node_types()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_left_type varchar(32);
    v_right_type varchar(32);
BEGIN
    SELECT node_type INTO v_left_type
    FROM entity_node
    WHERE case_id = NEW.case_id AND node_id = NEW.left_node_id;

    SELECT node_type INTO v_right_type
    FROM entity_node
    WHERE case_id = NEW.case_id AND node_id = NEW.right_node_id;

    IF NEW.match_type = 'EVENT_TO_CLAIM'
       AND (v_left_type <> 'EVENT' OR v_right_type <> 'CLAIM') THEN
        RAISE EXCEPTION 'EVENT_TO_CLAIM requires EVENT -> CLAIM nodes, got % -> %', v_left_type, v_right_type;
    ELSIF NEW.match_type = 'EVENT_TO_EVENT'
       AND (v_left_type <> 'EVENT' OR v_right_type <> 'EVENT') THEN
        RAISE EXCEPTION 'EVENT_TO_EVENT requires EVENT -> EVENT nodes, got % -> %', v_left_type, v_right_type;
    ELSIF NEW.match_type = 'ACCOUNT_TO_PERSON'
       AND (v_left_type <> 'ACCOUNT' OR v_right_type <> 'PERSON') THEN
        RAISE EXCEPTION 'ACCOUNT_TO_PERSON requires ACCOUNT -> PERSON nodes, got % -> %', v_left_type, v_right_type;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_candidate_match_node_types
BEFORE INSERT OR UPDATE ON candidate_match
FOR EACH ROW EXECUTE FUNCTION validate_candidate_match_node_types();

CREATE TABLE allocation_rule (
    rule_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    payer_person_id uuid REFERENCES person(person_id),
    payee_person_id uuid REFERENCES person(person_id),
    asset_id uuid REFERENCES asset(asset_id),
    percentage numeric(9,6) CHECK (percentage IS NULL OR (percentage >= 0 AND percentage <= 1)),
    fixed_amount numeric(38,18) CHECK (fixed_amount IS NULL OR fixed_amount >= 0),
    source_evidence_id uuid NOT NULL,
    fact_status varchar(16) NOT NULL DEFAULT 'CANDIDATE'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (percentage IS NOT NULL OR fixed_amount IS NOT NULL),
    FOREIGN KEY (case_id, source_evidence_id)
        REFERENCES evidence_item(case_id, evidence_id)
);

CREATE TABLE allocation_result (
    allocation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    run_id uuid NOT NULL,
    source_event_id uuid,
    payer_person_id uuid REFERENCES person(person_id),
    payee_person_id uuid REFERENCES person(person_id),
    amount numeric(38,18) NOT NULL CHECK (amount >= 0),
    asset_id uuid NOT NULL REFERENCES asset(asset_id),
    fiat_amount numeric(20,8),
    calculation_basis jsonb NOT NULL DEFAULT '{}'::jsonb,
    fact_status varchar(16) NOT NULL DEFAULT 'INFERENCE'
        CHECK (fact_status IN ('FACT','INFERENCE','CANDIDATE','DISPUTED','EXCLUDED','UNKNOWN')),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (case_id, run_id)
        REFERENCES analysis_run(case_id, run_id),
    FOREIGN KEY (case_id, source_event_id)
        REFERENCES money_event(case_id, event_id)
);

CREATE TABLE clue (
    clue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    clue_type varchar(32) NOT NULL,
    title varchar(200) NOT NULL,
    summary text NOT NULL,
    next_action text,
    uncertainty text,
    priority smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    status varchar(24) NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT','PENDING_REVIEW','CONFIRMED','DISMISSED','ACTIONED')),
    generated_by_run_id uuid,
    created_by uuid NOT NULL REFERENCES system_user(user_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, clue_id),
    FOREIGN KEY (case_id, generated_by_run_id)
        REFERENCES analysis_run(case_id, run_id)
);

CREATE TABLE clue_basis (
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    clue_id uuid NOT NULL,
    node_id uuid NOT NULL,
    basis_role varchar(24) NOT NULL
        CHECK (basis_role IN ('FACT','SUPPORT','CONTRADICT','MISSING','NEXT_ACTION')),
    description text NOT NULL,
    PRIMARY KEY (clue_id, node_id, basis_role),
    FOREIGN KEY (case_id, clue_id)
        REFERENCES clue(case_id, clue_id) ON DELETE CASCADE,
    FOREIGN KEY (case_id, node_id)
        REFERENCES entity_node(case_id, node_id)
);

CREATE TABLE case_access (
    case_id uuid NOT NULL REFERENCES investigation_case(case_id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES system_user(user_id) ON DELETE CASCADE,
    access_level varchar(16) NOT NULL
        CHECK (access_level IN ('READ','CONTRIBUTE','REVIEW','ADMIN')),
    granted_by uuid NOT NULL REFERENCES system_user(user_id),
    granted_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    PRIMARY KEY (case_id, user_id),
    CHECK (expires_at IS NULL OR expires_at >= granted_at)
);

CREATE TABLE monitor_subscription (
    subscription_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES investigation_case(case_id),
    account_id uuid NOT NULL REFERENCES financial_account(account_id),
    asset_id uuid REFERENCES asset(asset_id),
    last_seen_at timestamptz,
    rule_config jsonb NOT NULL DEFAULT '{}'::jsonb,
    status varchar(16) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE','PAUSED','CLOSED')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE monitor_alert (
    alert_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subscription_id uuid NOT NULL REFERENCES monitor_subscription(subscription_id),
    event_id uuid REFERENCES money_event(event_id),
    alert_type varchar(32) NOT NULL,
    alert_time timestamptz NOT NULL DEFAULT now(),
    status varchar(24) NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','ACKNOWLEDGED','DISMISSED','LINKED_TO_CLUE')),
    explanation jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE audit_log (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid REFERENCES investigation_case(case_id),
    actor_id uuid NOT NULL REFERENCES system_user(user_id),
    action varchar(64) NOT NULL,
    object_type varchar(64) NOT NULL,
    object_id uuid NOT NULL,
    before_json jsonb,
    after_json jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_mutation_on_append_only_table()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only table % cannot be updated or deleted', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER trg_source_record_append_only
BEFORE UPDATE OR DELETE ON source_record
FOR EACH ROW EXECUTE FUNCTION reject_mutation_on_append_only_table();

CREATE TRIGGER trg_audit_log_append_only
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION reject_mutation_on_append_only_table();

REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC;
REVOKE UPDATE, DELETE ON source_record FROM PUBLIC;

CREATE INDEX idx_money_event_case_time ON money_event(case_id, event_time DESC);
CREATE INDEX idx_money_event_from ON money_event(from_account_id, event_time DESC);
CREATE INDEX idx_money_event_to ON money_event(to_account_id, event_time DESC);
CREATE INDEX idx_money_event_tx ON money_event(network_id, tx_hash, tx_index);
CREATE INDEX idx_account_identifier_norm ON account_identifier(namespace_network_id, identifier_type, value_norm);
CREATE INDEX idx_relation_case_status ON investigative_relation(case_id, fact_status, relation_type);
CREATE INDEX idx_match_case_status ON candidate_match(case_id, review_status);
CREATE INDEX idx_evidence_case_type ON evidence_item(case_id, evidence_type);
CREATE INDEX idx_claim_predicate_time ON claim(predicate, event_time_from, event_time_to);
CREATE INDEX idx_clue_case_status ON clue(case_id, status, priority);

-- Idempotent import guards. tx_index_kind makes the deduplication key explicit
-- across EVM/TRC20 transfer logs, UTXO outputs and platform internal lines.
CREATE UNIQUE INDEX uq_money_event_onchain_idempotent
ON money_event (
    case_id,
    network_id,
    tx_hash,
    COALESCE(tx_index, -1),
    asset_id,
    COALESCE(from_account_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(to_account_id, '00000000-0000-0000-0000-000000000000'::uuid)
)
WHERE event_type = 'ONCHAIN_TRANSFER' AND tx_hash IS NOT NULL;

CREATE UNIQUE INDEX uq_money_event_offchain_reference
ON money_event (case_id, network_id, event_type, reference_no)
WHERE event_type <> 'ONCHAIN_TRANSFER' AND reference_no IS NOT NULL;
