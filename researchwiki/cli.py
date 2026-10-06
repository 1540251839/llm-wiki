from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .app import delete_source, do_ask, do_search, ingest, initialize, parse_only, rebuild
from .config import load_config
from .evaluation import run_evaluation
from .graph import extract_graph
from .index.database import Database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rw", description="Local provenance-aware ResearchWiki")
    parser.add_argument("--root", default=".", help="ResearchWiki workspace root")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the local workspace")
    parse_cmd = sub.add_parser("parse", help="extract structured paper JSON without writing Wiki pages")
    parse_cmd.add_argument("paper")
    ingest_cmd = sub.add_parser("ingest", help="import a paper and update the Wiki")
    ingest_cmd.add_argument("paper")
    ingest_cmd.add_argument("--resume", action="store_true", help="re-run an existing/failed import")
    ingest_cmd.add_argument("--quality", choices=("standard", "high"), default=None, help="use one-call or two-stage extraction")
    search_cmd = sub.add_parser("search", help="search indexed Wiki pages")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--top-k", type=int, default=None)
    ask_cmd = sub.add_parser("ask", help="answer a question from retrieved Wiki pages")
    ask_cmd.add_argument("question")
    ask_cmd.add_argument("--top-k", type=int, default=None)
    sub.add_parser("build", help="rebuild the Wiki index and page links")
    sub.add_parser("graph", help="rebuild the deterministic graph contract")
    eval_cmd = sub.add_parser("eval", help="run the fixed replayable QA evaluation")
    eval_cmd.add_argument("--profile", choices=("baseline", "optimized"), default="optimized")
    jobs_cmd = sub.add_parser("jobs", help="list persistent ingestion jobs")
    jobs_cmd.add_argument("--status", default="")
    reviews_cmd = sub.add_parser("reviews", help="list open quality reviews")
    reviews_cmd.add_argument("--status", default="OPEN")
    delete_cmd = sub.add_parser("delete", help="move one source and generated-only pages to workspace trash")
    delete_cmd.add_argument("source_id")
    delete_cmd.add_argument("--yes", action="store_true", help="confirm the recoverable deletion")
    delete_cmd.add_argument("--keep-raw", action="store_true", help="leave the immutable raw copy in place")
    web_cmd = sub.add_parser("web", help="start the local API and browser workbench")
    web_cmd.add_argument("--host", default="127.0.0.1")
    web_cmd.add_argument("--port", type=int, default=8000)
    web_cmd.add_argument("--reload", action="store_true", help="reload when Python or Web assets change")
    return parser


def main(argv: Any = None) -> None:
    args = _parser().parse_args(argv)
    config = load_config(args.root)
    try:
        if args.command == "init":
            paths = initialize(config)
            print("Initialized ResearchWiki in " + str(config.root))
            for key, value in paths.items():
                print(f"  {key}: {value}")
        elif args.command == "parse":
            print(json.dumps(parse_only(config, args.paper).to_dict(), ensure_ascii=False, indent=2))
        elif args.command == "ingest":
            result = ingest(config, args.paper, resume=args.resume, quality_mode=args.quality)
            if result["status"] == "already_exists":
                print("Already exists: " + str(result["title"]))
            else:
                print("Indexed: " + str(result["title"]))
                print("Paper: " + str(result["paper_path"]))
                for path in result["entity_paths"]:
                    print("Entity: " + path)
        elif args.command == "search":
            results = do_search(config, args.query, args.top_k)
            if not results:
                print("No results.")
            for index, result in enumerate(results, 1):
                print(f"{index}. [{result['type']}] {result['title']} — {result['path']}")
        elif args.command == "ask":
            print(do_ask(config, args.question, args.top_k))
        elif args.command == "build":
            stats = rebuild(config)
            print(f"Built index: {stats['pages']} pages, {stats['links']} links, {stats['stale']} stale pages removed")
        elif args.command == "graph":
            database = Database(config.db_path)
            try:
                print(json.dumps(extract_graph(database), ensure_ascii=False, indent=2))
            finally:
                database.close()
        elif args.command == "eval":
            print(json.dumps(run_evaluation(config, profile=args.profile), ensure_ascii=False, indent=2))
        elif args.command == "jobs":
            database = Database(config.db_path)
            try:
                print(json.dumps(database.list_ingest_jobs(status=args.status), ensure_ascii=False, indent=2))
            finally:
                database.close()
        elif args.command == "reviews":
            database = Database(config.db_path)
            try:
                print(json.dumps(database.list_reviews(status=args.status), ensure_ascii=False, indent=2))
            finally:
                database.close()
        elif args.command == "delete":
            if not args.yes:
                raise ValueError("deletion is recoverable but requires --yes")
            print(json.dumps(delete_source(config, args.source_id, delete_raw=not args.keep_raw), ensure_ascii=False, indent=2))
        elif args.command == "web":
            try:
                import uvicorn
            except ImportError as exc:
                raise RuntimeError("Web dependencies are missing; run: pip install -e .") from exc
            print(f"ResearchWiki Web: http://{args.host}:{args.port}")
            if args.reload:
                os.environ["RESEARCHWIKI_ROOT"] = str(config.root)
                uvicorn.run(
                    "researchwiki.web.server:create_server_app",
                    factory=True,
                    host=args.host,
                    port=args.port,
                    reload=True,
                )
            else:
                from .web import create_app

                uvicorn.run(create_app(config.root), host=args.host, port=args.port)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
