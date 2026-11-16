def ok(data: dict) -> dict:
    return {"ok": True, "data": data}


def fail(code: str, message: str, retry: str) -> dict:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "retry": retry},
    }
