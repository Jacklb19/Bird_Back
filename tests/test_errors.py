"""Error catalogue: each code keeps one status and the {"detail": ...} body the client relies on."""
import json

import pytest
from fastapi import HTTPException

from birdnet_api.errors import ErrorCode, error_response, raise_error
from tests.test_api import api  # noqa: F401  (fixture)


def test_codes_raise_and_render_their_status_and_message():
    for code in ErrorCode:
        assert 400 <= code.status < 600 and code.message
        with pytest.raises(HTTPException) as raised:
            raise_error(code)
        assert (raised.value.status_code, raised.value.detail) == (code.status, code.message)
        response = error_response(code)
        assert (response.status_code, json.loads(response.body)) == (code.status, {"detail": code.message})


def test_api_responses_keep_the_detail_shape(api):  # noqa: F811
    client, _, _, headers, _ = api
    area = {"west": -74.0, "south": 4.5, "east": -74.2, "north": 4.8}
    assert client.get("/v1/detections", params=area).json() == {"detail": ErrorCode.AUTH_REQUIRED.message}
    invalid = client.get("/v1/detections", params=area, headers=headers())
    assert (invalid.status_code, invalid.json()) == (422, {"detail": ErrorCode.INVALID_MAP_BOUNDS.message})
