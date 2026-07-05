"""cf-publish — deploy a local folder to Cloudflare Pages, no wrangler needed."""

from .pages import DeployResult, PagesError, deploy

__version__ = "0.1.0"
__all__ = ["deploy", "DeployResult", "PagesError", "__version__"]
