"""Authorization, scope and uncertainty over the direct API boundary."""

import json

import pytest

from kaiten_cli.policy import KaitenError


def test_comment_question_has_no_write_or_directory_read(workflow):
    adapter, backend = workflow
    result = adapter.prepare_comment("33", "Готово")
    assert result["ready"] is False and "тегнуть" in result["mention_question"]
    assert not backend.writes
    assert not any(call["path"] == "/users" for call in backend.calls)


@pytest.mark.parametrize("answer", ["нет", "none", "Иван Иванов", "@иван", "ИВАН"])
def test_authorized_comment_exact_internal_text_and_readback(workflow, answer):
    adapter, backend = workflow
    preview = adapter.prepare_comment("33", "Готово; $(touch NEVER)", mention=answer)
    result = adapter.post_comment("33", "Готово; $(touch NEVER)", mention=answer, approval=preview["approval"])
    assert result["status"] == "applied" and result["comment"]["internal"] is True
    assert result["comment"]["text"] == preview["text"]
    assert result["comment"]["author_id"] == 7 and result["comment"]["id"] == 2
    assert len(backend.writes) == 1
    assert "omit@example.invalid" not in json.dumps(preview)


@pytest.mark.parametrize("text", ["", " ", "x" * 4097, "@иван Привет", "@other Привет", "bad\x00"])
def test_invalid_or_embedded_mentions_fail_before_backend(workflow, text):
    adapter, backend = workflow
    with pytest.raises(KaitenError):
        adapter.prepare_comment("33", text)
    assert backend.calls == []


@pytest.mark.parametrize("users,code", [([], "mention_not_found"),
    ([{"id": 7, "full_name": "Иван", "username": "first"}, {"id": 8, "full_name": "Иван", "username": "second"}], "mention_ambiguous")])
def test_user_resolution_requires_one_match(workflow, users, code):
    adapter, backend = workflow
    backend.users = users
    with pytest.raises(KaitenError) as failure:
        adapter.prepare_comment("33", "Готово", mention="Иван")
    assert failure.value.code == code and not backend.writes
    assert "candidates" in failure.value.details


def test_changed_comment_preview_or_account_does_not_send(workflow):
    adapter, backend = workflow
    preview = adapter.prepare_comment("33", "Готово", mention="нет")
    with pytest.raises(KaitenError, match="preview"):
        adapter.post_comment("33", "Другой текст", mention="нет", approval=preview["approval"])
    assert not backend.writes


@pytest.mark.parametrize("kind,applied,expected", [("transport", True, "applied"), ("transport", False, "ambiguous"),
    ("bad_json", False, "ambiguous"), ("rejected", False, "not-applied")])
def test_comment_uncertainty_never_retries(workflow, kind, applied, expected):
    adapter, backend = workflow
    backend.apply = applied
    backend.reply = KaitenError("bad_json" if kind == "bad_json" else "network" if kind == "transport" else "forbidden", "Synthetic failure", {} if kind != "rejected" else {"status_code": 403})
    preview = adapter.prepare_comment("33", "Готово", mention="нет")
    result = adapter.post_comment("33", "Готово", mention="нет", approval=preview["approval"])
    assert result["status"] == expected and result["retry_safe"] is False
    assert len(backend.writes) == 1 and "SENSITIVE" not in json.dumps(result)


def test_old_identical_comment_is_not_proof_timeout_applied(workflow):
    adapter, backend = workflow
    backend.comments[0]["text"] = "Готово"
    backend.apply = False
    backend.reply = KaitenError("network", "Synthetic timeout")
    preview = adapter.prepare_comment("33", "Готово", mention="нет")
    assert adapter.post_comment("33", "Готово", mention="нет", approval=preview["approval"])["status"] == "ambiguous"
    assert len(backend.writes) == 1


@pytest.mark.parametrize("authorization", ["direct", "перемещай"])
def test_same_board_move_with_location_readback(workflow, authorization):
    adapter, backend = workflow
    preview = adapter.prepare_move("33", 6)
    result = adapter.apply_move("33", 6, authorization=authorization, approval=preview["approval"])
    assert result["status"] == "applied"
    assert result["location"] == {"board_id": 12, "column_id": 6, "lane_id": 5}
    assert len(backend.writes) == 1


@pytest.mark.parametrize("kwargs,code", [({"column_id": 999, "authorization": "direct"}, "target_invalid"),
    ({"column_id": 6, "lane_id": 999, "authorization": "direct"}, "target_invalid"),
    ({"column_id": 6, "board_id": 99, "authorization": "direct"}, "forbidden"),
    ({"column_id": 6}, "authorization_required"),
    ({"column_id": 6, "authorization": "перемещай", "approval": "wrong"}, "preview_changed")])
