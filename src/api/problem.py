"""What a refusal looks like, declared once.

A failure is part of the contract and was not in it. `openapi.json` carried
200, 201, 202 and the 422 FastAPI adds for validation, and nothing else, so a
client could not know what a 400 or a 404 holds and had to guess from a
response it had seen. The guess was per client.

There were 164 `raise HTTPException` sites and at least three shapes among
them: most passed `{"code": ..., "message": ...}`, fourteen passed a bare
string, one passed an f-string. The majority wins and becomes the rule,
because the frontend already reads `detail.code` to tell a refusal it can act
on from one it can only show.

`refuse` is the one way to build one. Two routers had written the same helper
under the same private name.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel


class Problem(BaseModel):
    """A refusal a caller can branch on.

    `code` is for the program: stable, greppable, and the thing a client
    switches on. `message` is for the person, and may be reworded without
    breaking anybody.
    """

    code: str
    message: str


class ProblemResponse(BaseModel):
    """The wire shape. FastAPI wraps whatever `detail` holds in this key, so
    a caller reads `detail.code`, not `code`."""

    detail: Problem


def refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, detail=Problem(code=code, message=message).model_dump())


# Attached to every router, so an operation documents its refusals without
# each one listing them. A route that cannot return one of these is rare
# enough that over-declaring here is cheaper than under-declaring everywhere.
REFUSALS: dict[int | str, dict[str, type[BaseModel]]] = {
    400: {"model": ProblemResponse},
    403: {"model": ProblemResponse},
    404: {"model": ProblemResponse},
    409: {"model": ProblemResponse},
}
