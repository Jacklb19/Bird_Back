"""ASGI entry point for the configured Vercel Python function."""
from birdnet_api.app import app

__all__ = ["app"]
