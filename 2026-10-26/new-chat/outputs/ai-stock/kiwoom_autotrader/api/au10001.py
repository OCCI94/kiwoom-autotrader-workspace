"""au10001: access-token request body; callers validate credentials and origin."""

API_ID = "au10001"
ENDPOINT = "/oauth2/token"


def request_body(app_key: str, app_secret: str) -> dict[str, str]:
    return {"grant_type": "client_credentials", "appkey": app_key, "secretkey": app_secret}
