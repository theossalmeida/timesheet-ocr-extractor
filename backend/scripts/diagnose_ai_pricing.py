"""Explain why AI usage rows are being saved without a cost.

Reproduces the exact pricing call the backend makes, outside of a request, so
the failure - an unimportable litellm, an unmapped model, no exchange rate -
shows up directly instead of being read out of a log file.

    .venv\\Scripts\\python.exe scripts\\diagnose_ai_pricing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def main():
    print('GEMINI_MODEL  =', repr(settings.GEMINI_MODEL))
    print('USD_BRL_RATE  =', repr(settings.USD_BRL_RATE))
    print()

    print('1) Can litellm be imported?')
    try:
        import importlib.metadata
        import litellm  # noqa: F401  (import itself is the check; litellm has no __version__ attribute)
        print('   OK - litellm', importlib.metadata.version('litellm'))
    except Exception as e:
        print(f'   FAILED - {type(e).__name__}: {e}')
        print('   This is the likely cause: the release venv does not have litellm installed.')
        print('   Fix: deploy\\update-all.bat (rebuilds the venv from requirements.txt), then re-run this script.')
        return

    print()
    print('2) Does litellm have a price for the configured model?')
    model = 'gemini/' + (settings.GEMINI_MODEL or '').strip()
    try:
        from litellm import cost_per_token
        result = cost_per_token(model=model, custom_llm_provider='gemini', call_type='generate_content', prompt_tokens=1000, completion_tokens=500)
        print(f'   OK - {model} -> input/output USD per token pair: {result}')
    except Exception as e:
        print(f'   FAILED - {type(e).__name__}: {e}')
        print(f'   litellm has no price entry for "{model}". Calls to this model will always be')
        print('   recorded with tokens but no cost. Check GEMINI_MODEL against a name litellm knows,')
        print('   or update litellm: .venv\\Scripts\\python.exe -m pip install -U litellm')
        return

    print()
    print('3) Is a USD/BRL exchange rate available?')
    from services import fx
    rate = fx.usd_brl()
    if rate:
        print(f'   OK - {rate}')
    else:
        print('   FAILED - no rate from the daily quote, no cached rate, no USD_BRL_RATE fallback.')
        print('   Costs will be stored in USD only until a rate is available.')
        return

    print()
    print('Pricing pipeline is healthy. Run scripts\\reprice_ai_usage.py to backfill any rows')
    print('that were metered while it was not.')


if __name__ == '__main__':
    main()
