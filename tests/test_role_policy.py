import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention, launch

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.npc.review import REVIEW_SYSTEM
from chatbot.observations import append_record
from chatbot.provenance import source_class
from chatbot.role_memory import RoleMemoryService
from chatbot.role_policy import (
    citation_repair_sources,
    claim_issues,
    invalid_claims,
    invalid_recalled_quotes,
    invalid_statement_promotions,
    needs_knowledge,
    review_grounding,
    semantic_review_marks,
    unexecuted_stage_object_action,
)
from chatbot.storage import Conversation, RoleMemory, SceneRecord, Task


def test_reviewer_distinguishes_current_self_commitment_from_prior_mutual_agreement():
    assert "候选台词本身可以产生这个角色当下的新答复或单方承诺" in REVIEW_SYSTEM
    assert "不得仅因此标记agreement_promotion" in REVIEW_SYSTEM


def test_object_mutation_cannot_hide_in_free_stage_performance():
    assert unexecuted_stage_object_action("（阿岚把烟按灭）我会先等消息。")
    assert unexecuted_stage_object_action("（她将便条递给沈舟）你先看看。")
    assert unexecuted_stage_object_action("(他把箱子打开)里面有什么？")
    assert unexecuted_stage_object_action("（放下手里的书，愣了一下）你还认得我？")
    assert unexecuted_stage_object_action("（按灭手里的烟）我会等消息。")
    assert unexecuted_stage_object_action("（手里的价签捏皱了一角）我听见了。")
    assert unexecuted_stage_object_action("（把价签揉皱）我听见了。")
    assert not unexecuted_stage_object_action("（阿岚点头，沉默片刻）我会先等消息。")
    assert not unexecuted_stage_object_action("（她把手放下，放低声音）我同意。")
    assert not unexecuted_stage_object_action("（放下手里的活儿，抬头看他）请说。")
    assert not unexecuted_stage_object_action("我提议稍后把便条递给沈舟。")


def test_semantic_diagnostics_preserve_rejection_and_never_record_free_text():
    check = {
        "supported": False,
        "reason_code": "acquisition_mismatch",
        "text": "私有台词",
        "basis": "私有依据",
    }
    review = {"accepted": True, "checks": [check], "issues": []}
    assert semantic_review_marks(review) == {
        "semantic_rejected",
        "unsupported_check",
        "reason_acquisition_mismatch",
    }
    check["reason_code"] = "任意模型正文"
    assert semantic_review_marks(review) == {"semantic_rejected", "unsupported_check", "reason_unclassified"}
    assert semantic_review_marks({"accepted": True, "checks": [], "issues": ["其他私有问题"]}) == {
        "semantic_rejected",
        "review_has_issues",
    }
    assert semantic_review_marks({"accepted": False, "checks": [], "issues": []}) == {
        "semantic_rejected",
        "review_flag_rejected",
    }
    assert semantic_review_marks({"accepted": True, "checks": [], "issues": []}) == {"semantic_accepted"}


def test_claim_diagnostics_report_all_causes_without_source_contents():
    claim = {"text": "秘密在箱中", "source_id": "heard", "quote": "不存在的原句", "mode": "fact"}
    speech = {"text": "有人说了另一句话", "claims": [claim]}
    sources = {"heard": "只听到半句"}
    observations = [{"id": "heard", "kind": "speech"}]
    issues = claim_issues(speech, sources, observations, "")
    assert {i["code"] for i in issues} == {"quote_mismatch", "claim_text_mismatch", "statement_promoted"}
    assert all(i["claim_index"] == 0 for i in issues)
    assert "秘密" not in str(issues) and "heard" not in str(issues)
    assert citation_repair_sources(speech, sources, issues) == {
        "heard": {"text": "只听到半句", "excerpt_truncated": False}
    }
    assert citation_repair_sources(speech, {}, issues) == {}
    clipped = citation_repair_sources(speech, {"heard": "字" * 5000}, issues)["heard"]
    assert len(clipped["text"]) == 4000 and clipped["excerpt_truncated"]
    # Preserve the old rejection boundary across source, quote, mode and literal-text changes.
    for source_id in ("heard", "missing"):
        for quote in ("", "只听到半句", "错误"):
            for mode in ("fact", "reported"):
                for text in (speech["text"], "不同正文"):
                    speech["claims"] = [
                        {**claim, "source_id": source_id, "quote": quote, "mode": mode, "text": text}
                    ]
                    old = invalid_claims(speech, sources) or invalid_statement_promotions(
                        speech, observations
                    )
                    assert bool(claim_issues(speech, sources, observations, "")) == bool(old)
    assert claim_issues({"text": "你说“另一句话”", "claims": []}, sources, observations, "原话") == [
        {"code": "recalled_quote_mismatch"}
    ]


