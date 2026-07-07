"""cf-publish — deploy a local folder to Cloudflare Pages, no wrangler needed."""

from .pages import DeployResult, PagesError, deploy
from .r2 import R2SyncResult, sync

__version__ = "0.2.0"
__all__ = ["deploy", "DeployResult", "sync", "R2SyncResult", "PagesError", "__version__"]
