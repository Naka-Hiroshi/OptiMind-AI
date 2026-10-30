"""The local API's routes. Every router makes its routes with LocalizedRoute, so an answer goes out
as the route made it and its texts reach the response that translates them (app/main.py)."""
from fastapi.datastructures import Default, DefaultPlaceholder
from fastapi.routing import APIRoute


class LocalizedRoute(APIRoute):
    """A route without the response model FastAPI infers from its return annotation (-> dict): that
    model would turn each text into its English before the answer is translated. A route that names
    its model keeps it (its texts are stored as "<field>_i18n" there, app/models.py)."""

    def __init__(self, path, endpoint, *, response_model=Default(None), **kwargs) -> None:
        if isinstance(response_model, DefaultPlaceholder):
            response_model = None
        super().__init__(path, endpoint, response_model=response_model, **kwargs)