def test_conditional_notice_cannot_ground_asserted_nonreceipt():
    conditional = "遇强风停航，复航时间由值班员收到通知后公布。"
    speech = {
        "text": "通知还没到，我不会报时间。",
        "claims": [
            {
                "text": "通知还没到",
                "source_id": "notice",
                "quote": "收到通知后公布",
                "mode": "fact",
            }
        ],
    }
    sources = {"notice": conditional}
    assert claim_issues(speech, sources, [], "") == [
        {"claim_index": 0, "code": "notice_receipt_not_in_quote"}
    ]
    speech["text"] = "我还没收到任何复航通知。"
    speech["claims"] = []
    assert claim_issues(speech, sources, [], "") == [{"code": "notice_receipt_unclaimed"}]
    speech["text"] = "复航通知目前没到我这里，我不知道几点能走。"
    assert claim_issues(speech, sources, [], "") == [{"code": "notice_receipt_unclaimed"}]
    speech["text"] = "现在通知没下来，我不会提前公布复航时间。"
    assert claim_issues(speech, sources, [], "") == [{"code": "notice_receipt_unclaimed"}]
    speech["text"] = "复航消息尚未下达，我不会提前公布时间。"
    assert claim_issues(speech, sources, [], "") == [{"code": "notice_receipt_unclaimed"}]
    speech["text"] = "复航通知目前没到我这里，我不知道几点能走。"
    speech["claims"] = [
        {
            "text": "复航通知目前没到我这里",
            "source_id": "notice",
            "quote": "收到通知后公布",
            "mode": "fact",
        }
    ]
    assert claim_issues(speech, sources, [], "") == [
        {"claim_index": 0, "code": "notice_receipt_not_in_quote"}
    ]
    speech["text"] = "现在通知没下来，我不会提前公布复航时间。"
    speech["claims"][0]["text"] = "现在通知没下来"
    assert claim_issues(speech, sources, [], "") == [
        {"claim_index": 0, "code": "notice_receipt_not_in_quote"}
    ]
    speech["claims"] = []
    speech["text"] = "我暂时不知道有没有新通知。"
    speech["claims"] = []
    assert claim_issues(speech, sources, [], "") == []
    speech["text"] = "我没法确认通知到没到。"
    assert claim_issues(speech, sources, [], "") == []
    confirmed = "值班员刚确认：通知还没到。"
    speech["text"] = "通知还没到。"
    speech["claims"] = [
        {"text": "通知还没到", "source_id": "confirmed", "quote": "通知还没到", "mode": "fact"}
    ]
    assert claim_issues(speech, {"confirmed": confirmed}, [], "") == []


