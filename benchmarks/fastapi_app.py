"""A FastAPI equivalent of our own /users/{id} endpoint, for a fair,
apples-to-apples dynamic-endpoint benchmark comparison.

Same route, same path parameter, same JSON payload shape as
app.py's get_user handler -- the point is to compare the SERVER layer
(concurrency model, request handling overhead), not different application
logic.

Run with: uvicorn benchmarks.fastapi_app:app --host 127.0.0.1 --port 8090
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()

FAKE_USERS = {
    "1": {"id": "1", "name": "Ada Lovelace"},
    "2": {"id": "2", "name": "Alan Turing"},
}


@app.get("/users/{user_id}")
def get_user(user_id: str):
    user = FAKE_USERS.get(user_id)
    if user is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return user
