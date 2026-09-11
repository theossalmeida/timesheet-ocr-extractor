"""Price AI calls that were recorded but never priced.

Token counts are captured at call time and prices are applied later, so a call
metered while pricing was broken (litellm missing from the release, an unknown
model) can be repaired from what is already stored - no reprocessing, no second
charge. Prices with today's rate; rows already priced are left untouched.

    .venv\\Scripts\\python.exe scripts\\reprice_ai_usage.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import pool
from documents import conversion_rate
from services.ai_pricing import price_calls


def warn_if_not_release_interpreter():
    """This script only needs a working litellm plus DATABASE_URL, so running it
    from any environment that has both is fine for repairing history. But if it
    differs from the one AutusBackend actually runs, fixing these rows here
    proves nothing about future ones - they will keep saving with no cost.
    """
    release_file = Path('C:/ProgramData/Autus/active-release.txt')
    if not release_file.exists():
        return
    release = Path(release_file.read_text(encoding='utf-8').strip())
    expected = (release / 'backend' / '.venv' / 'Scripts' / 'python.exe').resolve()
    actual = Path(sys.executable).resolve()
    if str(actual).lower() != str(expected).lower():
        print(f'Note: running under {actual}, not the release interpreter ({expected}).')
        print('This will still repair existing rows, but if pricing keeps failing for new')
        print('documents, run scripts\\diagnose_ai_pricing.py with the release interpreter.')
        print()


def reprice_missing(conn, rate):
    rows = conn.execute('SELECT * FROM ai_usage WHERE metered AND (cost_usd IS NULL OR cost_brl IS NULL) ORDER BY created_at').fetchall()
    if not rows:
        return 0
    priced = []
    for row in rows:
        call_rate = float(row['usd_brl_rate'] or rate or 0) or None
        if row['cost_usd'] is not None:
            call = dict(row)
            call['usd_brl_rate'] = call_rate
            call['cost_brl'] = round(float(row['cost_usd']) * call_rate, 6) if call_rate else None
        else:
            call = price_calls([dict(row)], call_rate)[0]
        priced.append(call)
    repaired = 0
    for row, call in zip(rows, priced):
        if call['cost_usd'] is None:
            print('Still unpriced:', row['id'], call['model'])
            continue
        conn.execute('UPDATE ai_usage SET input_usd=%s,output_usd=%s,cost_usd=%s,usd_brl_rate=%s,cost_brl=%s WHERE id=%s', (call['input_usd'],call['output_usd'],call['cost_usd'],call['usd_brl_rate'],call['cost_brl'],row['id']))
        repaired += 1
    conn.execute("""UPDATE extractions e SET cost_brl = totals.cost_brl FROM (
        SELECT extraction_id, sum(cost_brl) AS cost_brl FROM ai_usage
        GROUP BY extraction_id HAVING count(*) FILTER (WHERE cost_brl IS NULL) = 0
    ) AS totals WHERE e.id = totals.extraction_id AND e.cost_brl IS DISTINCT FROM totals.cost_brl""")
    return repaired


def main():
    warn_if_not_release_interpreter()
    pool.open()
    try:
        rate = conversion_rate()
        with pool.connection() as conn:
            repaired = reprice_missing(conn, rate)
        print('AI calls repriced:', repaired, 'at USD/BRL', round(rate, 4) if rate else 'unavailable')
    finally:
        pool.close()


if __name__ == '__main__':
    main()
