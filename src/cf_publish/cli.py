"""Command-line interface for cf-publish."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from . import __version__
from .pages import ENV_FILE, PagesError, deploy


def main(argv: "list[str] | None" = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Subcommand routing: `cf-publish r2 sync ...`. The bare deploy form
    # (`cf-publish DIR --project X`) stays as-is for backwards compatibility.
    if argv[:1] == ["r2"]:
        return _main_r2(argv[1:])
    ap = argparse.ArgumentParser(
        prog="cf-publish",
        description="Deploy a local folder to Cloudflare Pages via the "
                    "Direct Upload API — no wrangler, no npm.",
        epilog=(
            "credentials: set CLOUDFLARE_API_TOKEN (an API token with the "
            "'Cloudflare Pages: Edit' permission) and CLOUDFLARE_ACCOUNT_ID "
            "(shown on the dashboard's overview page) as environment "
            f"variables, or put KEY=VALUE lines in {ENV_FILE}."
        ),
    )
    ap.add_argument("directory",
                    help="folder to publish (its contents become the site)")
    ap.add_argument("--project", required=True, help="Pages project name")
    ap.add_argument("--branch", default="main",
                    help="'main' deploys to production, anything else gets "
                         "a preview URL (default: main)")
    ap.add_argument("--no-create", action="store_true",
                    help="fail if the project does not exist instead of creating it")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="fnmatch pattern to skip, matched against the relative "
                         "path and the filename; repeatable (e.g. '*.map')")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be uploaded, deploy nothing")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--quiet", action="store_true",
                     help="suppress progress; print only the deployment URL")
    out.add_argument("--json", action="store_true", dest="as_json",
                     help="suppress progress on stdout; print a JSON result")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    if args.quiet:
        progress = lambda msg: None  # noqa: E731
    else:
        progress = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    try:
        result = deploy(args.directory, args.project, branch=args.branch,
                        create=not args.no_create, exclude=args.exclude,
                        dry_run=args.dry_run, on_progress=progress)
    except PagesError as exc:
        print(f"cf-publish: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.as_json:
        print(json.dumps(dataclasses.asdict(result)))
    elif args.quiet:
        if result.url:
            print(result.url)


def _main_r2(argv: "list[str]") -> None:
    from .r2 import sync

    ap = argparse.ArgumentParser(
        prog="cf-publish r2",
        description="Sync a local folder to a Cloudflare R2 bucket "
                    "(S3-compatible API, no boto3/rclone needed).",
        epilog=(
            "credentials: set R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY "
            "(dashboard → R2 → Manage R2 API Tokens; NOT the Pages token) "
            "and CLOUDFLARE_ACCOUNT_ID as environment variables, or put "
            f"KEY=VALUE lines in {ENV_FILE}."
        ),
    )
    ap.add_argument("command", choices=["sync"])
    ap.add_argument("directory", help="local folder to sync")
    ap.add_argument("bucket", metavar="BUCKET[/PREFIX]",
                    help="destination bucket, optionally with a key prefix")
    ap.add_argument("--delete", action="store_true",
                    help="also delete remote objects that no longer exist locally")
    ap.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                    help="fnmatch pattern to skip; repeatable")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change, transfer nothing")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--quiet", action="store_true",
                     help="suppress progress output")
    out.add_argument("--json", action="store_true", dest="as_json",
                     help="print a JSON result")
    args = ap.parse_args(argv)

    progress = (lambda msg: None) if args.quiet else (
        lambda msg: print(msg, file=sys.stderr))
    try:
        result = sync(args.directory, args.bucket, delete=args.delete,
                      exclude=args.exclude, dry_run=args.dry_run,
                      on_progress=progress)
    except PagesError as exc:
        print(f"cf-publish: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.as_json:
        print(json.dumps(dataclasses.asdict(result)))


if __name__ == "__main__":
    main()
