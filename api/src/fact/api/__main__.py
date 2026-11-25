"""`python -m fact.api`: run the API with uvicorn on the configured host and port."""

import uvicorn
from fact.api.app import create_app
from fact.core.settings import get_settings


def main() -> None:
    settings = get_settings()
    # log_config=None keeps uvicorn's loggers on the root structlog handler; the middleware logs requests itself.
    uvicorn.run(create_app(settings), host=settings.api.host, port=settings.api.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
