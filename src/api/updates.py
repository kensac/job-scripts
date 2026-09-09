from typing import Annotated, Any

from pydantic import BeforeValidator
from pydantic.json_schema import SkipJsonSchema


def _reject_null(value: Any) -> Any:
    if value is None:
        raise ValueError("field may be omitted but must not be null")
    return value


# None is the internal default for omitted update fields. Defaults are not
# validated; a supplied null is rejected and excluded from the request schema.
type NonNullUpdate[T] = Annotated[T | SkipJsonSchema[None], BeforeValidator(_reject_null)]