def test_move_scope_target_and_authorization_denials(workflow, kwargs, code):
    adapter, backend = workflow
    with pytest.raises(KaitenError) as failure:
        adapter.apply_move("33", **kwargs)
    assert failure.value.code == code and not backend.writes


def test_stale_proposal_requires_fresh_preview(workflow):
    adapter, backend = workflow
    preview = adapter.prepare_move("33", 6)
    backend.card["column_id"] = 6
    with pytest.raises(KaitenError) as failure:
        adapter.apply_move("33", 6, authorization="перемещай", approval=preview["approval"])
    assert failure.value.code == "preview_changed" and not backend.writes


@pytest.mark.parametrize("applied,expected", [(True, "applied"), (False, "ambiguous")])
def test_move_timeout_uses_readback_without_second_patch(workflow, applied, expected):
    adapter, backend = workflow
    backend.apply = applied
    backend.reply = KaitenError("network", "Synthetic timeout")
    result = adapter.apply_move("33", 6, authorization="direct")
    assert result["status"] == expected and len(backend.writes) == 1


def test_noop_move_and_failed_readback(workflow):
    adapter, backend = workflow
    assert adapter.apply_move("33", 4, authorization="direct")["attempts"] == 0
    backend.readback_error = True
    result = adapter.apply_move("33", 6, authorization="direct")
    assert result["status"] == "ambiguous" and result["readback"] is False
    assert len(backend.writes) == 1


def test_comment_scope_and_missing_answer_do_not_write(workflow):
    adapter, backend = workflow
    with pytest.raises(KaitenError) as failure:
        adapter.prepare_comment("33", "Готово", board_id=99)
    assert failure.value.code == "forbidden" and backend.calls == []
    with pytest.raises(KaitenError) as failure:
        adapter.post_comment("33", "Готово", mention=None, approval="wrong")
    assert failure.value.code == "mention_required" and not backend.writes


def test_comment_failed_readback_never_retries(workflow):
    adapter, backend = workflow
    preview = adapter.prepare_comment("33", "Готово", mention="нет")
    backend.readback_error = True
    result = adapter.post_comment("33", "Готово", mention="нет", approval=preview["approval"])
    assert result["status"] == "ambiguous" and result["readback"] is False
    assert len(backend.writes) == 1


def test_move_rejection_has_definite_result(workflow):
    adapter, backend = workflow
    backend.apply = False
    backend.reply = KaitenError("forbidden", "Synthetic rejection", {"status_code": 403})
    result = adapter.apply_move("33", 6, authorization="direct")
    assert result["status"] == "not-applied" and len(backend.writes) == 1


def test_repeated_user_page_cannot_resolve_unique_name(workflow):
    adapter, backend = workflow
    backend.users = [{"id": i, "full_name": f"User {i}", "username": f"user{i}"} for i in range(1, 101)]
    with pytest.raises(KaitenError) as failure:
        adapter.prepare_comment("33", "Готово", mention="user1")
    assert failure.value.code == "bad_json" and not backend.writes


def test_unconfirmed_internal_response_is_ambiguous(workflow):
    adapter, backend = workflow
    preview = adapter.prepare_comment("33", "Готово", mention="нет")
    original = backend.request

    def request(method, path, **kwargs):
        reply = original(method, path, **kwargs)
        if method == "POST":
            backend.comments[-1]["internal"] = False
        return reply

    backend.request = request
    result = adapter.post_comment("33", "Готово", mention="нет", approval=preview["approval"])
    assert result["status"] == "ambiguous" and len(backend.writes) == 1


def test_changed_credential_invalidates_comment_and_move_approvals(workflow):
    adapter, backend = workflow
    comment = adapter.prepare_comment('33', 'Готово', mention='нет')
    move = adapter.prepare_move('33', 6)
    adapter.credentials.save_credentials('mpstats', 'mpstats.kaiten.ru', 'synthetic-replaced-token')
    with pytest.raises(KaitenError) as error:
        adapter.post_comment('33', 'Готово', mention='нет', approval=comment['approval'])
    assert error.value.code == 'preview_changed'
    with pytest.raises(KaitenError) as error:
        adapter.apply_move('33', 6, authorization='перемещай', approval=move['approval'])
    assert error.value.code == 'preview_changed' and not backend.writes
