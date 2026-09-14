"""
Vercel entry point for the REST API.

Deliberately at the repository root rather than inside `api/`: Vercel turns
every file under a top-level `api/` directory into its own function, which
would expose `config.py`, `repository.py` and friends as endpoints. Declaring
this single file in vercel.json keeps one function serving every route.

Vercel does not reliably run ASGI lifespan events, so the database pool is
created on first request -- see api/state.py.
"""

from api.config import ApiConfig
from api.main import create_app

app = create_app(ApiConfig.from_env())
