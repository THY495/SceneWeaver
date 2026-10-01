"""Shared scene test helpers."""

from chatbot.storage import Conversation, RoleMemory, Story, now
from support.agreements import consent, setup


def world(db, config, runtime, fake):
    story, actors, player, _, data = setup(db, config, fake)
    with db.session.begin() as s:
        p = s.get(Conversation, player)
        p.current_state = {**p.current_state, "location": "corridor"}
        st = s.get(Story, story)
        st.settings = {**st.settings, "activity_until": now().timestamp() + 300, "chain_limit": 1}
        s.add(RoleMemory(story_id=story, actor_id=actors[0], kind="goal", content="依照约定交出铜盒。"))
        for aid in actors[:2]:
            consent(s, s.get(Conversation, aid), data)
    return story, actors, player, data
