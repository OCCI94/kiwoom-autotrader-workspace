"""Expand one ranking snapshot into bounded existing stock commands."""
from dataclasses import dataclass
from kiwoom_autotrader.api.errors import BrokerAPIError
import re

from kiwoom_autotrader.api.rankings import fetch_ranked_stocks
from .rank import handle_rank
from .orders import parse_buy_command, parse_sell_command, MANUAL_BUY_CAP_KRW
from .search import parse_search_command
from .crosses import parse_cross_command

BATCH_USAGE = ("사용법: rank {1~4} cmd {1~20} {종목 명령}\n"
               "예: rank 4 cmd 15 gdcrs add ( ) 100000\n"
               "( ) 또는 () 한 곳에 종목코드가 들어갑니다.\n"
               "지원: srch, buy, sell, gdcrs add. 조회된 순서대로 한 번씩 실행합니다.\n"
               "_NX/_AL 접미사는 제거하여 기존 명령의 KRX 종목코드를 사용합니다.")
_PLACEHOLDER = re.compile(r"\(\s*\)")


@dataclass(frozen=True)
class RankBatch:
    number: int
    count: int
    template: str

    def command(self, code):
        if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
            raise ValueError(BATCH_USAGE)
        return _PLACEHOLDER.sub(code, self.template)


def parse_rank_batch(text):
    if not isinstance(text, str):
        return None
    words = text.strip().split(maxsplit=4)
    if not words or words[0].casefold() != "rank" or len(words) < 3:
        return None
    if words[2].casefold() != "cmd":
        return None
    if (len(words) != 5 or not text.strip().isprintable() or len(text.strip()) > 500
            or words[1] not in {"1", "2", "3", "4"}
            or re.fullmatch(r"[0-9]{1,2}", words[3]) is None
            or not 1 <= int(words[3]) <= 20
            or len(_PLACEHOLDER.findall(words[4])) != 1):
        raise ValueError(BATCH_USAGE)
    result = RankBatch(int(words[1]), int(words[3]), words[4])
    for code in ("005930", "000660"):
        example = result.command(code)
        prefix = example.split(maxsplit=1)[0].casefold()
        try:
            if prefix == "buy":
                parsed = parse_buy_command(example)
                if parsed.identifier != code:
                    raise ValueError
            elif prefix == "sell":
                parsed = parse_sell_command(example)
                if parsed.identifier != code:
                    raise ValueError
            elif prefix == "srch":
                if example.split() != [example.split()[0], code]:
                    raise ValueError
                parse_search_command(example)
            elif prefix == "gdcrs":
                parsed = parse_cross_command(example)
                if parsed.action != "add" or parsed.code != code:
                    raise ValueError
            else:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise ValueError(BATCH_USAGE) from None
    return result


def handle_rank_batch(text, session, dispatch, *, should_stop=lambda: False):
    try:
        batch = parse_rank_batch(text)
    except ValueError:
        return [BATCH_USAGE]
    if batch is None:
        return handle_rank(text, session)
    # Check the requested maximum batch before any ranking query or dispatch.
    example = parse_buy_command(batch.command("005930"))
    if example is not None:
        if example.budget is not None:
            principal = int(example.budget)
        elif example.price is not None:
            principal = int(example.quantity) * int(example.price)
        else:
            return ["일괄 시장가 매수는 합산 원금이 미확정입니다. 지정가 또는 max 예산을 지정하세요."]
        if principal * batch.count > MANUAL_BUY_CAP_KRW:
            return ["일괄 매수의 요청 합산 원금이 100만원 한도를 초과해 실행하지 않았습니다."]
    identity = (session.mode, session.token)
    try:
        stocks = fetch_ranked_stocks(session, batch.number, batch.count)
        # Validate the full snapshot before dispatching any side effects.
        codes = []
        for row in stocks:
            match = re.fullmatch(r"([0-9]{6})(?:_NX|_AL)?", row.code)
            if match is None:
                raise ValueError
            # Existing stock commands operate on the same security at KRX.
            code = match[1]
            if code not in codes:
                codes.append(code)
        codes = codes[:batch.count]
        commands = [batch.command(code) for code in codes]
    except BrokerAPIError as error:
        return [str(error)] if error.ip_related else ["순위 조회에 실패했습니다. 일괄 명령을 실행하지 않았습니다."]
    except Exception:
        return ["순위 조회에 실패했습니다. 일괄 명령을 실행하지 않았습니다."]
    if not commands:
        return ["조회된 종목이 없어 일괄 명령을 실행하지 않았습니다."]
    replies = [f"📋 rank {batch.number} 조회 결과 {len(commands)}종목에 명령을 실행합니다."]
    for index, (code, command) in enumerate(zip(codes, commands), 1):
        if should_stop() or (session.mode, session.token) != identity:
            replies.append("일괄 실행을 중단했습니다. 완료된 명령은 다시 실행하지 않습니다.")
            break
        try:
            result = dispatch(command)
        except Exception:
            replies.append(f"⚠️ {index}. {code}: 실행 결과를 확인하지 못해 이후 실행을 중단했습니다. 자동 재시도하지 않습니다.")
            break
        replies.extend(f"{index}. {code}\n{message}" for message in result)
        if getattr(result, "uncertain", False):
            replies.append("주문 결과가 불확실하여 남은 일괄 실행을 중단했습니다. 주문내역을 확인하세요.")
            break
    return replies
