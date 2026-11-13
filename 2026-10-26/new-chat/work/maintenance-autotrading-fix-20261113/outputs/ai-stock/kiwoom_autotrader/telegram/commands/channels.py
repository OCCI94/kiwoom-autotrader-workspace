"""channels Telegram command handler."""

from kiwoom_autotrader.telegram.bot_credentials import TelegramConfig

def build_reply(command: str, config: TelegramConfig) -> str:
    from kiwoom_autotrader.telegram.channel_store import ChannelStore
    from kiwoom_autotrader.telegram.account_credentials import load_account_config
    from kiwoom_autotrader.utils.account_service import account_paths

    def safe(value, limit):
        return "".join(ch for ch in str(value) if ch in "\n\t" or ord(ch) >= 32)[:limit]

    try:
        account = load_account_config()
        if account is None:
            return "개인 계정 연결이 필요합니다. Mac에서 telegram-account-setup.command를 열어 계정과 수집할 채널을 연결하세요."
        if account.user_id != config.chat_id:
            return "개인 계정과 봇의 연결 사용자가 일치하지 않습니다. Mac에서 계정 연결을 확인하세요."
        with ChannelStore(account_paths().state, account.user_id) as store:
            if command == "channels":
                summary = store.summary()
                lines = [f"수집 채널 {summary['channels']}개 · 보관 글 {summary['messages']}개",
                         f"과거 수집 완료 {summary['history_complete']}개 · 진행 중 {summary['history_pending']}개"]
                lines.extend("• " + safe(c['title'], 80) for c in store.channels()[:20])
            else:
                rows = store.recent(limit=5)
                if not rows:
                    return "아직 수집된 채널 글이 없습니다. 계정 연결과 채널 선택을 확인하세요."
                lines = ["수집한 최근 채널 글 5개"]
                for row in rows:
                    lines.append("\n[" + safe(row['title'], 70) + "]\n" + (safe(row['text'], 350) or "미디어 게시물"))
                    if row.get('permalink'):
                        lines.append(row['permalink'])
            text = "\n".join(lines)
            if any(secret in text for secret in (account.api_hash, account.session, config.bot_token)):
                return "채널 내용에 연결 정보가 포함되어 표시하지 않았습니다."
            return text[:4096]
    except Exception:
        return "채널 수집 기록을 확인하지 못했습니다. Mac에서 개인 계정 연결 상태를 확인하세요."

