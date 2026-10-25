-- Application value semantics over PostgreSQL; all storage/locking is native.
-- Dates remain ISO-8601 text at existing public API boundaries. Invalid dates
-- are unknown (NULL), never "now" or an eligibility grant.
-- Keep application lookups in the owning schema even during index creation,
-- when PostgreSQL restricts the caller's search_path to catalog/temp schemas.
CREATE OR REPLACE FUNCTION ap_day(value text, modifier text DEFAULT NULL)
RETURNS double precision LANGUAGE plpgsql STABLE AS $$
DECLARE instant timestamptz;
BEGIN
    IF value IS NULL OR trim(value) = '' THEN RETURN NULL; END IF;
    instant := CASE WHEN value='now' THEN CURRENT_TIMESTAMP ELSE value::timestamptz END;
    IF modifier IS NOT NULL THEN instant := instant + modifier::interval; END IF;
    RETURN extract(epoch FROM instant)::double precision / 86400.0;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow OR invalid_text_representation
THEN RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION ap_utc(value double precision)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
SELECT to_char(to_timestamp(value) AT TIME ZONE INTERVAL '0 hours','YYYY-MM-DD"T"HH24:MI:SS"Z"') $$;
CREATE OR REPLACE FUNCTION ap_datetime(value text, modifier text DEFAULT NULL)
RETURNS text LANGUAGE sql STABLE SET search_path FROM CURRENT AS $$
SELECT ap_utc(ap_day(value, modifier) * 86400.0) $$;
CREATE OR REPLACE FUNCTION ap_date(value text)
RETURNS text LANGUAGE sql STABLE SET search_path FROM CURRENT AS $$
SELECT substr(ap_datetime(value), 1, 10) $$;
CREATE OR REPLACE FUNCTION ap_json(value text)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
BEGIN RETURN value::jsonb;
EXCEPTION WHEN invalid_text_representation THEN RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION ap_json_valid(value text)
RETURNS boolean LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path FROM CURRENT AS $$
SELECT ap_json(value) IS NOT NULL $$;
CREATE OR REPLACE FUNCTION ap_json_text(value text, path text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path FROM CURRENT AS $$
SELECT ap_json(value) #>> string_to_array(substr(path,3),'.') $$;
CREATE OR REPLACE FUNCTION ap_json_number(value text, path text)
RETURNS numeric LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE SET search_path FROM CURRENT AS $$
DECLARE v jsonb;
BEGIN
    v := ap_json(value) #> string_to_array(substr(path,3),'.');
    IF jsonb_typeof(v) = 'number' THEN RETURN (v #>> '{}')::numeric; END IF;
    RETURN NULL;
END $$;
CREATE OR REPLACE FUNCTION ap_json_flag(value text, path text)
RETURNS integer LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path FROM CURRENT AS $$
SELECT CASE ap_json(value) #> string_to_array(substr(path,3),'.')
    WHEN 'true'::jsonb THEN 1 WHEN 'false'::jsonb THEN 0
    WHEN '1'::jsonb THEN 1 WHEN '0'::jsonb THEN 0 ELSE NULL END $$;
CREATE OR REPLACE FUNCTION ap_json_array_length(value text, path text DEFAULT NULL)
RETURNS integer LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path FROM CURRENT AS $$
SELECT CASE WHEN jsonb_typeof(v)='array' THEN jsonb_array_length(v) ELSE NULL END
FROM (SELECT CASE WHEN path IS NULL THEN ap_json(value)
                 ELSE ap_json(value) #> string_to_array(substr(path,3),'.') END v) x $$;
-- PostgreSQL overloads for application telemetry rounded to decimal places.
CREATE OR REPLACE FUNCTION round(value double precision, places integer)
RETURNS numeric LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
SELECT pg_catalog.round(value::numeric,places) $$;

-- Native policy keys. Keep in parity with scoring.tailor.resolve_company_key.
CREATE OR REPLACE FUNCTION ap_company_key(company text, site text, strategy text, target text, url text)
RETURNS text LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE key text; address text := lower(coalesce(target,''));
BEGIN
    key := nullif(lower(btrim(company)),'');
    IF key IS NOT NULL THEN RETURN key; END IF;
    key := substring(address from '(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([a-z0-9-]+)/');
    IF key IS NULL THEN key := substring(address from 'jobs\.lever\.co/([a-z0-9-]+)/'); END IF;
    IF key IS NULL THEN key := substring(address from 'jobs\.ashbyhq\.com/([a-z0-9-]+)/'); END IF;
    IF key IS NULL THEN key := substring(address from 'https?://([a-z0-9-]+)\.wd[0-9]+\.myworkdayjobs\.com/'); END IF;
    IF key IS NOT NULL THEN RETURN key; END IF;
    IF lower(strategy) IN ('greenhouse_api','workday_api','lever_api','ashby_api','amazon_jobs',
        'microsoft_careers','apple_jobs','google_careers','meta_careers','twilio_greenhouse')
    THEN RETURN nullif(lower(btrim(site)),''); END IF;
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION ap_public_id(value text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT substr(encode(sha256(convert_to(coalesce(value,''),'UTF8')),'hex'),1,24)
$$;

CREATE OR REPLACE FUNCTION ap_company_under_cap(company text, site text, strategy text,
    target text, candidate_url text, policy_text text)
RETURNS boolean LANGUAGE plpgsql STABLE SET search_path FROM CURRENT AS $$
DECLARE key text := ap_company_key(company,site,strategy,target,candidate_url);
        policy jsonb := policy_text::jsonb; cap integer; window_days integer; occupied bigint;
BEGIN
    IF key IS NULL THEN RETURN true; END IF;
    cap := coalesce((policy->'overrides'->key->>'cap')::integer,(policy->>'default_cap')::integer);
    window_days := coalesce((policy->'overrides'->key->>'window')::integer,(policy->>'default_window')::integer);
    IF cap < 0 THEN RETURN true; END IF;
    IF cap IS NULL OR window_days IS NULL THEN RAISE EXCEPTION 'Company policy is incomplete'; END IF;
    SELECT count(*) INTO occupied FROM jobs j LEFT JOIN application_routes r ON r.job_url=j.url
    WHERE j.url<>candidate_url
      AND ap_company_key(j.company,j.site,j.strategy,coalesce(r.target,j.application_url),j.url)=key
      AND (j.apply_status IN ('applied','in_progress') OR j.state IN ('verification_required','verification_quarantined')
        OR EXISTS (SELECT 1 FROM application_attempts aa WHERE aa.job_url=j.url
          AND aa.status IN ('prepared','side_effect_intent','verification_required','verification_quarantined','submitted')))
      AND (j.apply_status='in_progress' OR j.state IN ('verification_required','verification_quarantined')
        OR EXISTS (SELECT 1 FROM application_attempts aa WHERE aa.job_url=j.url
          AND aa.status IN ('prepared','side_effect_intent','verification_required','verification_quarantined'))
        OR coalesce(ap_day(coalesce(j.applied_at,j.last_attempted_at,
          (SELECT max(coalesce(aa.submitted_at,aa.updated_at,aa.prepared_at)) FROM application_attempts aa
           WHERE aa.job_url=j.url AND aa.status IN ('prepared','side_effect_intent','verification_required','verification_quarantined','submitted')))),
          ap_day('now')) > ap_day('now')-window_days);
    RETURN occupied < cap;
END $$;