def test_elliptical_resumption_time_receipt_still_needs_actual_receipt_evidence():
    text = "复航的时间我也没接到，不替谁定。"
    conditional = "遇强风停航，复航时间由值班员收到通知后公布。"
    assert claim_issues({"text": text, "claims": []}, {"notice": conditional}, [], "") == [
        {"code": "notice_receipt_unclaimed"}
    ]
    claim = {"text": text, "source_id": "notice", "quote": conditional, "mode": "fact"}
    assert claim_issues({"text": text, "claims": [claim]}, {"notice": conditional}, [], "") == [
        {"claim_index": 0, "code": "notice_receipt_not_in_quote"}
    ]
    for alternative in (
        "复航时间，我没接到。",
        "如果通知还没到，我就先等等。",
        "我不知道复航时间，不替谁定。",
        "若是没收到通知，我们可以再讨论。",
    ):
        expected = [{"code": "notice_receipt_unclaimed"}] if alternative.startswith("复航时间") else []
        assert claim_issues({"text": alternative, "claims": []}, {}, [], "") == expected
    conditional_claim = {
        "text": "通知还没到",
        "source_id": "if",
        "quote": "如果通知还没到，先等。",
        "mode": "fact",
    }
    assert claim_issues(
        {"text": "通知还没到", "claims": [conditional_claim]}, {"if": conditional_claim["quote"]}, [], ""
    ) == [{"claim_index": 0, "code": "notice_receipt_not_in_quote"}]
    confirmed = {**claim, "source_id": "actual", "quote": text}
    assert claim_issues({"text": text, "claims": [confirmed]}, {"actual": text}, [], "") == []


def test_subjective_or_unconfirmed_host_record_cannot_authorize_fact():
    sources = [
        {"id": "belief", "kind": "memory_change", "detail": {"mode": "fact"}},
        {"id": "rumor", "kind": "host", "detail": {"basis": "reported"}},
        {"id": "confirmed", "kind": "host", "detail": {"basis": "confirmed"}},
    ]
    speech = {"claims": [{"source_id": s["id"], "mode": "fact"} for s in sources]}
    assert [c["source_id"] for c in invalid_statement_promotions(speech, sources)] == ["belief", "rumor"]
    assert source_class(sources[0]) == "subjective_state"
    assert (
        invalid_statement_promotions({"claims": [{"source_id": "belief", "mode": "belief"}]}, sources) == []
    )


def test_runtime_scene_change_can_ground_arrival_without_promoting_speech():
    arrival = {"id": "arrival", "kind": "scene_change", "detail": {"from": "阅览室", "to": "门厅"}}
    speech = {
        "text": "我从阅览室来到门厅。",
        "claims": [
            {
                "source_id": "arrival",
                "mode": "fact",
                "text": "我从阅览室来到门厅。",
                "quote": "林岚从阅览室来到门厅。",
            }
        ],
    }
    assert source_class(arrival) == "confirmed_event"
    assert (
        claim_issues(
            speech,
            {"arrival": "林岚从阅览室来到门厅。"},
            [arrival],
            "",
        )
        == []
    )
    assert invalid_statement_promotions(
        {"claims": [{"source_id": "said", "mode": "fact"}]},
        [{"id": "said", "kind": "speech"}],
    )


def test_inspection_never_skips_lookup_and_legacy_intents_are_conservative():
    assert needs_knowledge({})
    assert needs_knowledge({"act": "inspect", "retrieval": "context"})
    assert not needs_knowledge({"act": "speak", "retrieval": "context"})


def test_statement_cannot_prove_its_contents_but_can_prove_it_was_said():
    claim = {"source_id": "old", "mode": "fact", "text": "全程无人接触", "quote": "全程无人接触"}
    speech = {"claims": [claim]}
    observations = [{"id": "old", "kind": "speech"}]
    assert invalid_statement_promotions(speech, observations)
    claim["mode"] = "reported"
    assert not invalid_statement_promotions(speech, observations)


def test_reviewer_does_not_inherit_unrelated_old_assertions_or_summary():
    info = {
        "profile": {"name": "陈墨"},
        "scene_facts": {"location": "铁盒"},
        "observations": [{"id": "old", "kind": "speech", "text": "全程无人接触"}],
        "historical_summary": "全程无人接触",
    }
    packet = review_grounding(info, [], {"claims": []}, {"old": "全程无人接触"})
    assert "全程无人接触" not in str(packet)


def test_review_packet_does_not_repeat_document_or_nested_old_claims():
    text = "原文依据" * 100
    info = {
        "profile": {},
        "observations": [{"id": "old", "kind": "speech", "detail": {"claims": ["巨大旧引文" * 1000]}}],
    }
    packet = review_grounding(
        info,
        [{"source": "S1", "text": text}],
        {"claims": [{"source_id": "S1"}, {"source_id": "old"}, {"source_id": "old"}]},
        {"S1": text, "old": "我曾经说过"},
    )
    assert len(packet["referenced_records_with_provenance"]) == 2
    assert packet["referenced_records_with_provenance"][0]["already_in_primary"] == {"document": "S1"}
    assert "巨大旧引文" not in str(packet)


