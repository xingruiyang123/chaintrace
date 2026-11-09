-- PostgreSQL 15+ / 16+
-- 用法：先执行 schema_v1_1_1.sql，再执行本文件。
-- 全部测试在事务中运行，最后回滚，不留下测试数据。

\set ON_ERROR_STOP on
SET search_path TO chaintrace;

BEGIN;

DO $$
DECLARE
    v_user uuid;
    v_case uuid;
    v_case2 uuid;
    v_tron uuid;
    v_eth uuid;
    v_trx uuid;
    v_usdt uuid;
    v_account_a uuid;
    v_account_b uuid;
    v_account_c uuid;
    v_account_deposit uuid;
    v_person uuid;
    v_source_chain uuid;
    v_source_exchange uuid;
    v_source_sim uuid;
    v_record_chain uuid;
    v_record_exchange uuid;
    v_record_sim uuid;
    v_evidence uuid;
    v_event uuid;
    v_platform_event uuid;
    v_event_case2 uuid;
    v_node_person uuid;
    v_node_account_a uuid;
    v_node_account_b uuid;
    v_node_event uuid;
    v_node_platform_event uuid;
    v_node_claim uuid;
    v_node_evidence uuid;
    v_claim uuid;
    v_relation uuid;
    v_run uuid;
    v_run_case2 uuid;
    v_clue uuid;
    v_node_case2 uuid;
    v_failed boolean;
    v_count integer;
