from fastapi import APIRouter, Request, Response
from app.security import create_session, get_csrf_token_for_session, SESSION_COOKIE_NAME

router = APIRouter(tags=["session"])


@router.get("/api/session")
def get_session(request: Request, response: Response):
    existing = get_csrf_token_for_session(request.cookies.get(SESSION_COOKIE_NAME))
    if existing:
        return {"csrf_token": existing}
    session_id, csrf_token = create_session()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=False,  # loopback local server
        path="/",
    )
    return {"csrf_token": csrf_token}
