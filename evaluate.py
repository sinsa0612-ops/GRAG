# BenchmarkQED 스타일 경량 품질 평가 — 질문 자동생성(AutoQ)과 LLM 페어와이즈 심판(AutoE)으로
# 두 설정(주로 두 컬렉션)의 '답변 품질'을 상대 비교한다. 추출 '수'가 아니라 실제 Q&A로 판정하기 위함.
import json
import logging

from adapters.llm_adapter import generate
from query import answer_question

logger = logging.getLogger(__name__)

_QUESTION_PROMPT = """\
아래는 어떤 자료의 발췌야. 이 내용만으로 답할 수 있는, 서로 다른 질문 {n}개를 한국어로 만들어.
- 절반은 구체적 사실 질문(누가/무엇을/언제), 절반은 요약·종합 질문으로.
- 각 질문은 한 문장. 번호·기호·설명 없이.
아래처럼 순수 JSON 문자열 배열 하나만 출력(코드펜스·설명·앞뒤 문장 금지):
["질문1", "질문2"]

[발췌]
{sample}
"""

_JUDGE_PROMPT = """\
질문에 대한 두 답변을 비교해 더 나은 쪽을 골라.
판정 기준(가중치 순): ①충실성(주어진 근거에 충실하고 없는 내용을 지어내지 않음) ②포괄성(중요한 점을 빠짐없이) ③관련성.
"둘 다 정보가 부족하다"고만 답한 경우엔 더 구체적으로 답한 쪽을 우대해.
아래 JSON만 출력(코드펜스·설명·앞뒤 문장 금지): {{"winner": "1" 또는 "2" 또는 "tie", "reason": "한 줄 근거"}}

[질문]
{question}

[답변 1]
{answer1}

[답변 2]
{answer2}
"""


# 발췌 텍스트로 평가용 질문 n개를 생성한다(AutoQ). JSON 배열이 깨지면 줄 단위로 폴백 파싱한다.
def generate_questions(sample: str, n: int = 8, model: str | None = None) -> list[str]:
    raw = generate(_QUESTION_PROMPT.format(n=n, sample=sample[:6000]), model=model)
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(cleaned)
        questions = [q.strip() for q in data if isinstance(q, str) and q.strip()]
    except (json.JSONDecodeError, TypeError):
        questions = [
            line.strip().lstrip("-•*0123456789. ").strip()
            for line in cleaned.splitlines()
            if line.strip()
        ]
    return [q for q in questions if q][:n]


# 한 질문에 대한 두 답변을 한 번 심판한다. 반환은 ("1"/"2"/"tie", 한 줄 근거).
def _judge_once(question: str, answer1: str, answer2: str, model: str | None = None) -> tuple[str, str]:
    raw = generate(
        _JUDGE_PROMPT.format(question=question, answer1=answer1, answer2=answer2), model=model
    )
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    try:
        verdict = json.loads(cleaned)
        winner = str(verdict.get("winner", "tie")).strip().lower()
        reason = str(verdict.get("reason", ""))
    except json.JSONDecodeError:
        low = cleaned.lower()
        winner = "1" if '"1"' in cleaned else "2" if '"2"' in cleaned else "tie"
        reason = "판정 파싱 실패" if winner == "tie" else cleaned[:80]
    if winner not in ("1", "2"):
        winner = "tie"
    return winner, reason


# 순서를 뒤집어 두 번 심판하고, 두 결과가 일치할 때만 승자로 인정한다(위치 편향 제거, 불일치=무승부).
def judge_pairwise(question: str, answer_a: str, answer_b: str, model: str | None = None) -> dict:
    w1, r1 = _judge_once(question, answer_a, answer_b, model)  # A가 1번
    w2, r2 = _judge_once(question, answer_b, answer_a, model)  # A가 2번
    first = "A" if w1 == "1" else "B" if w1 == "2" else "tie"
    second = "A" if w2 == "2" else "B" if w2 == "1" else "tie"
    if first == second and first != "tie":
        return {"winner": first, "reason": r1 or r2}
    if first != second and "tie" not in (first, second):
        return {"winner": "tie", "reason": f"순서에 따라 승자가 뒤바뀜({first}/{second})"}
    return {"winner": "tie", "reason": r1 or r2 or "무승부"}


# 두 컬렉션을 같은 질문들로 답하게 하고 페어와이즈 심판으로 승패를 집계한다(순차 — DB 동시접근 회피).
def compare_collections(
    coll_a: str, coll_b: str, questions: list[str], judge_model: str | None = None
) -> dict:
    results: list[dict] = []
    for question in questions:
        answer_a = answer_question(question, collections=[coll_a])
        answer_b = answer_question(question, collections=[coll_b])
        verdict = judge_pairwise(question, answer_a, answer_b, model=judge_model)
        results.append(
            {"question": question, "answer_a": answer_a, "answer_b": answer_b, **verdict}
        )
    wins = {"A": 0, "B": 0, "tie": 0}
    for result in results:
        wins[result["winner"]] += 1
    return {"coll_a": coll_a, "coll_b": coll_b, "wins": wins, "results": results}