BEGIN
    INSERT INTO system_user(user_code, display_name, role_code)
    VALUES ('SMOKE-001', '冒烟测试用户', 'REVIEWER')
    RETURNING user_id INTO v_user;

    INSERT INTO investigation_case(case_no, case_name, case_type, status, created_by)
    VALUES ('SMOKE-001', 'V1.1 冒烟测试案件', 'FRAUD', 'ACTIVE', v_user)
    RETURNING case_id INTO v_case;

    INSERT INTO investigation_case(case_no, case_name, case_type, status, created_by)
    VALUES ('SMOKE-002', 'V1.1 跨案件隔离测试', 'FRAUD', 'ACTIVE', v_user)
    RETURNING case_id INTO v_case2;

    INSERT INTO case_access(case_id, user_id, access_level, granted_by)
    VALUES (v_case, v_user, 'ADMIN', v_user);

    INSERT INTO network(network_type, network_code, network_name, chain_id)
    VALUES ('BLOCKCHAIN', 'TRON', 'TRON', '728126428')
    RETURNING network_id INTO v_tron;

    INSERT INTO network(network_type, network_code, network_name, chain_id)
    VALUES ('BLOCKCHAIN', 'ETHEREUM', 'Ethereum', '1')
    RETURNING network_id INTO v_eth;

    INSERT INTO asset(network_id, symbol, asset_name, decimals)
    VALUES (v_tron, 'TRX', 'TRON native asset', 6)
    RETURNING asset_id INTO v_trx;

    v_failed := false;
    BEGIN
        INSERT INTO asset(network_id, symbol, asset_name, decimals)
        VALUES (v_tron, 'TRX', 'duplicate native asset', 6);
    EXCEPTION WHEN unique_violation THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'asset NULLS NOT DISTINCT constraint did not reject duplicate native asset';
    END IF;

    INSERT INTO asset(network_id, symbol, asset_name, contract_address, decimals)
    VALUES (v_tron, 'USDT', 'Tether USD on TRON', 'TRON-USDT-CONTRACT', 6)
    RETURNING asset_id INTO v_usdt;

    INSERT INTO financial_account(account_type, network_id, account_label, account_status, fact_status)
    VALUES ('ONCHAIN_WALLET', v_tron, '钱包 A', 'ACTIVE', 'FACT')
    RETURNING account_id INTO v_account_a;

    INSERT INTO financial_account(account_type, network_id, account_label, account_status, fact_status)
    VALUES ('ONCHAIN_WALLET', v_eth, '钱包 B', 'ACTIVE', 'CANDIDATE')
    RETURNING account_id INTO v_account_b;

    INSERT INTO financial_account(account_type, network_id, account_label, account_status, fact_status)
    VALUES ('EXCHANGE_ACCOUNT', v_tron, '平台账户 C', 'ACTIVE', 'CANDIDATE')
    RETURNING account_id INTO v_account_c;

    INSERT INTO financial_account(account_type, network_id, account_label, account_status, fact_status)
    VALUES ('ONCHAIN_WALLET', v_tron, '平台充值地址 C-TRON', 'ACTIVE', 'FACT')
    RETURNING account_id INTO v_account_deposit;

    INSERT INTO account_identifier(account_id, namespace_network_id, identifier_type, value_norm, value_display, is_primary)
    VALUES (v_account_a, v_tron, 'BLOCKCHAIN_ADDRESS', '0xSAME-FORMAT-ADDRESS', '0xSAME-FORMAT-ADDRESS', true);

    INSERT INTO account_identifier(account_id, namespace_network_id, identifier_type, value_norm, value_display, is_primary)
    VALUES (v_account_b, v_eth, 'BLOCKCHAIN_ADDRESS', '0xSAME-FORMAT-ADDRESS', '0xSAME-FORMAT-ADDRESS', true);

    v_failed := false;
    BEGIN
        INSERT INTO account_identifier(account_id, namespace_network_id, identifier_type, value_norm, value_display)
        VALUES (v_account_c, v_tron, 'BLOCKCHAIN_ADDRESS', '0xSAME-FORMAT-ADDRESS', 'duplicate');
    EXCEPTION WHEN unique_violation THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'identifier namespace constraint did not reject duplicate';
    END IF;

    INSERT INTO person(person_code, display_name, identity_status, id_no_hmac, id_no_key_version)
    VALUES ('P-SMOKE-001', '胡某', 'UNVERIFIED', repeat('a', 64), 'kms-v1')
    RETURNING person_id INTO v_person;

    INSERT INTO case_person(case_id, person_id, role_code, case_status, fact_status)
    VALUES (v_case, v_person, 'SUSPECT', 'KNOWN', 'FACT');

    INSERT INTO case_account(case_id, account_id, case_role, fact_status)
    VALUES (v_case, v_account_a, 'KNOWN_ASSOCIATE', 'FACT'),
           (v_case, v_account_c, 'EXCHANGE', 'CANDIDATE'),
           (v_case, v_account_deposit, 'EXCHANGE', 'FACT');

    INSERT INTO data_source(case_id, source_type, provider, is_simulated, reliability_level)
    VALUES (v_case, 'PUBLIC_CHAIN_API', 'smoke-chain', true, 'HIGH')
    RETURNING data_source_id INTO v_source_chain;

    INSERT INTO data_source(case_id, source_type, provider, is_simulated, reliability_level)
    VALUES (v_case, 'EXCHANGE_EXPORT', 'smoke-exchange', true, 'MEDIUM')
    RETURNING data_source_id INTO v_source_exchange;

    INSERT INTO data_source(case_id, source_type, provider, is_simulated, reliability_level)
    VALUES (v_case, 'SIMULATION', 'smoke-simulator', true, 'LOW')
    RETURNING data_source_id INTO v_source_sim;

    WITH payload AS (
        SELECT '{"tx_hash":"TX-SMOKE-001"}'::jsonb AS raw_payload
    )
    INSERT INTO source_record(case_id, data_source_id, external_record_id, record_type, raw_payload, raw_sha256)
    SELECT v_case, v_source_chain, 'TX-SMOKE-001', 'TRC20_TRANSFER', raw_payload,
           encode(digest(raw_payload::text, 'sha256'), 'hex')
    FROM payload
    RETURNING source_record_id INTO v_record_chain;

    WITH payload AS (
        SELECT '{"deposit_id":"DEP-SMOKE-001"}'::jsonb AS raw_payload
    )
    INSERT INTO source_record(case_id, data_source_id, external_record_id, record_type, raw_payload, raw_sha256)
    SELECT v_case, v_source_exchange, 'DEP-SMOKE-001', 'EXCHANGE_DEPOSIT', raw_payload,
           encode(digest(raw_payload::text, 'sha256'), 'hex')
    FROM payload
    RETURNING source_record_id INTO v_record_exchange;

    WITH payload AS (
        SELECT '{"simulated":true}'::jsonb AS raw_payload
    )
    INSERT INTO source_record(case_id, data_source_id, external_record_id, record_type, raw_payload, raw_sha256)
    SELECT v_case, v_source_sim, 'SIM-SMOKE-001', 'SIMULATED_TRANSFER', raw_payload,
           encode(digest(raw_payload::text, 'sha256'), 'hex')
    FROM payload
    RETURNING source_record_id INTO v_record_sim;

    SELECT count(*) INTO v_count
    FROM source_record
    WHERE case_id = v_case
      AND raw_sha256 = encode(digest(raw_payload::text, 'sha256'), 'hex');
    IF v_count <> 3 THEN
        RAISE EXCEPTION 'raw SHA-256 verification failed for % source records', v_count;
    END IF;

    INSERT INTO evidence_item(case_id, evidence_type, title, file_sha256, integrity_status, fact_status)
    VALUES (
        v_case,
        'TRANSACTION',
        '链上与平台记录交叉核验材料',
        encode(digest(convert_to('evidence-smoke-001', 'utf8'), 'sha256'), 'hex'),
        'PENDING',
        'CANDIDATE'
    )
    RETURNING evidence_id INTO v_evidence;

    INSERT INTO money_event(
        case_id, event_type, network_id, asset_id, from_account_id, to_account_id,
        amount, event_time, tx_hash, tx_index, tx_index_kind, primary_source_record_id, fact_status
    ) VALUES (
        v_case, 'ONCHAIN_TRANSFER', v_tron, v_usdt, v_account_a, v_account_deposit,
        1000, '2026-11-04 12:00:00+00', 'TX-SMOKE-001', 0, 'TRANSFER_LOG', v_record_chain, 'FACT'
    ) RETURNING event_id INTO v_event;

    INSERT INTO money_event_source_record(case_id, event_id, source_record_id, source_role, link_status)
    VALUES (v_case, v_event, v_record_chain, 'PRIMARY', 'ACCEPTED'),
           (v_case, v_event, v_record_sim, 'SUPPORTING', 'ACCEPTED');

    INSERT INTO money_event(
        case_id, event_type, network_id, asset_id, from_account_id, to_account_id,
        amount, event_time, reference_no, primary_source_record_id, fact_status
    ) VALUES (
        v_case, 'EXCHANGE_DEPOSIT', v_tron, v_usdt, v_account_deposit, v_account_c,
        1000, '2026-11-04 12:00:03+00', 'DEP-SMOKE-001', v_record_exchange, 'FACT'
    ) RETURNING event_id INTO v_platform_event;

    INSERT INTO money_event_source_record(case_id, event_id, source_record_id, source_role, link_status)
    VALUES (v_case, v_platform_event, v_record_exchange, 'PRIMARY', 'ACCEPTED');

    SELECT count(*) INTO v_count
    FROM money_event_source_record
    WHERE case_id = v_case AND event_id = v_event;
    IF v_count <> 2 THEN
        RAISE EXCEPTION 'expected two source records for one money event, got %', v_count;
    END IF;

    SELECT count(*) INTO v_count
    FROM data_source
    WHERE case_id = v_case AND is_simulated = false;
    IF v_count <> 0 THEN
        RAISE EXCEPTION 'synthetic smoke sources must all be marked is_simulated=true';
    END IF;

    v_failed := false;
    BEGIN
        INSERT INTO money_event(
            case_id, event_type, network_id, asset_id, from_account_id, to_account_id,
            amount, event_time, tx_hash, tx_index, tx_index_kind, primary_source_record_id, fact_status
        ) VALUES (
            v_case, 'ONCHAIN_TRANSFER', v_tron, v_usdt, v_account_a, v_account_deposit,
            1000, '2026-11-04 12:00:00+00', 'TX-SMOKE-001', 0, 'TRANSFER_LOG', v_record_chain, 'FACT'
        );
    EXCEPTION WHEN unique_violation THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'on-chain idempotency index did not reject duplicate import';
    END IF;

    INSERT INTO entity_node(case_id, node_type, ref_id, display_label)
    VALUES (v_case, 'PERSON', v_person, '胡某')
    RETURNING node_id INTO v_node_person;
    INSERT INTO entity_node(case_id, node_type, ref_id, display_label)
    VALUES (v_case, 'ACCOUNT', v_account_a, '钱包 A')
    RETURNING node_id INTO v_node_account_a;
    INSERT INTO entity_node(case_id, node_type, ref_id, display_label)
    VALUES (v_case, 'ACCOUNT', v_account_c, '平台账户 C')
    RETURNING node_id INTO v_node_account_b;
    INSERT INTO entity_node(case_id, node_type, ref_id, display_label)
    VALUES (v_case, 'EVENT', v_event, '资金事件 TX-SMOKE-001')
    RETURNING node_id INTO v_node_event;
    INSERT INTO entity_node(case_id, node_type, ref_id, display_label)
    VALUES (v_case, 'EVENT', v_platform_event, '平台入账 DEP-SMOKE-001')
    RETURNING node_id INTO v_node_platform_event;
    INSERT INTO entity_node(case_id, node_type, ref_id, display_label)
    VALUES (v_case, 'EVIDENCE', v_evidence, '交叉核验材料')
    RETURNING node_id INTO v_node_evidence;

    INSERT INTO investigative_relation(case_id, left_node_id, right_node_id, relation_type, fact_status, confidence_level, conclusion)
    VALUES (v_case, v_node_person, v_node_account_a, 'CONTROLS', 'CANDIDATE', 'MEDIUM', '待核验的人员—钱包关联')
    RETURNING relation_id INTO v_relation;

    INSERT INTO relation_evidence(case_id, relation_id, evidence_id, support_type, description)
    VALUES (v_case, v_relation, v_evidence, 'SUPPORTS', '交易与平台记录共同支持');

    INSERT INTO investigative_relation(case_id, left_node_id, right_node_id, relation_type, fact_status, confidence_level, conclusion)
    VALUES (v_case, v_node_event, v_node_platform_event, 'RECONCILES_WITH', 'CANDIDATE', 'MEDIUM', '链上转账与平台入账分别建模后进行候选核对');

    v_failed := false;
    BEGIN
        INSERT INTO relation_evidence(case_id, relation_id, evidence_id, support_type, description)
        VALUES (v_case, v_relation, v_evidence, 'MISSING', '缺失材料不应引用 evidence_id');
    EXCEPTION WHEN check_violation THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'relation_evidence accepted illegal MISSING support_type';
    END IF;

    INSERT INTO claim(case_id, evidence_id, subject_node_id, predicate, object_node_id, asset_id, amount, fact_status)
    VALUES (v_case, v_evidence, v_node_person, 'PAID_TO', v_node_account_c, v_usdt, 1000, 'CANDIDATE')
    RETURNING claim_id INTO v_claim;

    INSERT INTO entity_node(case_id, node_type, ref_id, display_label)
    VALUES (v_case, 'CLAIM', v_claim, '主张：胡某 PAID_TO 平台账户 C')
    RETURNING node_id INTO v_node_claim;

    INSERT INTO analysis_run(case_id, algorithm_code, algorithm_version, input_snapshot_hash, status, operator_id)
    VALUES (v_case, 'EVENT_CLAIM_MATCH', '1.1.0', repeat('5', 64), 'SUCCEEDED', v_user)
    RETURNING run_id INTO v_run;

    INSERT INTO candidate_match(case_id, run_id, left_node_id, right_node_id, match_type, score, explanation)
    VALUES (v_case, v_run, v_node_event, v_node_claim, 'EVENT_TO_CLAIM', 0.85, '{"amount":"1000","direction":"same"}');

    SELECT count(*) INTO v_count
    FROM candidate_match cm
    JOIN entity_node l ON l.case_id = cm.case_id AND l.node_id = cm.left_node_id
    JOIN entity_node r ON r.case_id = cm.case_id AND r.node_id = cm.right_node_id
    WHERE cm.case_id = v_case
      AND cm.match_type = 'EVENT_TO_CLAIM'
      AND l.node_type = 'EVENT'
      AND r.node_type = 'CLAIM';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'EVENT_TO_CLAIM node type verification failed';
    END IF;

    INSERT INTO clue(case_id, clue_type, title, summary, next_action, uncertainty, priority, status, generated_by_run_id, created_by)
    VALUES (v_case, 'UNKNOWN_ASSOCIATE', '平台账户 C 待核验', '资金事件与平台记录存在同向对应', '调取平台账户归属材料', '尚无实名账户凭据', 2, 'PENDING_REVIEW', v_run, v_user)
    RETURNING clue_id INTO v_clue;

    INSERT INTO clue_basis(case_id, clue_id, node_id, basis_role, description)
    VALUES (v_case, v_clue, v_node_event, 'FACT', '存在链上资金事件'),
           (v_case, v_clue, v_node_evidence, 'MISSING', '缺少平台实名关联材料');

    INSERT INTO investigation_case(case_no, case_name, case_type, status, created_by)
    VALUES ('SMOKE-003', 'V1.1 节点隔离辅助案件', 'FRAUD', 'ACTIVE', v_user)
    RETURNING case_id INTO v_case2;

    INSERT INTO entity_node(case_id, node_type, ref_id, display_label)
    VALUES (v_case2, 'CASE', v_case2, '第二案件节点')
    RETURNING node_id INTO v_node_case2;

    INSERT INTO money_event(case_id, event_type, network_id, asset_id, amount, event_time, fact_status)
    VALUES (v_case2, 'UNKNOWN', v_tron, v_usdt, 1, '2026-11-04 13:00:00+00', 'FACT')
    RETURNING event_id INTO v_event_case2;

    INSERT INTO analysis_run(case_id, algorithm_code, algorithm_version, input_snapshot_hash, status, operator_id)
    VALUES (v_case2, 'EVENT_CLAIM_MATCH', '1.1.1', repeat('6', 64), 'SUCCEEDED', v_user)
    RETURNING run_id INTO v_run_case2;

    v_failed := false;
    BEGIN
        INSERT INTO investigative_relation(case_id, left_node_id, right_node_id, relation_type)
        VALUES (v_case, v_node_person, v_node_case2, 'ASSOCIATED_WITH');
    EXCEPTION WHEN foreign_key_violation THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'cross-case relation was accepted';
    END IF;

    v_failed := false;
    BEGIN
        INSERT INTO candidate_match(case_id, run_id, left_node_id, right_node_id, match_type, score)
        VALUES (v_case, v_run_case2, v_node_event, v_node_claim, 'EVENT_TO_CLAIM', 0.5);
    EXCEPTION WHEN foreign_key_violation THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'candidate_match accepted an analysis run from another case';
    END IF;

    v_failed := false;
    BEGIN
        INSERT INTO money_event(case_id, event_type, network_id, asset_id, amount, event_time, parent_event_id, fact_status)
        VALUES (v_case, 'UNKNOWN', v_tron, v_usdt, 1, '2026-11-04 14:00:00+00', v_event_case2, 'FACT');
    EXCEPTION WHEN foreign_key_violation THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'money_event accepted a parent event from another case';
    END IF;

    INSERT INTO audit_log(case_id, actor_id, action, object_type, object_id, after_json)
    VALUES (v_case, v_user, 'EXPORT', 'CLUE', v_clue, '{"exported":true}');

    v_failed := false;
    BEGIN
        UPDATE source_record SET raw_payload = '{"tampered":true}' WHERE source_record_id = v_record_chain;
    EXCEPTION WHEN raise_exception THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'source_record update was not blocked';
    END IF;

    v_failed := false;
    BEGIN
        DELETE FROM audit_log WHERE case_id = v_case;
    EXCEPTION WHEN raise_exception THEN
        v_failed := true;
    END;
    IF NOT v_failed THEN
        RAISE EXCEPTION 'audit_log delete was not blocked';
    END IF;

    RAISE NOTICE 'V1.1 smoke checks passed: namespace, NULLS NOT DISTINCT, multi-source event, case-scope FKs, missing evidence separation, HMAC columns, clue basis and append-only guards.';
END;
$$;

ROLLBACK;
