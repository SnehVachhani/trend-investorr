-- Replace the original named delivery schedule once while preserving later edits.
UPDATE app_config
SET value = '0.0'
WHERE key IN ('charges_brokerage_rate_pct', 'charges_brokerage_cap_rupees', 'charges_brokerage_min_rupees')
  AND EXISTS (
      SELECT 1 FROM app_config
      WHERE key = 'charges_broker_name' AND value = 'Angel One SmartAPI'
  );

UPDATE app_config
SET value = '12.5'
WHERE key = 'charges_dp_charge_per_sell_rupees'
  AND EXISTS (
      SELECT 1 FROM app_config
      WHERE key = 'charges_broker_name' AND value = 'Angel One SmartAPI'
  );

DELETE FROM app_config WHERE key = 'charges_broker_name';
