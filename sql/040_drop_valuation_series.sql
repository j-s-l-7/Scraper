-- Move long valuation_series into wide valuation_history, drop it, and drop the weekly price rows
-- (yfinance daily history replaces them). Idempotent: does nothing once valuation_series is gone.
do $$
declare
  known text[] := array['pe','median_pe','eps','pbv','median_pbv','book_value','ev_multiple','median_ev_multiple',
                        'ebitda','mcap_sales','median_mcap_sales','sales','gpm','opm','npm','quarter_sales'];
  sel text;
  c text;
begin
  if to_regclass('equity.valuation_series') is not null then
    sel := '';
    foreach c in array known loop
      sel := sel || format(', max(value) filter (where series=%L)::real as %I', c, c);
    end loop;
    execute format($f$
      insert into equity.valuation_history (company_id, basis, obs_date, %s, extra)
      select company_id, basis, obs_date %s,
             nullif(jsonb_strip_nulls(coalesce(
               jsonb_object_agg(series, value) filter (where series <> all(%L::text[])), '{}')), '{}')
      from equity.valuation_series
      group by company_id, basis, obs_date
      on conflict (company_id, basis, obs_date) do nothing$f$,
      array_to_string(known, ', '), sel, known);
    drop table equity.valuation_series;
  end if;
end $$;

delete from equity.price_history where frequency = 'W';
