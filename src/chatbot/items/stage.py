"""Optional player-stage rendering; canonical speech and agreement history stay intact."""

from ..observations import observed_record


def handoff_stage(s, record, viewer_id):
    if not viewer_id or record.redacted or record.payload.get("perception"):
        return {}
    offer = record.payload.get("handoff_offer")
    if offer:
        return {
            "stage": {
                "mode": "narration",
                "text": f"{record.actor_name}提出将{offer['item_name']}交给{offer['recipient_name']}，等待答复。",
            }
        }
    source_id = record.payload.get("handoff_offer_event_id")
    if source_id:
        source = observed_record(s, viewer_id, source_id)
        if source and not source.payload.get("perception") and source.payload.get("handoff_offer"):
            # The client collapses only when the replacement is in the same visible page.
            return {"stage": {"mode": "redundant", "with_event_id": source.id}}
        return {}
    agreement = record.payload.get("agreement") or {}
    if agreement.get("type") != "decision":
        return {}
    proposal = observed_record(s, viewer_id, record.parent_id)
    if not proposal or proposal.payload.get("perception"):
        return {}
    source_id = proposal.payload.get("handoff_offer_event_id")
    if not source_id:
        return {}
    source = observed_record(s, viewer_id, source_id)
    if not source or source.payload.get("perception"):
        return {}
    offer = source.payload.get("handoff_offer")
    value = proposal.payload.get("agreement") or {}
    items = value.get("items", [])
    if (
        not offer
        or len(items) != 1
        or agreement.get("proposal_id") != value.get("proposal_id")
        or agreement.get("version") != value.get("version")
        or agreement.get("item_ids") != [items[0]["id"]]
    ):
        return {}
    verb = {"accept": "同意", "refuse": "拒绝", "withdraw": "撤回了对"}.get(agreement.get("decision"))
    if not verb:
        return {}
    subject = (
        f"接收{offer['item_name']}"
        if record.actor_id == offer["recipient_id"]
        else f"{source.actor_name}将{offer['item_name']}交给{offer['recipient_name']}"
    )
    suffix = "的同意。" if agreement["decision"] == "withdraw" else "。"
    text = f"{record.actor_name}{verb}{subject}{suffix}"
    if agreement["decision"] == "accept":
        text += "此时尚未完成交接。"
    return {"stage": {"mode": "narration", "text": text}}
