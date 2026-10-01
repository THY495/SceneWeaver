"""Short-lived, single-scene media capabilities for native HTML video requests."""

import hashlib
import hmac
import json
import secrets
import time

from fastapi import Depends, HTTPException

from .storage import Conversation, ExperimentInitial, Story
from .video_assets import video_response


def install_player_video_routes(router, db, config, who):
    signing_key = secrets.token_bytes(32)

    def current(actor_id, node_id, identity):
        with db.session() as s:
            actor = s.get(Conversation, actor_id)
            if not actor or actor.owner_id != identity or not actor.current_state.get("player"):
                raise HTTPException(404)
            story = s.get(Story, actor.story_id)
            origin = s.get(ExperimentInitial, actor.story_id)
            state = story.settings.get("narrative", {}) if story else {}
            if (
                not origin
                or state.get("node") != node_id
                or state.get("mode") == "backstage"
                or state.get("execution", {}).get("phase") != "video"
            ):
                raise HTTPException(404)
            node = next(n for n in origin.recipe["narrative"]["nodes"] if n["id"] == node_id)
            asset = next(
                (a for a in origin.recipe.get("video_assets", []) if a["id"] == node["video"]["asset"]), None
            )
            if not asset:
                raise HTTPException(404)
            return asset["content_hash"], actor.epoch, state["execution"]["version"]

    def signature(identity, actor, node, state, expiry):
        payload = json.dumps([identity, actor, node, *state, expiry], separators=(",", ":")).encode()
        return hmac.new(signing_key, payload, hashlib.sha256).hexdigest()

    @router.get("/player-videos/{actor_id}/{node_id}")
    def authenticated_video(actor_id: str, node_id: str, identity=Depends(who)):
        key, *_ = current(actor_id, node_id, identity)
        return video_response(config, identity, key)

    @router.post("/player-videos/{actor_id}/{node_id}/ticket")
    def ticket(actor_id: str, node_id: str, identity=Depends(who)):
        state = current(actor_id, node_id, identity)
        expiry = int(time.time()) + 3600
        return {
            "ticket": f"{expiry}.{signature(identity, actor_id, node_id, state, expiry)}",
            "expires_in": 3600,
        }

    @router.get("/player-video-stream/{actor_id}/{node_id}")
    def stream(actor_id: str, node_id: str, ticket: str = ""):
        # No general API credential in a URL, and no capability for other assets.
        if len(ticket) > 100 or ticket.count(".") != 1:
            raise HTTPException(401, "播放授权无效，请重试。")
        expiry, supplied = ticket.split(".")
        if len(supplied) != 64 or any(c not in "0123456789abcdef" for c in supplied):
            raise HTTPException(401, "播放授权无效，请重试。")
        if not expiry.isascii() or not expiry.isdecimal() or int(expiry) <= time.time():
            raise HTTPException(401, "播放授权已过期，请重试。")
        identity = config.workspace_owner
        state = current(actor_id, node_id, identity)
        expected = signature(identity, actor_id, node_id, state, int(expiry))
        if not hmac.compare_digest(expected, supplied):
            raise HTTPException(401, "播放授权无效，请重试。")
        response = video_response(config, identity, state[0])
        response.headers["Cache-Control"] = "private, no-store"
        return response
