"""Short-lived per-page presence; one hidden page must not hide another live page."""


def merge_presence(previous, client_id, visible, typing, tick):
    clients = {key: value for key, value in previous.items() if value.get("until", 0) > tick}
    clients[client_id or "legacy"] = {"visible": visible, "typing": typing, "until": tick + 15}
    # Bound the state even if a client repeatedly reconnects with fresh identifiers.
    clients = dict(sorted(clients.items(), key=lambda pair: pair[1]["until"])[-32:])
    active = [value for value in clients.values() if value["visible"]]
    aggregate = {
        "visible": bool(active),
        "typing": any(value["typing"] for value in active),
        "until": max(value["until"] for value in (active or list(clients.values()))),
    }
    return clients, aggregate
