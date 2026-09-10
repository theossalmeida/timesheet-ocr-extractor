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


def main():
    pool.open()
    try:
        rate = conversion_rate()
        if not rate:
            print('No USD/BRL rate available; run again when the quote service is reachable.')
            return
        with pool.connection() as conn:
            rows = conn.execute('SELECT * FROM ai_usage WHERE metered AND cost_usd IS NULL ORDER BY created_at').fetchall()
            if not rows:
                print('Nothing to reprice.')
                return
            priced = price_calls([dict(row) for row in rows], rate)
            repaired = 0
            for row, call in zip(rows, priced):
                if call['cost_usd'] is None:
                    print('Still unpriced:', row['id'], call['model'])
                    continue
                conn.execute('UPDATE ai_usage SET input_usd=%s,output_usd=%s,cost_usd=%s,usd_brl_rate=%s,cost_brl=%s WHERE id=%s', (call['input_usd'],call['output_usd'],call['cost_usd'],call['usd_brl_rate'],call['cost_brl'],row['id']))
                repaired += 1
            # A document costs the sum of its calls, and only once every call
            # of that document is priced.
            conn.execute("""UPDATE extractions e SET cost_brl = totals.cost_brl FROM (
                SELECT extraction_id, sum(cost_brl) AS cost_brl FROM ai_usage
                GROUP BY extraction_id HAVING count(*) FILTER (WHERE cost_brl IS NULL) = 0
            ) AS totals WHERE e.id = totals.extraction_id AND e.cost_brl IS DISTINCT FROM totals.cost_brl""")
        print('AI calls repriced:', repaired, 'at USD/BRL', round(rate, 4))
    finally:
        pool.close()


if __name__ == '__main__':
    main()
