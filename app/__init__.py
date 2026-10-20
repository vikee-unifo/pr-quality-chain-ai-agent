"""PR Quality Chain.

On import: load pr-quality-chain/.env (real environment variables win), then apply safe LiteLLM defaults.
LiteLLM reads these at import time, so they are set before anything in the app can import it.
Explicit operator choices are kept (e.g. LITELLM_LOCAL_MODEL_COST_MAP=False to fetch fresh prices);
unset or empty values get the safe default.
"""

import os

from app.config import load_env_file

load_env_file()

_SAFE_DEFAULTS = {
    # The app owns .env loading (above); stop LiteLLM's own load_dotenv() from loading it a second way.
    "LITELLM_MODE": "PRODUCTION",
    # Use the price map bundled with the installed LiteLLM instead of downloading it on import.
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    # LiteLLM's debug logs include request parameters (prompts); only let errors through.
    "LITELLM_LOG": "ERROR",
}
for _name, _value in _SAFE_DEFAULTS.items():
    if not os.environ.get(_name):
        os.environ[_name] = _value
