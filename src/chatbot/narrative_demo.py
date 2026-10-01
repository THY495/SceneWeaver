"""A small authored branch/merge example; no expected NPC lines are scripted."""


def ferry_story():
    world = "当代雾港，暴雨即将到来。人物都是成年人，此刻都在码头候船亭。"
    names = [
        ("captain", "阿岚", "你是渡船船长，重视安全，说话直接，也愿意倾听乘客。"),
        ("keeper", "沈舟", "你是码头值班员，做事耐心，坚持区分公告、传闻与实际见闻。"),
    ]
    nodes = [
        ("opening", "末班渡船", "候船亭亮着灯，阿岚与沈舟都在场。一阵雷声从远处传来。"),
        ("notice", "公告栏", "你读到公告：遇强风停航，复航时间由值班员收到通知后公布。"),
        ("weather", "窗外的风", "你透过候船亭玻璃看到树枝摇晃，雨水开始敲打窗户。"),
        ("merge", "新的广播", "候船亭广播响起：今晚渡船暂停出发，请旅客在候船亭等待后续通知。"),
        ("wait", "等待消息", "你决定继续在候船亭等待。灯光映在湿漉漉的地面上。"),
        ("plan", "另作打算", "你决定暂缓乘船，留在候船亭思考其它安排。"),
    ]
    return {
        "schema_version": 1,
        "template_id": "ferry-narrative",
        "template_version": 1,
        "title": "末班渡船：剧情分支试玩",
        "description": "六个节点、一次分支汇合。可在任意节点自由交谈，再选择推进。",
        "world": world,
        "people": [
            {
                "alias": alias,
                "profile": {
                    "name": name,
                    "core": core,
                    "world": world,
                    "relationships": [],
                    "document_ids": [],
                },
                "location": "候船亭",
                "goals": [],
                "unnoticed": [],
            }
            for alias, name, core in names
        ],
        "player": {"alias": "player", "name": "旅人", "location": "候船亭"},
        "speech": [],
        "knowledge": [],
        "narrative": {
            "entry": "opening",
            "nodes": [
                {
                    "id": key,
                    "title": title,
                    "text": text,
                    "observers": ["player", "captain", "keeper"]
                    if key in {"opening", "merge"}
                    else ["player"],
                }
                for key, title, text in nodes
            ],
            "edges": [
                {"id": source + "-" + target, "source": source, "target": target, "label": label}
                for source, target, label in [
                    ("opening", "notice", "阅读公告栏"),
                    ("opening", "weather", "观察窗外天气"),
                    ("notice", "merge", "继续等候"),
                    ("weather", "merge", "继续等候"),
                    ("merge", "wait", "留在这里等待消息"),
                    ("merge", "plan", "考虑其它安排"),
                ]
            ],
        },
    }
