"""Faz 9.3 — the learning map's narration is checked against the map it sits on.

The LLM is replaced at `ai_service._call_llm`, as in the claim-explanation and
report tests, so these check what goes into the prompt and what is done with
the answer -- never whether an explanation is *good*. The load-bearing
properties:

- the model sees the computed arrows and the topic, never more text than the
  graph carried (titles; no abstract unless one was passed, and then clipped);
- a narration whose id set differs from the map's is thrown away whole;
- the output language follows the active locale, i.e. the caller's
  `force_locale`.
"""

from __future__ import annotations

import pytest
from flask_babel import force_locale

from app.modules.scrape import ai_service
from app.modules.scrape.learning_map import learning_map_graph


def _graph():
    return {
        "root": "Graph neural networks",
        "insufficient": False,
        "work_count": 60,
        "params": {},
        "nodes": [
            {
                "id": "Graph theory",
                "name": "Graph theory",
                "count": 40,
                "layer": 0,
                "order": 0,
                "role": "prerequisite",
                "median_year": 2019,
                "growth": 0.0,
                "key_work": {
                    "title": "Spectral graph theory primer",
                    "doi": "10.1000/a",
                    "url": "https://example.com/a",
                    "cited_by_count": 900,
                    "in_library": False,
                },
            },
            {
                "id": "Message passing",
                "name": "Message passing",
                "count": 24,
                "layer": 1,
                "order": 1,
                "role": "core",
                "median_year": 2019,
                "growth": 0.1,
                "key_work": {
                    "title": "Neural message passing for chemistry",
                    "doi": None,
                    "url": "https://example.com/b",
                    "cited_by_count": 500,
                    "in_library": True,
                },
            },
            {
                "id": "GCN",
                "name": "GCN",
                "count": 10,
                "layer": 2,
                "order": 2,
                "role": "method",
                "median_year": 2018,
                "growth": None,
                "key_work": None,
            },
        ],
        # Citation-flow edge records (the shape 9.1's successor writes); the
        # narration must read only from/to.
        "edges": [
            {"from": "Graph theory", "to": "Message passing", "flow": 12, "reverse_flow": 2},
            {"from": "Message passing", "to": "GCN", "flow": 7, "reverse_flow": 1},
        ],
    }


def _answer(ids=("Graph theory", "Message passing", "GCN")):
    return {
        "nodes": [
            {
                "id": i,
                "label": f"{i} label",
                "explain": f"{i} explained.",
                "check_question": f"What is {i}?",
            }
            for i in ids
        ]
    }


@pytest.fixture
def llm(app, monkeypatch):
    """A fake provider: records (system, user_msg) and returns scripted answers.

    `answers` is consumed one per call; the last one repeats, so a test that
    sets a single bad answer also sees it on the repair retry.
    """
    state = {"calls": [], "answers": [_answer()]}

    def fake(*, system, user_msg, max_tokens, user=None, expect_json=True):
        state["calls"].append({"system": system, "user_msg": user_msg})
        answers = state["answers"]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        return (answer, "{}") if answer is not None else (None, None)

    monkeypatch.setattr(ai_service, "_call_llm", fake)
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: True)
    return state


def _narrate(graph=None, topic="graph neural networks", locale="tr"):
    with force_locale(locale):
        return ai_service.narrate_learning_map(graph or _graph(), topic=topic)


def test_happy_path_returns_cards_keyed_by_id(llm):
    out = _narrate()
    assert set(out["nodes"]) == {"Graph theory", "Message passing", "GCN"}
    assert out["nodes"]["GCN"] == {
        "label": "GCN label",
        "explain": "GCN explained.",
        "check_question": "What is GCN?",
    }
    assert len(llm["calls"]) == 1


def test_works_on_a_real_learning_map_graph(llm):
    """End to end with 9.1's output: whatever the edge record carries, only
    from/to reach the prompt."""
    # Citation flow (9.1b): B's works cite A's, C's cite B's, D's cite A's.
    works = []
    for concept, cites in (("A", None), ("B", "A"), ("C", "B"), ("D", "A")):
        for i in range(10):
            works.append(
                {
                    "external_id": f"W{concept}{i}",
                    "title": f"{concept} work {i}",
                    "categories": [concept],
                    "referenced_works": [f"W{cites}{i}"] if cites else [],
                }
            )
    graph = learning_map_graph(works, min_support=8)
    assert not graph["insufficient"]
    ids = [n["id"] for n in graph["nodes"]]
    llm["answers"] = [_answer(ids)]

    out = _narrate(graph)

    assert set(out["nodes"]) == set(ids)
    assert "A → B" in llm["calls"][0]["user_msg"]
    assert "reverse_flow" not in llm["calls"][0]["user_msg"]


def test_prompt_carries_topic_arrows_and_titles(llm):
    _narrate(topic="graph neural networks")
    msg = llm["calls"][0]["user_msg"]
    assert "graph neural networks" in msg
    assert "Graph theory → Message passing" in msg
    assert "Message passing → GCN" in msg
    assert "Spectral graph theory primer" in msg
    assert "prerequisite" in msg
    # Edge weights are the numbers' business, not the narration's.
    assert "reverse_flow" not in msg
    assert "abstract" not in msg


