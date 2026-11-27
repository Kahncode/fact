"""`python -m fact.api`: run the API with uvicorn on the configured host and port, until its stop file appears."""

import uvicorn
from fact.api.app import create_app
from fact.core.settings import get_settings
from fact.core.stop_file import stop_file_path, watch_stop_file


def main() -> None:
    settings = get_settings()
    # log_config=None keeps uvicorn's loggers on the root structlog handler; the middleware logs requests itself.
    config = uvicorn.Config(
        create_app(settings), host=settings.api.host, port=settings.api.port, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    if (path := stop_file_path()) is not None:  # started by `fact dev`: shut down as uvicorn does on Ctrl-C

        def stop() -> None:
            server.should_exit = True

        watch_stop_file(path, stop)
    server.run()


if __name__ == "__main__":
    main()
