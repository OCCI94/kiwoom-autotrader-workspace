"""A self-contained, script-free Korean view of an existing audit report."""
from html import escape

from .models import AuditReport

_LABELS = {"PASS": "확인", "NOT_MET": "불일치", "OBSERVE": "관찰 기록", "UNKNOWN": "미확인", "UNSUPPORTED": "미지원"}


def render_html(report: AuditReport) -> str:
    data = report.to_dict()
    rows = []
    for item in data["checks"]:
        statistics = " · ".join(f"{key}: {value}" for key, value in item["statistics"].items()) or "통계 없음"
        rows.append("<tr><td>" + escape(item["name"]) + "</td><td>"
                    + escape(_LABELS[item["verdict"]] + " (" + item["verdict"] + ")")
                    + "</td><td>" + escape(item["reason_code"]) + "</td><td>" + escape(statistics) + "</td></tr>")
    sources = "".join("<li>" + escape(item["kind"]) + " · " + str(item["bytes"])
                      + " bytes · SHA-256 <code>" + escape(item["sha256"]) + "</code></li>"
                      for item in data["sources"]) or "<li>읽을 수 있는 자료가 없습니다.</li>"
    return """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>ai-stock 오프라인 기록 감사</title>
<style>body{font-family:system-ui,sans-serif;max-width:1150px;margin:40px auto;padding:0 24px;color:#17212b;background:#f7f8fa;line-height:1.6}h1{font-size:1.8rem}.notice{padding:16px 20px;background:#fff4d6;border:1px solid #e6c775;border-radius:8px}.table{overflow-x:auto}table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #dbe1e6;padding:12px;text-align:left;vertical-align:top}th{background:#eaf0f5}code{overflow-wrap:anywhere}li{margin:12px 0}small{color:#52606d}</style>
</head><body><h1>ai-stock 오프라인 기록 감사</h1>
<p class="notice"><strong>현재 매매 준비 상태: 미확인 (UNKNOWN)</strong><br>
저장된 파일만 검사했습니다. 현재 계좌·미체결 주문·시장·프로세스의 실제 상태를 확인하지 않았습니다.
기록 검사 PASS는 매매 준비 완료를 뜻하지 않습니다. 자료 없음·읽기 실패를 정상 무체결로 간주하지 않습니다.</p>
<p>보고서 생성 시각: """ + escape(data["generated_at"]) + """</p>
<div class="table"><table><thead><tr><th>검사</th><th>판정</th><th>사유코드</th><th>고정 통계</th></tr></thead><tbody>""" + "".join(rows) + """</tbody></table></div>
<h2>검사한 자료의 출처</h2><ul>""" + sources + """</ul>
<p><small>이 화면은 저장된 감사 결과입니다. 새 조회·자동 갱신·외부 리소스·스크립트·주문 기능이 없습니다.
종목·계좌·주문 원문과 키·토큰은 표시하지 않습니다.</small></p></body></html>
"""
