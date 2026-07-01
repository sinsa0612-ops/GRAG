# 품질평가 하네스(AutoQ/AutoE)가 질문 파싱·위치편향 제거·집계를 올바르게 하는지 확인한다.
# 외부 LLM(generate)과 answer_question은 mock으로 차단해 네트워크 없이 빠르게 검증한다.
import evaluate


def test_generate_questions_parses_json_array(monkeypatch):
    monkeypatch.setattr(evaluate, "generate", lambda prompt, **kwargs: '["Q1?", "Q2?", "Q3?"]')
    assert evaluate.generate_questions("sample", n=2) == ["Q1?", "Q2?"]


def test_generate_questions_line_fallback(monkeypatch):
    monkeypatch.setattr(evaluate, "generate", lambda prompt, **kwargs: "- 첫 질문?\n- 둘째 질문?")
    assert evaluate.generate_questions("s", n=5) == ["첫 질문?", "둘째 질문?"]


def test_judge_pairwise_consistent_winner(monkeypatch):
    # 순서를 바꿔도 둘 다 A를 고르면(1번호출 winner=1, 2번호출 winner=2) 최종 승자는 A.
    verdicts = iter(['{"winner":"1","reason":"A가 더 충실"}', '{"winner":"2","reason":"A가 더 충실"}'])
    monkeypatch.setattr(evaluate, "generate", lambda prompt, **kwargs: next(verdicts))
    assert evaluate.judge_pairwise("q", "answerA", "answerB")["winner"] == "A"


def test_judge_pairwise_position_flip_is_tie(monkeypatch):
    # 두 순서에서 모두 '1번'을 골라 승자가 순서에 따라 뒤바뀌면 무승부로 처리.
    monkeypatch.setattr(evaluate, "generate", lambda prompt, **kwargs: '{"winner":"1","reason":"앞이 좋음"}')
    assert evaluate.judge_pairwise("q", "answerA", "answerB")["winner"] == "tie"


def test_judge_pairwise_handles_malformed_judge(monkeypatch):
    monkeypatch.setattr(evaluate, "generate", lambda prompt, **kwargs: "JSON 아님")
    assert evaluate.judge_pairwise("q", "a", "b")["winner"] == "tie"


def test_compare_collections_aggregates(monkeypatch):
    monkeypatch.setattr(evaluate, "answer_question", lambda q, collections=None: f"ans-{collections[0]}")
    monkeypatch.setattr(evaluate, "judge_pairwise", lambda q, a, b, model=None: {"winner": "A", "reason": "x"})
    report = evaluate.compare_collections("cA", "cB", ["q1", "q2"])
    assert report["wins"] == {"A": 2, "B": 0, "tie": 0}
    assert report["results"][0]["answer_a"] == "ans-cA"