def test_abstract_is_sent_only_when_given_and_then_clipped(llm):
    graph = _graph()
    tail = "UNIQUE_TAIL_MARKER"
    graph["nodes"][0]["key_work"]["abstract"] = "word " * 400 + tail
    _narrate(graph)
    msg = llm["calls"][0]["user_msg"]
    assert "key_work_abstract" in msg
    assert tail not in msg
    # Only the node that carried an abstract sends one.
    assert msg.count("key_work_abstract") == 1


def test_arrows_to_unknown_nodes_are_not_sent(llm):
    graph = _graph()
    graph["edges"].append({"from": "GCN", "to": "Not a node"})
    _narrate(graph)
    assert "Not a node" not in llm["calls"][0]["user_msg"]


def test_missing_node_discards_the_narration(llm):
    llm["answers"] = [_answer(("Graph theory", "Message passing"))]
    assert _narrate() is None


def test_extra_node_discards_the_narration(llm):
    llm["answers"] = [_answer(("Graph theory", "Message passing", "GCN", "Invented"))]
    assert _narrate() is None


def test_duplicated_node_discards_the_narration(llm):
    llm["answers"] = [_answer(("Graph theory", "Message passing", "GCN", "GCN"))]
    assert _narrate() is None


def test_ids_are_matched_exactly(llm):
    llm["answers"] = [_answer(("graph theory", "Message passing", "GCN"))]
    assert _narrate() is None


@pytest.mark.parametrize(
    "answer",
    [
        None,  # unparseable JSON: _call_llm returns (None, None)
        ["not", "an", "object"],
        "a scalar",
        {"nodes": "not a list"},
        {"nodes": {"Graph theory": {}}},
        {"cards": []},
        {"nodes": ["Graph theory", "Message passing", "GCN"]},
    ],
)
def test_malformed_answers_return_none(llm, answer):
    llm["answers"] = [answer]
    assert _narrate() is None


def test_non_object_answer_gets_one_repair_retry(llm):
    llm["answers"] = [None, _answer()]
    out = _narrate()
    assert out is not None
    assert len(llm["calls"]) == 2
    assert "JSON NESNESİ" in llm["calls"][1]["user_msg"]


def test_long_fields_are_clipped_and_empty_ones_become_blank(llm):
    answer = _answer()
    answer["nodes"][0]["label"] = "An extremely long plain language label " * 3
    answer["nodes"][0]["explain"] = "Sentence. " * 200
    answer["nodes"][0]["check_question"] = "Why " * 200 + "?"
    answer["nodes"][1]["label"] = "   "
    answer["nodes"][1].pop("explain")
    answer["nodes"][2]["check_question"] = None
    llm["answers"] = [answer]

    out = _narrate()["nodes"]

    gt = out["Graph theory"]
    assert len(gt["label"]) <= ai_service.LEARNING_MAP_LABEL_CHARS
    assert len(gt["explain"]) <= ai_service.LEARNING_MAP_EXPLAIN_CHARS
    assert len(gt["check_question"]) <= ai_service.LEARNING_MAP_QUESTION_CHARS
    assert gt["label"].endswith("…")
    assert out["Message passing"]["label"] == ""
    assert out["Message passing"]["explain"] == ""
    assert out["GCN"]["check_question"] == ""


@pytest.mark.parametrize(
    "graph",
    [
        {"insufficient": True, "nodes": [], "edges": []},
        {"insufficient": False, "nodes": [], "edges": []},
        {"insufficient": False, "nodes": [{"name": "no id"}], "edges": []},
        None,
    ],
)
def test_insufficient_graph_makes_no_call(llm, graph):
    with force_locale("tr"):
        assert ai_service.narrate_learning_map(graph, topic="x") is None
    assert llm["calls"] == []


def test_ai_disabled_returns_none_without_a_call(llm, monkeypatch):
    monkeypatch.setattr(ai_service, "is_ai_enabled", lambda user=None: False)
    assert _narrate() is None
    assert llm["calls"] == []


def test_ai_disabled_by_config_returns_none(app, monkeypatch):
    """Without the `llm` fixture: no key resolvable, so the real
    `is_ai_enabled` says no and nothing reaches a provider."""

    def boom(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("LLM called with AI disabled")

    monkeypatch.setattr(ai_service, "_call_llm", boom)
    monkeypatch.setitem(app.config, "LLM_PROVIDER", "openrouter")
    monkeypatch.setitem(app.config, "OPENROUTER_API_KEY", "")
    assert _narrate() is None


@pytest.mark.parametrize(
    ("locale", "expected", "absent"),
    [("en", "English", "DAİMA Türkçe"), ("tr", "DAİMA Türkçe", "English")],
)
def test_prompt_language_follows_the_active_locale(llm, locale, expected, absent):
    _narrate(locale=locale)
    system = llm["calls"][0]["system"]
    assert expected in system
    assert absent not in system


def test_unshipped_locale_falls_back_to_turkish(llm):
    _narrate(locale="de")
    assert "DAİMA Türkçe" in llm["calls"][0]["system"]


def test_system_prompt_formats_cleanly(llm):
    """The schema's braces are doubled for `str.format`; a slip would either
    raise or leave a literal placeholder in the prompt."""
    _narrate()
    system = llm["calls"][0]["system"]
    assert "{language}" not in system
    assert '"nodes": [' in system
    assert "check_question" in system
