-- 109: provenance for daily-price rows written by the source chain. NULL = the yfinance load or the daily
-- bhavcopy job (the historical rows); 'nse_bhavcopy:fill' = a session `gaps` copied in from equity.nse_bhav_full,
-- put on yfinance's basis (split-adjusted close, all-factor adj_close). Metadata-only change. Idempotent.
alter table equity.historical_prices add column if not exists source text;
