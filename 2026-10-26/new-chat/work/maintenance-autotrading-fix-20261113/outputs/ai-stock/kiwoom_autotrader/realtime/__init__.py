"""Features obtain the one listener-owned engine instead of opening sockets."""
from .engine import SharedWebSocketEngine, Subscription, SubscriptionHandle, WebSocketEngineError


def get_websocket_engine(session):
    engine = getattr(session, "websocket_engine", None)
    if engine is None:
        raise WebSocketEngineError("프로그램의 공용 WebSocket 엔진이 초기화되지 않았습니다.")
    return engine
