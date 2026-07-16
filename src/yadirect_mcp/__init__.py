"""MCP server for Yandex Direct reporting and guarded campaign setup."""


def main() -> None:
    """Start the server without loading configuration on package import."""
    from .server import main as run

    run()


__all__ = ["main"]