def test_review_preserves_scoped_hearing_without_promoting_contents():
    observed = dict(id="s", kind="speech", actor_id="a", speaker="林岚", text="我见过钥匙")
    info = {"profile": {}, "observations": [observed]}
    speech = {"claims": [{"source_id": "s", "mode": "fact"}]}
    packet = review_grounding(info, [], speech, {"s": observed["text"]})
    ref = packet["referenced_records_with_provenance"][0]
    assert ref["utterance_observation"] == {
        "acquisition": "observed_utterance",
        "speaker_known": True,
        "actor_id": "a",
        "speaker": "林岚",
        "complete": True,
        "contents_verified": False,
    }
    assert invalid_statement_promotions(speech, [observed])


def test_anonymous_partial_hearing_cannot_recover_identity_or_hidden_suffix():
    observed = dict(
        id="s",
        kind="speech",
        actor_id=None,
        speaker="未辨认的声音",
        text="钥匙",
        perception={"speaker_known": False, "complete": False},
    )
    packet = review_grounding(
        {"profile": {}, "observations": [observed]}, [], {"claims": [{"source_id": "s"}]}, {"s": "钥匙"}
    )
    ref = packet["referenced_records_with_provenance"][0]
    assert ref["text"] == "钥匙"
    assert ref["utterance_observation"]["actor_id"] is None
    assert ref["utterance_observation"]["speaker"] is None
    assert not ref["utterance_observation"]["complete"]


def test_non_observation_reference_does_not_invent_hearing():
    packet = review_grounding({"profile": {}}, [], {"claims": [{"source_id": "s"}]}, {"s": "传闻"})
    assert "utterance_observation" not in packet["referenced_records_with_provenance"][0]


def test_review_includes_uncited_recent_speech_without_promoting_or_unmasking_it():
    events = [
        dict(id="self", kind="speech", actor_id="npc", speaker="陈默", text="我得核实流程。"),
        dict(
            id="heard",
            kind="speech",
            actor_id=None,
            speaker="隐藏身份",
            text="我还没决定",
            perception={"speaker_known": False, "complete": False},
        ),
        dict(id="unavailable", kind="speech", actor_id="other", text="秘密"),
    ]
    packet = review_grounding(
        {"profile": {}, "observations": events},
        [],
        {"claims": []},
        {"self": "我得核实流程。", "heard": "我还没决定"},
    )
    history = packet["recent_observed_utterances"]
    assert [e["id"] for e in history] == ["self", "heard"]
    assert history[0]["text"] == "我得核实流程。"
    assert not history[0]["utterance_observation"]["contents_verified"]
    assert history[1]["utterance_observation"]["actor_id"] is None
    assert history[1]["utterance_observation"]["speaker"] is None
    assert not history[1]["utterance_observation"]["complete"]
    assert "秘密" not in str(packet) and "隐藏身份" not in str(packet)
    assert packet["referenced_records_with_provenance"] == []


def test_recent_review_history_is_bounded_and_marks_truncated_excerpt():
    events = [dict(id=str(i), kind="speech", actor_id="npc", text="字" * 700) for i in range(12)]
    packet = review_grounding(
        {"profile": {}, "observations": events},
        [],
        {"claims": []},
        {e["id"]: e["text"] for e in events},
    )
    history = packet["recent_observed_utterances"]
    assert sum(len(e["text"]) for e in history) == 4000
    assert history[-1]["id"] == "11" and len(history) <= 8
    assert history[0]["excerpt_truncated"]
    assert not history[0]["utterance_observation"]["complete"]


