"""A minimal board reply for producer-shaped listing fixtures."""


class BoardResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body

    @property
    def text(self):
        return self.body
