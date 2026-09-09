"""Results query API (#19).

Server-side filtering and aggregation over persisted plans and verification
records. The endpoint returns chart/table-ready JSON computed entirely by
`aco.results`; clients never see raw SQL and never re-implement the
statistics.
"""

import sqlite3

from fastapi import FastAPI

from .. import results


def register_routes(app: FastAPI, conn: sqlite3.Connection) -> None:
    @app.get("/v1/results")
    async def list_results(task_set: str | None = None, config: str | None = None,
                           scorer: str | None = None, view: str = "raw",
                           batch: str | None = None):
        # AppErrors (invalid view / unparsable filters) are handled by the
        # app-wide handler, like every other endpoint
        return results.collect(conn, task_set=task_set, config=config,
                               scorer=scorer, view=view, batch=batch)
