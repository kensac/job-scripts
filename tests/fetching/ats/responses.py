"""A minimal resolver reply: a JSON body, a status and optional headers."""


class AtsResponse:
    def __init__(self, body=None, status_code=200, headers=None):
        self._body = body
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._body
