-- 113: a filled daily-price row (source 'nse_bhavcopy:fill') that another writer (yfinance, the daily bhavcopy job)
-- updates becomes that writer's row: source cleared, so the fill never overwrites it again. The fill marks its own
-- writes with `set local equity.writer = 'fill'`. Idempotent.
create or replace function equity.trg_price_fill_source() returns trigger language plpgsql as $$
begin
    if old.source = 'nse_bhavcopy:fill' and coalesce(current_setting('equity.writer', true), '') <> 'fill' then
        new.source := null;
    end if;
    return new;
end $$;
drop trigger if exists price_fill_source on equity.historical_prices;
create trigger price_fill_source before update on equity.historical_prices
    for each row execute function equity.trg_price_fill_source();
