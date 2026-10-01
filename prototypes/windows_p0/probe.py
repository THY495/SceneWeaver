"""Windows packaging/storage spike. Not the production engine or its UI.

Uses the production pure item validator; SQLite receipts and fixture dialogue
are deliberately isolated from the PostgreSQL app and all real credentials.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Limit, Placement, World
from chatbot.items.rules import preview_move


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Probe:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "p0.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS world (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events
                  (seq INTEGER PRIMARY KEY, speaker TEXT NOT NULL, text TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts
                  (id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS knowledge
                  (actor TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL,
                   PRIMARY KEY(actor,id));
            """)
            if db.execute("SELECT version FROM metadata").fetchone() not in (None, (1,)):
                raise ValueError("不支持的 P0 数据版本，未改写存档。")
            if not db.execute("SELECT 1 FROM world").fetchone():
                world = World(
                    items=[instantiate("paper-sheet", "letter", Placement(kind="actor", target="player"))],
                    actors=[
                        ActorState(
                            id=actor,
                            location="pier",
                            lift_limit_g=Limit(mode="unlimited"),
                            carry_limit_g=Limit(mode="unlimited"),
                        )
                        for actor in ["player", "captain"]
                    ],
                )
                db.execute("INSERT INTO metadata VALUES (1)")
                db.execute("INSERT INTO world VALUES (1,?)", (world.model_dump_json(),))
                db.execute(
                    "INSERT INTO events(speaker,text) VALUES (?,?)",
                    ("旁白", "夜色中的候船亭里，你握着一封旧信。"),
                )
                db.executemany(
                    "INSERT INTO knowledge VALUES (?,?,?)",
                    [
                        ("player", "schedule", "末班船尚未离开。"),
                        ("captain", "schedule", "末班船尚未离开。"),
                        ("captain", "secret", "蓝鹭是船长私下约定的暗号。"),
                    ],
                )

    @contextmanager
    def connect(self):
        # Short serialized transactions; no model/network call inside the write transaction.
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def knowledge(self, actor, query):
        if actor not in {"player", "captain"}:
            raise ValueError("未知角色")
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT text FROM knowledge WHERE actor=? AND instr(text,?)>0 ORDER BY id", (actor, query)
                )
            ]

    def snapshot(self):
        with self.connect() as db:
            db.execute("BEGIN")
            world = World.model_validate_json(db.execute("SELECT data FROM world WHERE id=1").fetchone()[0])
            item = world.items[0]
            return {
                "revision": item.revision,
                "placement": item.placement.model_dump(),
                "events": [
                    {"speaker": row[0], "text": row[1]}
                    for row in db.execute("SELECT speaker,text FROM events ORDER BY seq")
                ],
                "receipts": db.execute("SELECT count(*) FROM receipts").fetchone()[0],
            }

    def command(self, request_id, command, text="", revision=1, crash="none"):
        request = encode({"command": command, "text": text, "revision": revision})
        if command not in {"talk", "drop", "take"} or not request_id or len(text) > 2000:
            raise ValueError("无效操作")
        # Fake provider work outside transaction. Real provider integration is not part of this spike.
        reply = "我听见了。末班船还没有离开。〔P0 假模型〕"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request,result FROM receipts WHERE id=?", (request_id,)).fetchone()
            if old:
                if old[0] != request:
                    raise ValueError("同一请求编号不能更改操作")
                return json.loads(old[1])
            if command == "talk":
                if not text.strip():
                    raise ValueError("请输入文字")
                db.executemany(
                    "INSERT INTO events(speaker,text) VALUES (?,?)", [("玩家", text), ("沈舟", reply)]
                )
            else:
                world = World.model_validate_json(
                    db.execute("SELECT data FROM world WHERE id=1").fetchone()[0]
                )
                target = (
                    Placement(kind="location", target="pier")
                    if command == "drop"
                    else Placement(kind="actor", target="player")
                )
                candidate = preview_move(world, "player", "letter", revision, target)
                db.execute("UPDATE world SET data=? WHERE id=1", (candidate.model_dump_json(),))
                db.execute(
                    "INSERT INTO events(speaker,text) VALUES (?,?)",
                    ("旁白", "你放下了旧信。" if command == "drop" else "你拿起了旧信。"),
                )
            result = {"ok": True, "request_id": request_id, "command": command}
            db.execute("INSERT INTO receipts VALUES (?,?,?)", (request_id, request, encode(result)))
            if crash == "before":
                os._exit(71)  # Test process loss with an open transaction/hot journal.
        if crash == "after":
            os._exit(72)  # Committed, but caller did not receive its receipt.
        return result


def gui(probe, smoke=False):
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("Sceneweaver P0 · 独立运行验证（假模型）")
    root.geometry("760x500")
    if smoke:
        root.withdraw()
    tk.Label(
        root, text="候船亭 · 仅验证打包与持久化，不是最终游戏界面", font=("Microsoft YaHei UI", 13)
    ).pack(pady=12)
    history = tk.Text(root, wrap="word", height=16)
    history.pack(fill="both", expand=True, padx=16)
    entry = tk.Entry(root)
    entry.pack(fill="x", padx=16, pady=8)
    status = tk.StringVar()
    tk.Label(root, textvariable=status).pack()

    def refresh():
        state = probe.snapshot()
        history.config(state="normal")
        history.delete("1.0", "end")
        history.insert("end", "\n\n".join(f"{e['speaker']}：{e['text']}" for e in state["events"]))
        history.config(state="disabled")
        history.see("end")
        status.set("旧信：" + ("随身" if state["placement"]["kind"] == "actor" else "候船亭地面"))

    def act(command):
        try:
            probe.command(
                str(uuid4()), command, entry.get() if command == "talk" else "", probe.snapshot()["revision"]
            )
            if command == "talk":
                entry.delete(0, "end")
            refresh()
        except Exception as exc:
            messagebox.showerror("未执行", str(exc))

    buttons = tk.Frame(root)
    buttons.pack(pady=8)
    for label, command in [("交谈（假模型）", "talk"), ("放下旧信", "drop"), ("拿起旧信", "take")]:
        tk.Button(buttons, text=label, command=lambda c=command: act(c)).pack(side="left", padx=5)
    tk.Button(buttons, text="保存并退出", command=root.destroy).pack(side="left", padx=5)
    refresh()
    if smoke:
        root.update()
        root.destroy()
    else:
        root.mainloop()


def main():
    started = time.perf_counter()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SceneweaverP0" / "ferry-v1",
    )
    parser.add_argument("--command", choices=["snapshot", "talk", "drop", "take", "knowledge", "gui-smoke"])
    parser.add_argument("--request-id", default="")
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--text", default="")
    parser.add_argument("--actor", default="player")
    parser.add_argument("--crash", choices=["none", "before", "after"], default="none")
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    try:
        probe = Probe(args.data_dir)
        if not args.command:
            gui(probe)
            return
        if args.command == "snapshot":
            value = probe.snapshot()
        elif args.command == "knowledge":
            value = {"matches": probe.knowledge(args.actor, args.text)}
        elif args.command == "gui-smoke":
            gui(probe, smoke=True)
            value = {"gui_initialized": True}
        else:
            value = probe.command(args.request_id, args.command, args.text, args.revision, args.crash)
        value = {
            **value,
            "frozen": bool(getattr(sys, "frozen", False)),
            "runtime_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        code = 0
    except Exception as exc:
        value, code = {"error": str(exc)}, 1
    if args.result:
        args.result.write_text(encode(value), encoding="utf-8")
    elif sys.stdout:
        print(encode(value))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
