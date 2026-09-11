"""Explain why AI usage rows are being saved without a cost.

Calls the actual pricing function the backend uses (services.ai_pricing.usd_cost)
with a representative call, so any failure - an unimportable litellm, a version
mismatch in one of its dependencies, an unmapped model, no exchange rate - shows
up with its real traceback instead of a guess from a simplified reproduction.

    .venv\\Scripts\\python.exe scripts\\diagnose_ai_pricing.py
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def check_running_interpreter():
    """The AutusBackend service runs a specific python.exe under
    C:\\ProgramData\\Autus\\releases\\<revision>\\backend\\.venv. Every check
    below tests *this* interpreter's environment - if it is not that one
    (a local dev venv under the git checkout, say), a clean result here proves
    nothing about what the running service actually does.
    """
    release_file = Path('C:/ProgramData/Autus/active-release.txt')
    if not release_file.exists():
        print('   (active-release.txt not found - skipping this check; not on the production host?)')
        return
    release = Path(release_file.read_text(encoding='utf-8').strip())
    expected = (release / 'backend' / '.venv' / 'Scripts' / 'python.exe').resolve()
    actual = Path(sys.executable).resolve()
    if str(actual).lower() == str(expected).lower():
        print(f'   OK - running as the release interpreter ({actual})')
        return
    print(f'   MISMATCH - this script is running under:\n     {actual}')
    print(f'   but AutusBackend runs under:\n     {expected}')
    print('   Every check below is testing the WRONG environment. Re-run with:')
    print(f'     & "{expected}" "{Path(__file__).resolve()}"')
    sys.exit(1)


def main():
    print('GEMINI_MODEL  =', repr(settings.GEMINI_MODEL))
    print('USD_BRL_RATE  =', repr(settings.USD_BRL_RATE))
    print()

    print('0) Is this the interpreter the Windows service actually runs?')
    check_running_interpreter()

    print()
    print('1) Is the bundled LiteLLM price table available?')
    try:
        import importlib.metadata
        from services.ai_pricing import model_prices
        print('   OK - litellm', importlib.metadata.version('litellm'), 'models:', len(model_prices()))
    except Exception:
        print('   FAILED:')
        traceback.print_exc()
        return

    print()
    print('2) Does the real pricing function price a representative call?')
    print('   (calls services.ai_pricing.usd_cost directly - the same code finish_extraction runs)')
    from services.ai_pricing import usd_cost
    sample = {
        'provider': 'gemini',
        'model': (settings.GEMINI_MODEL or '').strip(),
        'kind': 'extract',
        'metered': True,
        'prompt_tokens': 905,
        'cached_tokens': 0,
        'output_tokens': 777,
        'thought_tokens': 697,
        'total_tokens': 1682,
    }
    import logging
    logging.basicConfig(level=logging.ERROR)
    result = usd_cost(sample)
    if result is None:
        print('   FAILED - usd_cost returned None. The exact exception was logged above')
        print('   (services.ai_pricing logs it at ERROR level with the exception class name).')
        return
    print(f'   OK - input/output USD for this call: {result}')

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