def test_recall_quotes_must_be_literal_even_when_omitted_from_claims():
    sources = {"visible": "有人说原册在医药箱里，但我只是转述。"}
    assert invalid_recalled_quotes("你说“听说有人在医药箱里”。", sources, "我两次说法是什么")
    assert not invalid_recalled_quotes("你说『我只是转述』。", sources, "我两次说法是什么")
    assert not invalid_recalled_quotes("你的意思是还未亲眼确认。", sources, "我两次说法是什么")
    assert not invalid_recalled_quotes("她说了一声“哎”。", {}, "你好")


def test_subjective_label_does_not_bypass_source_boundary():
    claim = {"text": "我怀疑钥匙在他那里", "source_id": "private", "quote": "钥匙", "mode": "belief"}
    speech = {"text": claim["text"], "claims": [claim]}
    assert invalid_claims(speech, {"public": "钥匙"}) == [claim]
    claim["source_id"] = "public"
    assert not invalid_claims(speech, {"public": "钥匙"})
    claim["quote"] = " "
    assert invalid_claims(speech, {"public": "钥匙 "})


async def test_unsupported_check_overrides_reviewer_acceptance(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    reviews = iter(
        [
            {
                "accepted": True,
                "issues": [],
                "checks": [
                    {
                        "text": "我们已经同意正式记录",
                        "basis": "仅同意比较，不包括正式记录",
                        "supported": False,
                    }
                ],
            },
            {"accepted": True, "issues": [], "checks": []},
        ]
    )
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "馆长，你们同意了什么？", "semantic-check")
    task = runtime.claim("foreground")
    await Agents(db, config, runtime, fake).respond(task)
    assert len([c for c in fake.calls if c[0] == "dialogue"]) == 2


async def test_second_semantic_repair_requires_review_before_publication(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    speeches = iter(
        [
            {"text": "雨已经停了。", "claims": []},
            {"text": "雨马上就下。", "claims": []},
            {"text": "我们可以去窗边看看。", "claims": []},
        ]
    )
    reviews = iter(
        [
            {"accepted": False, "issues": ["目前没有雨已停的证据"]},
            {"accepted": False, "issues": ["目前没有即将下雨的证据"]},
            {"accepted": True, "issues": []},
        ]
    )
    fake.replies["dialogue"] = lambda _: next(speeches)
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "去窗边看看天气。", "second-semantic-repair")
    task = runtime.claim("foreground")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["answer"] == "我们可以去窗边看看。"
    assert len([c for c in fake.calls if c[0] == "dialogue"]) == 3
    assert len([c for c in fake.calls if c[0] == "actor_review"]) == 3
    final_prompt = [c for c in fake.calls if c[0] == "dialogue"][-1][1][-1]["content"]
    assert "最后一次修复" in final_prompt
    assert "雨马上就下" in final_prompt and "雨已经停了" not in final_prompt


async def test_citation_repair_does_not_consume_semantic_repair(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    speeches = iter(
        [
            {"text": "旧草稿", "claims": [{"text": "旧草稿", "quote": "不存在", "source_id": "missing"}]},
            {"text": "语义仍待修正", "claims": []},
            {"text": "你愿意说明这条消息的来源吗？", "claims": []},
        ]
    )
    reviews = iter(
        [
            {"accepted": False, "issues": ["无依据的约定"]},
            {"accepted": True, "issues": []},
        ]
    )
    fake.replies["dialogue"] = lambda _: next(speeches)
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "馆长，请核对", "separate-repairs")
    task = runtime.claim("foreground")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["answer"] == "你愿意说明这条消息的来源吗？"
    assert len([c for c in fake.calls if c[0] == "dialogue"]) == 3
    repair_messages = [c for c in fake.calls if c[0] == "dialogue"][-1][1]
    repair = next(m["content"] for m in repair_messages if "后台质量检查（数据）" in m["content"])
    assert "语义仍待修正" in repair and "无依据的约定" in repair
    assert "旧草稿" not in repair


