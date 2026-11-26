"""company_text's batch helpers in a child Python process of their own.

Stripping a batch's text and building company profiles are pure Python over
text. In a worker that also runs many request threads, they hold the
interpreter lock those threads need (tasks.derivations.local_embeddings
measured it). A child process does them instead. It is started as
`python -m core.derivations.text_process`, so it imports this module and
company_text and nothing else: multiprocessing's spawn would re-import the
worker's own main module, database pool included.

Requests and answers are length-prefixed JSON over the child's stdin and
stdout (byte strings and sets tagged, the only values the helpers pass that
JSON lacks), and the child runs only the helpers named in CALLS. When the child
is gone, or a helper fails in it, the call runs in this process instead.
The helpers are pure, so the answer is the same, only not off the lock.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import IO, Any

from core.derivations import company_text

logger = logging.getLogger(__name__)

CALLS: dict[str, Callable[[Any], Any]] = {
    "embedding_texts": company_text.embedding_texts,
    "shared_paragraphs_of": company_text.shared_paragraphs_of,
}

_LENGTH = struct.Struct("!Q")


def _tagged(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, (set, frozenset)):
        return {"set": list(value)}
    raise TypeError(f"{type(value).__name__} does not cross to the text process")


def _untagged(obj: dict[str, Any]) -> Any:
    if obj.keys() == {"bytes"}:
        return bytes.fromhex(obj["bytes"])
    if obj.keys() == {"set"}:
        return set(obj["set"])
    return obj


def _send(stream: IO[bytes], message: Any) -> None:
    body = json.dumps(message, default=_tagged).encode()
    stream.write(_LENGTH.pack(len(body)) + body)
    stream.flush()


def _receive(stream: IO[bytes]) -> Any:
    head = stream.read(_LENGTH.size)
    if len(head) < _LENGTH.size:
        raise EOFError("text process closed its pipe")
    (length,) = _LENGTH.unpack(head)
    body = stream.read(length)
    if len(body) < length:
        raise EOFError("text process closed its pipe mid-message")
    return json.loads(body, object_hook=_untagged)


class TextProcess:
    """A child that runs CALLS. One call at a time: `call` holds a lock for
    the round trip."""

    def __init__(self) -> None:
        # This interpreter running this module: nothing from outside. It
        # imports from where this process does, which a child does not
        # inherit when the parent's path was set in-process (pytest).
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
        self._child: subprocess.Popen[bytes] | None = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", __name__],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
        )
        self._lock = threading.Lock()

    def call(self, name: str, arg: Any) -> Any:
        with self._lock:
            child = self._child
            if child is not None:
                assert child.stdin is not None and child.stdout is not None
                try:
                    _send(child.stdin, (name, arg))
                    ok, answer = _receive(child.stdout)
                    if ok:
                        return answer
                    logger.warning("text process failed %s: %s; running it here", name, answer)
                except OSError, EOFError, ValueError:
                    logger.warning("text process is gone; running %s here", name, exc_info=True)
                    self._close(child)
                    self._child = None
        return CALLS[name](arg)

    @staticmethod
    def _close(child: subprocess.Popen[bytes]) -> None:
        try:
            if child.stdin is not None:
                child.stdin.close()
            child.wait(timeout=_CLOSE_SECONDS)
        except OSError, subprocess.TimeoutExpired:
            child.kill()
            child.wait()

    def close(self) -> None:
        with self._lock:
            if self._child is not None:
                self._close(self._child)
                self._child = None

    def __enter__(self) -> TextProcess:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# How long a child given end of input has to exit before it is killed: it
# finishes the call in hand, which is one batch's text, seconds at most.
_CLOSE_SECONDS = 30


def _serve(requests: IO[bytes], answers: IO[bytes]) -> None:
    while True:
        try:
            name, arg = _receive(requests)
        except EOFError:
            return
        try:
            _send(answers, (True, CALLS[name](arg)))
        except Exception as exc:
            _send(answers, (False, f"{type(exc).__name__}: {exc}"))


if __name__ == "__main__":
    _serve(sys.stdin.buffer, sys.stdout.buffer)