async def test_text_citation_handle_is_repaired_before_review_and_publish(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    speeches = iter(
        [
            {"text": "我会核对[S1]。", "claims": []},
            {"text": "我会先核对，不把猜测当成事实。", "claims": []},
        ]
    )
    fake.replies["dialogue"] = lambda _: next(speeches)
    runtime.submit(player, config.workspace_owner, "馆长，请核对", "text-handle-repair")
    task = runtime.claim("foreground")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["answer"] == "我会先核对，不把猜测当成事实。"
    assert len([call for call in fake.calls if call[0] == "dialogue"]) == 2
    assert len([call for call in fake.calls if call[0] == "actor_review"]) == 1


async def test_nonreceipt_repair_explains_unknown_vs_unreceived_without_relaxing_gate(
    db, config, runtime, fake
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    speeches = iter(
        [
            {"text": "我没收到通知，时间就说不准。", "claims": []},
            {"text": "复航时间我现在说不准；有没有新通知，我也无法核实。", "claims": []},
        ]
    )
    fake.replies["dialogue"] = lambda _: next(speeches)
    runtime.submit(player, config.workspace_owner, "请说你现在知道什么", "notice-repair-specific")
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
    assert result["answer"] == "复航时间我现在说不准；有没有新通知，我也无法核实。"
    calls = [call for call in fake.calls if call[0] == "dialogue"]
    assert len(calls) == 2
    assert "不证明现在尚未收到" in calls[-1][1][-1]["content"]
    assert len([call for call in fake.calls if call[0] == "actor_review"]) == 1


async def test_rejected_part_is_pruned_then_reviewed_before_submission(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    bad, retained = "公告栏在左手边。", "你看过以后，愿意的话回来聊聊。"
    fake.replies["dialogue"] = {
        "text": bad + "\n\n" + retained,
        "spoken_parts": [bad, retained],
        "claims": [],
    }
    reviews = iter(
        [
            {
                "accepted": False,
                "issues": ["布局没有依据"],
                "checks": [
                    {
                        "text": bad,
                        "supported": False,
                        "reason_code": "unsupported_fact",
                        "basis": "未设定布局",
                    }
                ],
            },
            {"accepted": True, "issues": []},
        ]
    )
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "我自己去看看", "prune-and-review")
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
    assert result["answer"] == retained
    assert len([call for call in fake.calls if call[0] == "dialogue"]) == 1
    reviewed = [call for call in fake.calls if call[0] == "actor_review"]
    assert len(reviewed) == 2
    payload = json.loads(reviewed[-1][1][-1]["content"])
    assert payload["candidate"] == retained


async def test_pruned_safe_fragment_still_needs_to_answer_the_task(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    bad, invitation = "公告栏在左手边。", "你看过以后，愿意的话回来聊聊。"
    speeches = iter(
        [
            {"text": bad + "\n\n" + invitation, "spoken_parts": [bad, invitation], "claims": []},
            {"text": "公告栏的具体方位，我现在没法告诉你。", "claims": []},
        ]
    )
    fake.replies["dialogue"] = lambda _: next(speeches)
    reviews = iter(
        [
            {
                "accepted": False,
                "issues": ["没有位置依据"],
                "checks": [
                    {
                        "text": bad,
                        "supported": False,
                        "basis": "无位置记录",
                        "reason_code": "unsupported_fact",
                    }
                ],
            },
            {"accepted": False, "issues": ["邀请回来未回答本轮方位问题；应明确当前未知。"]},
            {"accepted": True, "issues": []},
        ]
    )
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "公告栏具体在哪里？", "pruning-not-task-completion")
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
    assert result["answer"] == "公告栏的具体方位，我现在没法告诉你。"
    assert len([call for call in fake.calls if call[0] == "dialogue"]) == 2
    assert len([call for call in fake.calls if call[0] == "actor_review"]) == 3


async def test_fact_failure_recovers_only_purpose_and_keeps_player_request(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    intents = iter(
        [
            intention(player, purpose="告诉玩家公告栏在何处"),
            {"purpose": "回答公告栏的具体位置问题；当前没有位置依据则明确本人未知。"},
        ]
    )
    fake.replies["role_intent"] = lambda _: next(intents)
    bad, answer = "公告栏就在检票口左侧。", "公告栏具体在哪儿，我现在说不准。"
    speeches = iter([{"text": bad, "claims": []}, {"text": answer, "claims": []}])
    fake.replies["dialogue"] = lambda _: next(speeches)
    reviews = iter(
        [
            {
                "accepted": False,
                "issues": ["位置无依据"],
                "checks": [
                    {
                        "text": bad,
                        "basis": "只有值班员身份，没有位置。",
                        "supported": False,
                        "reason_code": "unsupported_fact",
                    }
                ],
            },
            {"accepted": True, "issues": []},
        ]
    )
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "公告栏具体在哪里？", "fact-purpose-recovery")
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
    assert result["answer"] == answer
    segment = result["segments"][0]
    assert segment["intent"]["addressee_id"] == player
    assert segment["intent"]["purpose"].startswith("回答公告栏的具体位置问题")
    calls = [call for call in fake.calls if call[0] == "role_intent"]
    assert len(calls) == 2
    payload = json.loads(calls[-1][1][-1]["content"])
    assert payload["current_player_request"] == "公告栏具体在哪里？"
    assert payload["original_intent"]["addressee_id"] == player
    assert len([call for call in fake.calls if call[0] == "dialogue"]) == 2
    assert len([call for call in fake.calls if call[0] == "actor_review"]) == 2


async def test_negative_weather_needs_evidence_before_a_reviewer_can_accept_it(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    bad = "风还没真压下来。"
    safe = "此刻的风势，我现在说不准。"
    speeches = iter([{"text": bad, "claims": []}, {"text": safe, "claims": []}])
    fake.replies["dialogue"] = lambda _: next(speeches)
    fake.replies["actor_review"] = {"accepted": True, "issues": []}
    runtime.submit(player, config.workspace_owner, "说清楚你现在知道的天气。", "negative-weather-local-gate")
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
    assert result["answer"] == safe
    reviews = [call for call in fake.calls if call[0] == "actor_review"]
    assert len(reviews) == 1
    assert json.loads(reviews[0][1][-1]["content"])["candidate"] == safe


async def test_local_fact_pruning_is_reviewed_before_publication(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    parts = ["登记册原文我没读过。", "登记册的内容我现在不清楚。"]
    fake.replies["dialogue"] = {"text": "".join(parts), "spoken_parts": parts, "claims": []}
    runtime.submit(player, config.workspace_owner, "登记册原文写什么？", "local-prune")
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
    assert result["answer"] == parts[1]
    assert len([c for c in fake.calls if c[0] == "dialogue"]) == 1
    reviews = [c for c in fake.calls if c[0] == "actor_review"]
    assert len(reviews) == 1
    assert json.loads(reviews[0][1][-1]["content"])["candidate"] == parts[1]


async def test_pruned_local_candidate_must_still_answer_the_original_question(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    speeches = iter(
        [
            {
                "text": "登记册原文我没读过。你好。",
                "spoken_parts": ["登记册原文我没读过。", "你好。"],
                "claims": [],
            },
            {"text": "登记册的内容我现在不清楚。", "claims": []},
        ]
    )
    reviews = iter(
        [
            {"accepted": False, "issues": ["剩余台词没有回答原问题"]},
            {"accepted": True, "issues": []},
        ]
    )
    fake.replies["dialogue"] = lambda _: next(speeches)
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "登记册原文写什么？", "local-prune-missing-answer")
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
    assert result["answer"] == "登记册的内容我现在不清楚。"
    assert len([c for c in fake.calls if c[0] == "dialogue"]) == 2
    assert len([c for c in fake.calls if c[0] == "actor_review"]) == 2


async def test_purpose_recovery_is_once_and_never_authorizes_unsafe_speech(db, config, runtime, fake):
    from chatbot.npc.performance import UnsafeDraftExhausted

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    intents = iter([intention(player, purpose="说明公告栏方位"), {"purpose": "仍然说明公告栏方位"}])
    fake.replies["role_intent"] = lambda _: next(intents)
    bad = "公告栏在检票口左边。"
    fake.replies["dialogue"] = {"text": bad, "claims": []}
    fake.replies["actor_review"] = {
        "accepted": False,
        "issues": ["无位置依据"],
        "checks": [{"text": bad, "basis": "没有位置", "supported": False, "reason_code": "unsupported_fact"}],
    }
    runtime.submit(player, config.workspace_owner, "公告栏在哪儿？", "purpose-recovery-bounded")
    task = runtime.claim("foreground")
    with pytest.raises(UnsafeDraftExhausted):
        await Agents(db, config, runtime, fake).respond(task)
    assert len([call for call in fake.calls if call[0] == "role_intent"]) == 2
    assert len([call for call in fake.calls if call[0] == "dialogue"]) == 3
    with db.session() as s:
        assert not list(
            s.scalars(
                select(SceneRecord).where(
                    SceneRecord.task_id == task.id,
                    SceneRecord.kind == "speech",
                    SceneRecord.actor_id != player,
                )
            )
        )


async def test_stale_goal_id_is_repaired_during_intent_planning(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    attempts = iter(
        [
            intention(player, goal_id="archived-goal"),
            intention(player, goal_id=None),
        ]
    )
    fake.replies["role_intent"] = lambda _: next(attempts)
    runtime.submit(player, config.workspace_owner, "馆长，请继续", "stale-goal-repair")
    task = runtime.claim("foreground")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["segments"]
    assert len([call for call in fake.calls if call[0] == "role_intent"]) == 2


def test_nonurgent_reflection_waits_but_pressure_promotes_existing_job(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    root = runtime.submit(player, config.workspace_owner, "你好", "policy-root")
    with db.session.begin() as s:
        actor = s.get(Conversation, actors[0])
        for i in range(10):
            append_record(
                s,
                story_id=actor.story_id,
                kind="speech",
                actor=actor,
                observer_ids=[actor.id],
                dedupe=f"policy-{i}",
                text=f"话题{i}",
            )
    service = RoleMemoryService(db, config, runtime, fake)
    job = service.schedule(actors[0], config.workspace_owner, parent_id=root.id)
    assert job
    while candidate := runtime.claim("background", background=True):
        assert candidate.id != job.id
        runtime.finish(candidate.id, candidate.lease_version, {"test": True})
    promoted = service.schedule(actors[0], config.workspace_owner, parent_id=root.id, force=True)
    assert promoted.id == job.id
    assert runtime.claim("background", background=True).id == job.id


async def test_same_meaning_memory_keeps_active_identity_and_archived_version(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = launch(client)
    with db.session.begin() as s:
        actor = s.get(Conversation, actors[0])
        events = []
        for i in range(5):
            events.append(
                append_record(
                    s,
                    story_id=story,
                    kind="speech",
                    actor=actor,
                    observer_ids=[actor.id],
                    dedupe=f"merge-{i}",
                    text="我想核对账册。",
                )
            )
        memory = RoleMemory(
            story_id=story, actor_id=actor.id, kind="goal", content="核对账册", source_ids=[events[0].id]
        )
        s.add(memory)
        s.flush()
        mid, source = memory.id, events[1].id
    fake.replies["role_memory"] = {
        "summary": "目标未变",
        "notes": [
            {
                "kind": "goal",
                "content": "继续核对账册",
                "source_id": source,
                "quote": "我想核对账册。",
                "same_as": mid,
            }
        ],
    }
    service = RoleMemoryService(db, config, runtime, fake)
    service.schedule(actors[0], config.workspace_owner, force=True)
    job = runtime.claim("background", background=True)
    await service.reflect(job)
    with db.session() as s:
        current = s.get(RoleMemory, mid)
        assert current.content == "核对账册" and current.version == 2
        assert source in current.source_ids and current.status == "active"
        previous = s.get(RoleMemory, current.replaces_id)
        assert previous.status == "archived" and source not in previous.source_ids
        assert (
            len(
                list(
                    s.scalars(
                        select(RoleMemory).where(
                            RoleMemory.actor_id == actors[0],
                            RoleMemory.kind == "goal",
                            RoleMemory.status == "active",
                        )
                    )
                )
            )
            == 1
        )
        assert s.get(Task, job.id).status == "running"
